"""Cycle-accurate model of the Weft core (everything except the SPI port).

The model follows docs/spec.md literally so that it can run in lockstep with
the RTL. Host register accesses (`Chip.write`, `Chip.read`) take effect at
once, between clock cycles. `Chip.step` advances one clock cycle.

Order of events inside one `step` (one rising clock edge):

1. Every shuttle runs its divider and, on a tick, one instruction attempt.
   All shuttles read the shared state (pins, pins_prev, IRQ flags,
   instruction memory) as it was at the start of the cycle.
2. The CRC registers take the bits fed in the previous cycle (the spec's
   one-cycle-late update), and the bits fed this cycle become pending.
3. IRQ flags become (flags & ~clears) | sets.
4. The input synchronizer shifts: pins_prev <- pins <- sync1 <- inputs.
5. The pin outputs are derived from the new pv and pe registers.
"""

from collections import deque

from . import isa

ID_VALUE = 0x4C
FIFO_DEPTH = 4
NUM_SHUTTLES = 4
MASK16 = 0xFFFF
MASK32 = 0xFFFFFFFF

# Marks a stalled instruction attempt.
STALL = object()

# Width in bits of every shuttle configuration register, by offset k.
# Offsets that are not listed (0x17 and 0x24 upward) do not exist.
CONFIG_WIDTHS = {
    0x00: 8, 0x01: 8, 0x02: 8, 0x03: 4, 0x04: 4, 0x05: 4, 0x06: 4, 0x07: 4,
    0x08: 3, 0x09: 5, 0x0A: 6, 0x0B: 6, 0x0C: 6, 0x0D: 6, 0x0E: 6, 0x0F: 4,
    0x10: 1, 0x11: 4, 0x12: 8, 0x13: 8, 0x14: 4, 0x15: 4, 0x16: 6,
    0x18: 8, 0x19: 8, 0x1A: 8, 0x1B: 8, 0x1C: 8, 0x1D: 8, 0x1E: 8, 0x1F: 8,
    0x20: 8, 0x21: 8, 0x22: 8, 0x23: 8,
}


def bit(value, i):
    return (value >> i) & 1


def reverse_bits(value, width):
    """Reverse the low `width` bits of `value`."""
    out = 0
    for i in range(width):
        out = (out << 1) | ((value >> i) & 1)
    return out


def set_pin(reg, pin, value):
    """Return the 16 bit register `reg` with bit `pin mod 16` set to `value`."""
    pin &= 15
    return (reg & ~(1 << pin)) | ((value & 1) << pin)


def crc_advance(crc, bits, width, poly):
    """Feed `bits` (in feed order) through the spec's Galois LFSR.

    The polynomial is XORed in unmasked, exactly as the spec writes it; bits
    above the width are cleared again by the next shift and never observed.
    """
    mask = (1 << width) - 1
    for b in bits:
        fb = bit(crc, width - 1) ^ b
        crc = (crc << 1) & mask
        if fb:
            crc ^= poly
    return crc


class Shuttle:
    """One of the four shuttles: registers, FIFOs, divider, line coder, CRC."""

    def __init__(self, index, crc_forwarding=False):
        self.index = index
        self.crc_forwarding = crc_forwarding
        self.regs = bytearray(0x40)
        self.table = [0] * 32
        self.pv = 0
        self.pe = 0
        self.reset()

    # ----- configuration fields, decoded from the raw register bytes -----

    def _word(self, k, nbytes):
        return int.from_bytes(self.regs[k:k + nbytes], "little")

    @property
    def div_int(self): return self._word(0x00, 2)
    @property
    def div_frac(self): return self.regs[0x02]
    @property
    def in_base(self): return self.regs[0x03]
    @property
    def out_base(self): return self.regs[0x04]
    @property
    def set_base(self): return self.regs[0x05]
    @property
    def set_count(self): return self.regs[0x06]
    @property
    def side_base(self): return self.regs[0x07]
    @property
    def side_count(self): return self.regs[0x08] & 3
    @property
    def side_pindir(self): return bit(self.regs[0x08], 2)
    @property
    def out_right(self): return bit(self.regs[0x09], 0)
    @property
    def in_right(self): return bit(self.regs[0x09], 1)
    @property
    def autopull(self): return bit(self.regs[0x09], 2)
    @property
    def autopush(self): return bit(self.regs[0x09], 3)
    @property
    def fst_diff(self): return bit(self.regs[0x09], 4)
    @property
    def pull_thresh(self): return self.regs[0x0A] or 32
    @property
    def push_thresh(self): return self.regs[0x0B] or 32
    @property
    def wrap_bot(self): return self.regs[0x0C]
    @property
    def wrap_top(self): return self.regs[0x0D]
    @property
    def start_pc(self): return self.regs[0x0E]
    @property
    def jmp_pin(self): return self.regs[0x0F]
    @property
    def sync_en(self): return self.regs[0x10]
    @property
    def sync_pin(self): return self.regs[0x11]
    @property
    def sync_phase(self): return self._word(0x12, 2)
    @property
    def fst_init(self): return self.regs[0x14]
    @property
    def crc_src(self): return self.regs[0x15] & 3
    @property
    def crc_init_ones(self): return bit(self.regs[0x15], 2)
    @property
    def crc_xorout_ones(self): return bit(self.regs[0x15], 3)

    @property
    def crc_width(self):
        # 0 means 32. Widths above 32 are outside the spec; treat them as 32.
        w = self.regs[0x16]
        return 32 if w == 0 else min(w, 32)

    @property
    def crc_poly(self): return self._word(0x18, 4)
    @property
    def crc_check(self): return self._word(0x1C, 4)
    @property
    def match_mask(self): return self._word(0x20, 2)
    @property
    def match_val(self): return self._word(0x22, 2)

    @property
    def crc_mask(self):
        return (1 << self.crc_width) - 1

    @property
    def crc_init_value(self):
        return MASK32 if self.crc_init_ones else 0

    # ----- reset, restart and host access -----

    def reset(self):
        """rst_n: configuration to reset values, then a restart, then pv = pe = 0."""
        self.regs[:] = bytes(0x40)
        self.regs[0x00] = 1      # DIV_INT resets to 1
        self.regs[0x0A] = 8      # PULL_THRESH
        self.regs[0x0B] = 8      # PUSH_THRESH
        self.table = [0] * 32
        self.restart()
        self.pv = 0
        self.pe = 0

    def restart(self):
        """Host RESTART: every state item to its restart value; pv and pe are kept."""
        self.pc = self.start_pc
        self.x = 0
        self.y = 0
        self.osr = 0
        self.osr_left = 0
        self.isr = 0
        self.isr_cnt = 0
        self.dly = 0
        self.fst = self.fst_init
        self.fst_err = 0
        self.crc_reg = self.crc_init_value
        self.crc_pending = []     # bits fed last cycle, landing at the end of this one
        self.tx_fifo = deque()
        self.rx_fifo = deque()
        self.cnt = 0
        self.acc = 0
        self.tick = False         # whether the last cycle had a tick (for debugging)
        self._feed = []           # bits fed by this cycle's instruction
        self._crc_reset = False   # CTL CRC executed this cycle

    def write_config(self, k, value):
        if k in CONFIG_WIDTHS:
            self.regs[k] = value & ((1 << CONFIG_WIDTHS[k]) - 1)

    # ----- the CRC as instructions see it -----

    @property
    def crc(self):
        """The CRC value an instruction reads in the current cycle.

        The register is updated one cycle after the feed, and readers see only
        the register, as the spec says. `crc_forwarding=True` folds pending bits
        into reads instead; it is kept only to show that the choice is visible.
        """
        if self.crc_forwarding:
            return crc_advance(self.crc_reg, self.crc_pending, self.crc_width, self.crc_poly)
        return self.crc_reg

    def clock_crc(self):
        """End of cycle: land last cycle's bits, or reset; queue this cycle's bits."""
        if self._crc_reset:
            # A CTL CRC reset wins over the update that was pending.
            self.crc_reg = self.crc_init_value
        elif self.crc_pending:
            self.crc_reg = crc_advance(self.crc_reg, self.crc_pending, self.crc_width, self.crc_poly)
        self.crc_pending = self._feed
        self._feed = []
        self._crc_reset = False

    def _feed_crc(self, data, n, right):
        """Queue the first min(n, 8) bits of an n bit shift, in shift order."""
        k = min(n, 8)
        if right:
            self._feed += [bit(data, i) for i in range(k)]
        else:
            self._feed += [bit(data, n - 1 - i) for i in range(k)]

    # ----- divider -----

    def run_divider(self, enabled, pins, pins_prev):
        """One clock of the divider. Returns True when this cycle is a tick."""
        if not enabled:
            self.cnt = 0
            self.acc = 0
            return False
        if self.sync_en and bit(pins ^ pins_prev, self.sync_pin & 15):
            # Edge on the sync pin: no tick, restart the bit timer at SYNC_PHASE.
            self.cnt = self.sync_phase
            self.acc = 0
            return False
        if self.cnt == 0:
            total = self.acc + self.div_frac
            self.acc = total & 0xFF
            self.cnt = max(self.div_int, 1) - 1 + (total >> 8)
            return True
        self.cnt -= 1
        return False

    # ----- line coder helpers -----

    def _entry(self, b):
        e = self.table[2 * self.fst + b]
        return e & 0xF, bit(e, 4), bit(e, 5), bit(e, 6)   # next, out, xfer, flag

    def fpend(self):
        """True when the current state's two entries are identical and have xfer clear.

        Then the next step does not depend on the data bit at all, so it can run without one.
        """
        e0, e1 = self.table[2 * self.fst], self.table[2 * self.fst + 1]
        return e0 == e1 and not bit(e0, 5)

    # ----- FIFO helpers -----

    def _load_osr(self, byte):
        """What PULL (and autopull) does with a byte from the TX FIFO."""
        self.osr = byte if self.out_right else (byte << 24)
        self.osr_left = self.pull_thresh

    def _push_byte(self):
        return (self.isr >> 24) & 0xFF if self.in_right else self.isr & 0xFF

    # ----- one tick -----

    def run_tick(self, pins, irq, imem):
        """Execute one tick. Returns (irq_sets, irq_clears) from this shuttle."""
        self._irq_sets = 0
        self._irq_clears = 0
        if self.dly > 0:
            self.dly -= 1
            return 0, 0

        op, delay, side, arg = isa.fields(imem[self.pc])
        result = self._execute(op, arg, pins, irq)

        if result is not STALL:
            self.dly = delay
            if result is not None:
                self.pc = result
            elif self.pc == self.wrap_top:
                self.pc = self.wrap_bot
            else:
                self.pc = (self.pc + 1) & 63

        # Side-set is applied on every attempt, after the main effect, so it wins.
        for i in range(self.side_count):
            pin = self.side_base + i
            if self.side_pindir:
                self.pe = set_pin(self.pe, pin, bit(side, i))
            else:
                self.pv = set_pin(self.pv, pin, bit(side, i))
        return self._irq_sets, self._irq_clears

    def _execute(self, op, arg, pins, irq):
        """Run one instruction. Returns STALL, None (no jump) or the jump target."""
        if op == isa.OP_JMP:
            return self._jmp(arg, pins)
        if op == isa.OP_WAIT:
            return self._wait(arg, pins, irq)
        if op == isa.OP_IN:
            return self._in(arg, pins)
        if op == isa.OP_OUT:
            return self._out(arg)
        if op == isa.OP_PUSH:
            return self._push(arg)
        if op == isa.OP_PULL:
            return self._pull(arg)
        if op == isa.OP_MOV:
            return self._mov(arg, pins)
        if op == isa.OP_SET:
            return self._set(arg)
        if op == isa.OP_IRQ:
            if bit(arg, 4):
                self._irq_clears |= 1 << (arg & 7)
            else:
                self._irq_sets |= 1 << (arg & 7)
            return None
        if op == isa.OP_CTL:
            if arg & isa.CTL_CRC:
                self._crc_reset = True
            if arg & isa.CTL_FST:
                self.fst = self.fst_init
                self.fst_err = 0
            return None
        return None  # opcodes 10 to 15: NOP

    def _jmp(self, arg, pins):
        cond = (arg >> 8) & 0xF
        target = arg & 0x3F
        x, y = self.x, self.y
        if cond == 0:
            take = True
        elif cond == 1:
            take = x == 0
        elif cond == 2:
            take = x != 0
            self.x = (x - 1) & MASK16       # decrements whether or not it jumps
        elif cond == 3:
            take = y == 0
        elif cond == 4:
            take = y != 0
            self.y = (y - 1) & MASK16
        elif cond == 5:
            # MORE: an OUT would not stall for lack of data.
            take = (self.osr_left != 0 or (self.autopull and len(self.tx_fifo) != 0)
                    or self.fpend())
        elif cond == 6:
            take = bit(pins, self.jmp_pin & 15) == 1
        elif cond == 7:
            take = bit(pins, self.jmp_pin & 15) == 0
        elif cond == 8:
            take = self.osr_left != 0
        elif cond == 9:
            take = len(self.tx_fifo) != 0
        elif cond == 10:
            take = (pins & self.match_mask) == self.match_val
        elif cond == 11:
            take = (pins & self.match_mask) != self.match_val
        elif cond == 12:
            take = (self.crc & self.crc_mask) == (self.crc_check & self.crc_mask)
        elif cond == 13:
            take = self.fst_err == 1
        elif cond == 14:
            take = self.fpend()
        else:
            take = len(self.rx_fifo) < FIFO_DEPTH
        return target if take else None

    def _wait(self, arg, pins, irq):
        polarity = bit(arg, 7)
        source = (arg >> 5) & 3
        index = arg & 0xF
        if source == isa.WAIT_GPIO:
            value = bit(pins, index)
        elif source == isa.WAIT_PIN:
            value = bit(pins, (self.in_base + index) & 15)
        elif source == isa.WAIT_IRQ:
            value = bit(irq, index & 7)
        else:
            value = 0
        if value != polarity:
            return STALL
        if source == isa.WAIT_IRQ and polarity == 1:
            self._irq_clears |= 1 << (index & 7)
        return None

    def _in(self, arg, pins):
        source = (arg >> 8) & 0xF
        if source == isa.IN_FST:
            return self._in_fst(pins)   # the count is meant to be 1 and is ignored
        n = isa.count_of(arg)
        if source == 0:
            d = 0
            for i in range(n):
                d |= bit(pins, (self.in_base + i) & 15) << i
        elif source == 1:
            d = self.x
        elif source == 2:
            d = self.y
        elif source == 4:
            d = self.isr
        elif source == 5:
            d = self.osr
        else:
            d = 0          # NULL, and undefined sources
        d &= (1 << n) - 1
        return self._shift_in(d, n, self.crc_src == 2)

    def _shift_in(self, d, n, feed):
        """Shift n bits into the ISR with autopush. Returns STALL or None."""
        if self.in_right:
            isr = (self.isr >> n) | (d << (32 - n))
        else:
            isr = (self.isr << n) | d
        isr &= MASK32
        isr_cnt = min(self.isr_cnt + n, 32)
        push = self.autopush and isr_cnt >= self.push_thresh
        if push and len(self.rx_fifo) >= FIFO_DEPTH:
            return STALL   # the whole instruction stalls, nothing changes
        self.isr, self.isr_cnt = isr, isr_cnt
        if feed:
            self._feed_crc(d, n, self.in_right)
        if push:
            self.rx_fifo.append(self._push_byte())
            self.isr = 0
            self.isr_cnt = 0
        return None

    def _in_fst(self, pins):
        """Line coder receive step: one line symbol in, zero or one data bit out."""
        nxt, out, xfer, flag = self._entry(bit(pins, self.in_base & 15))
        if xfer and self._shift_in(out, 1, self.crc_src == 2) is STALL:
            return STALL
        self.fst = nxt
        if flag:
            self.fst_err = 1
        return None

    def _out(self, arg):
        dest = (arg >> 8) & 0xF
        n = isa.count_of(arg)

        # Autopull: refill an empty OSR first, unless OUT FST needs no data bit.
        needs_data = not (dest == isa.OUT_FST and self.fpend())
        if self.autopull and self.osr_left == 0 and needs_data:
            if not self.tx_fifo:
                return STALL
            self._load_osr(self.tx_fifo.popleft())

        if dest == isa.OUT_FST:
            return self._out_fst()

        if self.out_right:
            d = self.osr & ((1 << n) - 1)
            self.osr = self.osr >> n
        else:
            d = self.osr >> (32 - n)
            self.osr = (self.osr << n) & MASK32
        self.osr_left = max(self.osr_left - n, 0)
        if self.crc_src == 1:
            self._feed_crc(d, n, self.out_right)

        if dest == 0:
            # For n > 16 the pin index wraps; the later bit wins.
            for i in range(n):
                self.pv = set_pin(self.pv, self.out_base + i, bit(d, i))
        elif dest == 1:
            self.x = d & MASK16
        elif dest == 2:
            self.y = d & MASK16
        elif dest == 4:
            for i in range(n):
                self.pe = set_pin(self.pe, self.out_base + i, bit(d, i))
        elif dest == 5:
            return d & 0x3F
        elif dest == 6:
            self.isr = d
            self.isr_cnt = n
        return None

    def _out_fst(self):
        """Line coder transmit step: one line symbol out, zero or one data bit used."""
        b = bit(self.osr, 0) if self.out_right else bit(self.osr, 31)
        nxt, out, xfer, flag = self._entry(b)
        self.pv = set_pin(self.pv, self.out_base, out)
        if self.fst_diff:
            # Differential pair: the inverted symbol goes to the next pin.
            self.pv = set_pin(self.pv, self.out_base + 1, out ^ 1)
        if xfer:
            if self.out_right:
                self.osr >>= 1
            else:
                self.osr = (self.osr << 1) & MASK32
            self.osr_left = max(self.osr_left - 1, 0)
            if self.crc_src == 1:
                self._feed.append(b)
        self.fst = nxt
        if flag:
            self.fst_err = 1
        return None

    def _push(self, arg):
        iffull, block = bit(arg, 1), bit(arg, 0)
        if iffull and self.isr_cnt < self.push_thresh:
            return None
        if len(self.rx_fifo) >= FIFO_DEPTH:
            if block:
                return STALL
        else:
            self.rx_fifo.append(self._push_byte())
        self.isr = 0
        self.isr_cnt = 0
        return None

    def _pull(self, arg):
        ifempty, block = bit(arg, 1), bit(arg, 0)
        if ifempty and self.osr_left != 0:
            return None
        if self.tx_fifo:
            self._load_osr(self.tx_fifo.popleft())
        elif block:
            return STALL
        else:
            self.osr = self.x
            self.osr_left = self.pull_thresh
        return None

    def _mov(self, arg, pins):
        dest = (arg >> 8) & 0xF
        operation = (arg >> 6) & 3
        source = arg & 0xF

        if dest == 3 and source == 6:
            # MOV OSR, CRC: lay the CRC out so that r[W-1] leaves first.
            w = self.crc_width
            r = self._crc_result()
            self.osr = reverse_bits(r, w) if self.out_right else (r << (32 - w)) & MASK32
            self.osr_left = w
            return None

        if source == 0:
            rot = self.in_base & 15
            v = ((pins >> rot) | (pins << (16 - rot))) & MASK16
        elif source == 1:
            v = self.x
        elif source == 2:
            v = self.y
        elif source == 4:
            v = self.isr
        elif source == 5:
            v = self.osr
        elif source == 6:
            v = self._crc_result()
        elif source == 7:
            v = ((len(self.rx_fifo) >= FIFO_DEPTH) << 6) | ((len(self.tx_fifo) == 0) << 5) \
                | (self.fst_err << 4) | self.fst
        else:
            v = 0          # NULL, and undefined sources

        if operation == isa.MOV_OP_INVERT:
            v = ~v & MASK32
        elif operation == isa.MOV_OP_REVERSE:
            v = reverse_bits(v, 32)

        if dest == 0:
            self.x = v & MASK16
        elif dest == 1:
            self.y = v & MASK16
        elif dest == 2:
            self.isr = v
            self.isr_cnt = 0
        elif dest == 3:
            self.osr = v
            self.osr_left = 32
        elif dest == 4:
            return v & 0x3F
        return None

    def _crc_result(self):
        """(crc XOR xorout) masked to the width, as MOV reads it."""
        xorout = self.crc_mask if self.crc_xorout_ones else 0
        return (self.crc ^ xorout) & self.crc_mask

    def _set(self, arg):
        dest = (arg >> 8) & 0xF
        imm = arg & 0xFF
        # The immediate has 8 bits, so SET_COUNT above 8 acts as 8.
        count = min(self.set_count, 8)
        if dest == 0:
            for i in range(count):
                self.pv = set_pin(self.pv, self.set_base + i, bit(imm, i))
        elif dest == 1:
            self.x = imm
        elif dest == 2:
            self.y = imm
        elif dest == 4:
            for i in range(count):
                self.pe = set_pin(self.pe, self.set_base + i, bit(imm, i))
        return None

    def snapshot(self):
        """Architectural state, for debugging and lockstep comparison."""
        return {
            "pc": self.pc, "x": self.x, "y": self.y,
            "osr": self.osr, "osr_left": self.osr_left,
            "isr": self.isr, "isr_cnt": self.isr_cnt, "dly": self.dly,
            "fst": self.fst, "fst_err": self.fst_err,
            "crc": self.crc & self.crc_mask,
            "pv": self.pv, "pe": self.pe,
            "tx_fifo": list(self.tx_fifo), "rx_fifo": list(self.rx_fifo),
            "cnt": self.cnt, "acc": self.acc, "tick": self.tick,
        }


class Chip:
    """The whole Weft core: four shuttles, shared memory, IRQs and pins.

    `crc_forwarding` picks how the late CRC update is read (see Shuttle.crc).
    """

    def __init__(self, crc_forwarding=False):
        self.shuttles = [Shuttle(e, crc_forwarding) for e in range(NUM_SHUTTLES)]
        self.reset()

    def reset(self):
        """rst_n low: every register to its reset value."""
        self.imem = [0] * isa.IMEM_SIZE
        self.staging = [0, 0]      # instruction bytes 0 and 1, held until byte 2
        self.enable = 0
        self.irq = 0
        self.sync1 = 0
        self.pins = 0
        self.pins_prev = 0
        self.cycle = 0
        for sh in self.shuttles:
            sh.reset()

    # ----- host register map -----

    def write(self, addr, value):
        """Apply one host register write at once, between cycles."""
        addr &= 0x7FFF
        value &= 0xFF
        if addr == 0x001:
            self.enable = value & 0xF
        elif addr == 0x002:
            for sh in self.shuttles:
                if bit(value, sh.index):
                    sh.restart()
        elif addr == 0x003:
            self.irq &= ~value & 0xFF
        elif addr == 0x004:
            self.irq |= value
        elif 0x008 <= addr <= 0x00B:
            fifo = self.shuttles[addr - 0x008].tx_fifo
            if len(fifo) < FIFO_DEPTH:
                fifo.append(value)
        elif 0x100 <= addr < 0x200:
            self.shuttles[(addr - 0x100) >> 6].write_config(addr & 0x3F, value)
        elif 0x200 <= addr < 0x280:
            self.shuttles[(addr - 0x200) >> 5].table[addr & 0x1F] = value & 0x7F
        elif 0x400 <= addr < 0x500:
            i, b = (addr - 0x400) >> 2, addr & 3
            if b < 2:
                self.staging[b] = value
            elif b == 2:
                self.imem[i] = (value << 16) | (self.staging[1] << 8) | self.staging[0]

    def read(self, addr):
        """Apply one host register read at once. Reading 0x008+e pops."""
        addr &= 0x7FFF
        if addr == 0x000:
            return ID_VALUE
        if addr == 0x001:
            return self.enable
        if addr == 0x003:
            return self.irq
        if addr == 0x005:
            status = 0
            for sh in self.shuttles:
                status |= (len(sh.tx_fifo) < FIFO_DEPTH) << sh.index
                status |= (len(sh.rx_fifo) > 0) << (4 + sh.index)
            return status
        if addr in (0x006, 0x007):
            # RX FIFO levels: low shuttle of the pair in [2:0], high one in [6:4].
            lo, hi = self.shuttles[2 * (addr - 0x006):2 * (addr - 0x006) + 2]
            return len(lo.rx_fifo) | (len(hi.rx_fifo) << 4)
        if 0x008 <= addr <= 0x00B:
            fifo = self.shuttles[addr - 0x008].rx_fifo
            return fifo.popleft() if fifo else 0
        return 0   # write-only and unmapped addresses read as 0

    def write_all(self, writes):
        """Apply a list of (addr, byte) host writes in order."""
        for addr, value in writes:
            self.write(addr, value)

    # ----- one clock cycle -----

    def step(self, ui_in=0, uio_in=0):
        """Advance one clock. Returns (uo_out, uio_out, uio_oe) after the edge."""
        pins, pins_prev, irq, imem = self.pins, self.pins_prev, self.irq, self.imem
        sets = clears = 0
        for sh in self.shuttles:
            sh.tick = sh.run_divider(bit(self.enable, sh.index), pins, pins_prev)
            if sh.tick:
                s, c = sh.run_tick(pins, irq, imem)
                sets |= s
                clears |= c
        for sh in self.shuttles:
            sh.clock_crc()
        self.irq = ((irq & ~clears) | sets) & 0xFF
        self.pins_prev = pins
        self.pins = self.sync1
        self.sync1 = (ui_in & 0xFF) | ((uio_in & 0xFF) << 8)
        self.cycle += 1
        return self.outputs()

    def outputs(self):
        """Pin outputs from the current pv and pe registers."""
        value = 0
        oe = 0
        for pin in range(16):
            for sh in reversed(self.shuttles):   # the highest numbered owner wins
                if bit(sh.pe, pin):
                    value |= bit(sh.pv, pin) << pin
                    oe |= 1 << pin
                    break
        uo_out = value & 0x7F          # uo_out[7] is MISO, not driven by shuttles
        return uo_out, (value >> 8) & 0xFF, (oe >> 8) & 0xFF

    def snapshot(self):
        return {
            "cycle": self.cycle, "enable": self.enable, "irq": self.irq,
            "sync1": self.sync1, "pins": self.pins, "pins_prev": self.pins_prev,
            "shuttles": [sh.snapshot() for sh in self.shuttles],
        }


def run_lockstep(setup_writes, inputs, cycles=None, chip=None):
    """Run the model for lockstep comparison against the RTL.

    `setup_writes` is a list of (addr, byte) host writes applied after reset
    and before the first cycle. `inputs` is either a list of (ui_in, uio_in)
    pairs, one per cycle, or a callback `inputs(cycle, outputs)` returning
    (ui_in, uio_in), where `outputs` is the previous cycle's result (zeros
    before the first cycle); a callback needs `cycles`. Returns the list of
    (uo_out & 0x7F, uio_out, uio_oe) after each cycle.
    """
    chip = chip or Chip()
    chip.write_all(setup_writes)
    if callable(inputs):
        if cycles is None:
            raise ValueError("a callback input needs a cycle count")
        trace, last = [], chip.outputs()
        for cycle in range(cycles):
            last = chip.step(*inputs(cycle, last))
            trace.append(last)
        return trace
    pairs = inputs if cycles is None else inputs[:cycles]
    return [chip.step(ui, uio) for ui, uio in pairs]
