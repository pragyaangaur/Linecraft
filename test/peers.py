"""Independent models of the devices on the other end of the wire.

These are written from the protocol standards and share no code with the Weft toolchain, so a test that passes here checks the chip against the protocol itself and not against our own idea of it.
"""

import cocotb
from cocotb.triggers import RisingEdge


def bit(v, i):
    return (int(v) >> i) & 1


class SpiTarget:
    """SPI target in any mode. It shifts out `reply` bytes MSB first and records what it receives.

    CPOL is the idle SCK level. With CPHA = 0 both sides sample on the leading edge and change data on the trailing edge, and the first bit must be ready when CS falls. With CPHA = 1 data changes on the leading edge and is sampled on the trailing edge.
    """

    def __init__(self, dut, sck, mosi, cs, miso, reply, drive, mode=0):
        # `drive(value, mask)` sets ui_in bits. It must be the host driver's
        # pin setter, because two writers of ui_in in one timestep would
        # overwrite each other's bits.
        self.dut, self.sck, self.mosi, self.cs, self.miso = dut, sck, mosi, cs, miso
        self.drive = drive
        self.cpol, self.cpha = mode >> 1, mode & 1
        self.reply = list(reply)
        self.received = []
        self.cs_frames = 0

    async def run(self):
        dut = self.dut
        prev_sck, prev_cs = self.cpol, 1
        shift_in, nbits, nout = 0, 0, 0
        out_byte = 0
        while True:
            await RisingEdge(dut.clk)
            uo = int(dut.uo_out.value)
            sck, cs, mosi = bit(uo, self.sck), bit(uo, self.cs), bit(uo, self.mosi)
            if prev_cs and not cs:
                self.cs_frames += 1
                shift_in, nbits, nout = 0, 0, 0
                out_byte = self.reply.pop(0) if self.reply else 0
                if self.cpha == 0:
                    self._drive((out_byte >> 7) & 1)
                    nout = 1
            leading = not cs and prev_sck == self.cpol and sck != self.cpol
            trailing = not cs and prev_sck != self.cpol and sck == self.cpol
            sample = leading if self.cpha == 0 else trailing
            change = trailing if self.cpha == 0 else leading
            if change:
                if nout == 8:
                    out_byte = self.reply.pop(0) if self.reply else 0
                    nout = 0
                self._drive((out_byte >> (7 - nout)) & 1)
                nout += 1
            if sample:
                shift_in = ((shift_in << 1) | mosi) & 0xFF
                nbits += 1
                if nbits == 8:
                    self.received.append(shift_in)
                    nbits = 0
            prev_sck, prev_cs = sck, cs

    def _drive(self, v):
        self.drive(v << self.miso, 1 << self.miso)


class I2cTarget:
    """I2C target on an open drain bus, for writes and reads.

    It ACKs its own address and every byte written to it. On a read it sends the bytes in `read_data` and records the controller's ACK (0) or NACK (1) after each one. The chip pulls a line low by setting its output enable, the target pulls SDA low itself, and the resolved bus is fed back on uio_in.
    """

    def __init__(self, dut, sda, scl, address, read_data=()):
        self.dut, self.sda, self.scl, self.address = dut, sda, scl, address
        self.read_data = list(read_data)
        self.transactions = []
        self.read_acks = []
        self.events = []
        self.pull_sda = False

    def _bus(self):
        oe = int(self.dut.uio_oe.value)
        out = int(self.dut.uio_out.value)
        sda = 0 if ((oe >> self.sda) & 1 and not (out >> self.sda) & 1) or self.pull_sda else 1
        scl = 0 if ((oe >> self.scl) & 1 and not (out >> self.scl) & 1) else 1
        return sda, scl

    def _feed(self, sda, scl):
        v = int(self.dut.uio_in.value)
        v = (v & ~((1 << self.sda) | (1 << self.scl))) | (sda << self.sda) | (scl << self.scl)
        self.dut.uio_in.value = v

    async def run(self):
        prev_sda, prev_scl = 1, 1
        mode = "idle"            # idle, addr, write, read, done
        cur, nbits, byte, out = [], 0, 0, 0
        while True:
            await RisingEdge(self.dut.clk)
            sda, scl = self._bus()
            self._feed(sda, scl)
            if scl and prev_scl and prev_sda and not sda:
                self.events.append("START")
                mode, cur, nbits, byte = "addr", [], 0, 0
                self.pull_sda = False
            elif scl and prev_scl and not prev_sda and sda:
                self.events.append("STOP")
                if cur:
                    self.transactions.append(cur)
                mode, cur = "idle", []
                self.pull_sda = False
            elif mode in ("addr", "write", "read") and scl and not prev_scl:     # rising SCL
                if nbits < 8:
                    if mode != "read":
                        byte = (byte << 1) | sda
                    nbits += 1
                else:
                    nbits = 9
                    if mode == "read" and len(cur) > 1:    # not the address ACK
                        self.read_acks.append(sda)
                        if sda:
                            mode = "done"
            elif mode in ("addr", "write", "read") and not scl and prev_scl:     # falling SCL
                if nbits == 8:
                    if mode == "read":
                        self.pull_sda = False                 # let the controller ACK
                    else:
                        cur.append(byte)
                        match = (cur[0] >> 1) == self.address
                        self.pull_sda = match                 # ACK
                        if mode == "addr" and match:
                            mode = "read" if cur[0] & 1 else "write"
                elif nbits == 9:
                    nbits, byte = 0, 0
                    self.pull_sda = False
                    if mode == "read":
                        out = self.read_data.pop(0) if self.read_data else 0xFF
                        cur.append(out)
                        self.pull_sda = not (out >> 7) & 1
                elif mode == "read" and 0 < nbits < 8:
                    self.pull_sda = not (out >> (7 - nbits)) & 1
            prev_sda, prev_scl = sda, scl


def crc16_usb(data):
    """CRC-16/USB: poly 0x8005 reflected (0xA001), init 0xFFFF, final XOR 0xFFFF."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc ^ 0xFFFF


def usb_ls_decode(samples, cycles_per_bit):
    """Decode one low speed USB packet from a list of (d_minus, d_plus) samples, one per clock.

    Low speed idle (J) is D- high. The decoder finds the first K, samples each bit in its middle, undoes NRZI and bit stuffing, and stops at SE0. It returns the packet bytes after SYNC (PID, data and CRC) and the length of the SE0 in bits.
    """
    start = next(i for i, (dm, dp) in enumerate(samples) if dm == 0 and dp == 1)
    bits, level, ones = [], 1, 0
    t = start + cycles_per_bit / 2
    se0_bits = 0
    while int(t) < len(samples):
        dm, dp = samples[int(t)]
        if dm == 0 and dp == 0:
            se0_bits += 1
            t += cycles_per_bit
            continue
        if se0_bits:
            break
        b = 1 if dm == level else 0
        level = dm
        if ones == 6:
            assert b == 0, "stuff bit missing"
            ones = 0
        else:
            bits.append(b)
            ones = ones + 1 if b else 0
        t += cycles_per_bit
    nbytes = len(bits) // 8
    data = [sum(bits[8 * i + j] << j for j in range(8)) for i in range(nbytes)]
    assert data[0] == 0x80, f"bad SYNC {data[0]:#x}"
    return data[1:], se0_bits, len(bits) % 8


def crc15_can(bits):
    """CRC-15/CAN over a list of bits, MSB first: poly 0x4599, init 0."""
    crc = 0
    for b in bits:
        fb = ((crc >> 14) & 1) ^ b
        crc = (crc << 1) & 0x7FFF
        if fb:
            crc ^= 0x4599
    return crc


def can_decode(samples, cycles_per_bit):
    """Decode one CAN 2.0A data frame from a list of line levels, one per clock.

    It finds SOF (the first dominant bit), samples each bit in its middle, removes stuff bits (and checks that each one is the complement of the five before it), and checks the CRC, the delimiters and EOF. It returns (id, data).
    """
    start = samples.index(0)
    t = start + cycles_per_bit / 2
    raw = []
    while int(t) < len(samples):
        raw.append(samples[int(t)])
        t += cycles_per_bit
    bits, i, run, last = [], 0, 0, None

    def take():
        nonlocal i, run, last
        b = raw[i]
        i += 1
        run = run + 1 if b == last else 1
        last = b
        if run == 5:
            stuff = raw[i]
            i += 1
            assert stuff != b, f"bad stuff bit at raw bit {i - 1}"
            run, last = 1, stuff
        return b

    sof = take()
    assert sof == 0
    ident = 0
    for _ in range(11):
        ident = (ident << 1) | take()
    rtr, ide, r0 = take(), take(), take()
    assert (rtr, ide, r0) == (0, 0, 0)
    dlc = 0
    for _ in range(4):
        dlc = (dlc << 1) | take()
    data = []
    for _ in range(dlc):
        v = 0
        for _ in range(8):
            v = (v << 1) | take()
        data.append(v)
    covered = [0] + [(ident >> (10 - k)) & 1 for k in range(11)] + [0, 0, 0] \
        + [(dlc >> (3 - k)) & 1 for k in range(4)] + [(d >> (7 - k)) & 1 for d in data for k in range(8)]
    crc = 0
    for _ in range(15):
        crc = (crc << 1) | take()
    assert crc == crc15_can(covered), f"CRC {crc:#x} != {crc15_can(covered):#x}"
    tail = raw[i:i + 10]
    assert tail == [1] * 10, f"delimiters and EOF should be recessive, got {tail}"
    return ident, data


class JtagTap:
    """A JTAG TAP from IEEE 1149.1 with a 4 bit IR, BYPASS and a 32 bit IDCODE register.

    It follows TCK on the chip's outputs, moves through the 16 state TAP controller on each rising edge, shifts the selected register, and drives TDO on the falling edge.
    """

    NEXT = {
        "TLR": ("RTI", "TLR"), "RTI": ("RTI", "SELDR"), "SELDR": ("CAPDR", "SELIR"),
        "CAPDR": ("SHDR", "EX1DR"), "SHDR": ("SHDR", "EX1DR"), "EX1DR": ("PAUDR", "UPDR"),
        "PAUDR": ("PAUDR", "EX2DR"), "EX2DR": ("SHDR", "UPDR"), "UPDR": ("RTI", "SELDR"),
        "SELIR": ("CAPIR", "TLR"), "CAPIR": ("SHIR", "EX1IR"), "SHIR": ("SHIR", "EX1IR"),
        "EX1IR": ("PAUIR", "UPIR"), "PAUIR": ("PAUIR", "EX2IR"), "EX2IR": ("SHIR", "UPIR"),
        "UPIR": ("RTI", "SELDR"),
    }
    IDCODE_IR, BYPASS_IR = 0b0001, 0b1111

    def __init__(self, dut, tck, tms, tdi, tdo, drive, idcode):
        self.dut, self.tck, self.tms, self.tdi, self.tdo = dut, tck, tms, tdi, tdo
        self.drive, self.idcode = drive, idcode
        self.state, self.ir, self.shift, self.nbits = "TLR", self.IDCODE_IR, 0, 0
        self.out = 0
        self.dr_captures = []

    async def run(self):
        prev = 0
        while True:
            await RisingEdge(self.dut.clk)
            uo = int(self.dut.uo_out.value)
            tck, tms, tdi = bit(uo, self.tck), bit(uo, self.tms), bit(uo, self.tdi)
            if tck and not prev:                       # rising edge: sample and move
                if self.state in ("SHDR", "SHIR"):
                    self.shift = (self.shift >> 1) | (tdi << (self.nbits - 1))
                self.state = self.NEXT[self.state][tms]
                if self.state == "TLR":
                    self.ir = self.IDCODE_IR
                elif self.state == "CAPDR":
                    self.nbits = 32 if self.ir == self.IDCODE_IR else 1
                    self.shift = self.idcode if self.ir == self.IDCODE_IR else 0
                elif self.state == "CAPIR":
                    self.nbits, self.shift = 4, 0b0001
                elif self.state == "UPIR":
                    self.ir = self.shift & 0xF
                elif self.state == "UPDR":
                    self.dr_captures.append(self.shift)
            if prev and not tck:                       # falling edge: next TDO bit
                self.out = self.shift & 1 if self.state in ("SHDR", "SHIR") else 0
                self.drive(self.out << self.tdo, 1 << self.tdo)
            prev = tck


def crc8_maxim(data):
    """CRC-8/MAXIM-DOW (Dallas 1-Wire): reflected polynomial 0x8C, init 0."""
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8C if crc & 1 else crc >> 1
    return crc


class OneWireDevice:
    """A 1-Wire device from the Maxim timing rules that answers READ ROM (0x33).

    `us` is the number of clock cycles per microsecond. The device sees the resolved open drain line on uio pin `pin` and pulls it low itself for presence pulses and for 0 bits it sends.
    """

    def __init__(self, dut, pin, us, rom):
        self.dut, self.pin, self.us, self.rom = dut, pin, us, list(rom)
        self.events = []

    async def run(self):
        us, pin = self.us, self.pin
        low_for, prev, pull_until, t = 0, 1, -1, 0
        mode, bits, cmd, out_bits, sample_at = "idle", 0, 0, [], -1
        presence_at = -1
        while True:
            await RisingEdge(self.dut.clk)
            t += 1
            oe, out = int(self.dut.uio_oe.value), int(self.dut.uio_out.value)
            chip_low = (oe >> pin) & 1 and not (out >> pin) & 1
            dev_low = t < pull_until
            line = 0 if chip_low or dev_low else 1
            v = int(self.dut.uio_in.value)
            self.dut.uio_in.value = (v & ~(1 << pin)) | (line << pin)
            if t == presence_at:
                pull_until = t + 120 * us          # presence pulse
            if line == 0:
                low_for += 1
            if prev == 1 and line == 0 and not dev_low:
                slot_start = t                     # the controller started a slot
                sample_at = t + 30 * us
                if mode == "tx" and out_bits:
                    bit = out_bits.pop(0)
                    if bit == 0:
                        pull_until = t + 30 * us
            if prev == 0 and line == 1:
                if low_for >= 400 * us:
                    self.events.append("RESET")
                    mode, bits, cmd = "cmd", 0, 0
                    presence_at = t + 30 * us
                low_for = 0
            if t == sample_at and mode == "cmd":
                cmd |= line << bits
                bits += 1
                if bits == 8:
                    self.events.append(f"CMD {cmd:#04x}")
                    if cmd == 0x33:
                        mode = "tx"
                        out_bits = [(b >> k) & 1 for b in self.rom for k in range(8)]
                    else:
                        mode = "idle"
            prev = line


def dali_decode(samples, half):
    """Decode DALI forward frames from per-cycle samples of the line (IEC 62386-101).

    A frame is a start bit, 16 data bits MSB first and two stop bits, in Manchester code where a 1 is low then high. Every edge must fall on a multiple of `half` cycles from the start edge, the middle of every bit must have an edge, and the stop bits must be at idle for four half bits. Returns the 16 bit frames in order.
    """
    frames, i, n = [], 0, len(samples)
    while True:
        while i < n and samples[i] == 1:
            i += 1
        if i + 38 * half > n:
            return frames
        start = i
        for t in range(start, start + 38 * half):
            if samples[t] != samples[t - 1]:
                assert (t - start) % half == 0, f"edge at {t - start} cycles is off the half bit grid of {half}"
        syms = [samples[start + k * half + half // 2] for k in range(38)]
        assert syms[:2] == [0, 1], f"bad start bit {syms[:2]}"
        value = 0
        for k in range(16):
            pair = tuple(syms[2 + 2 * k:4 + 2 * k])
            assert pair in ((0, 1), (1, 0)), f"no mid-bit edge in bit {k}"
            value = (value << 1) | (1 if pair == (0, 1) else 0)
        assert syms[34:] == [1, 1, 1, 1], f"bad stop bits {syms[34:]}"
        frames.append(value)
        i = start + 38 * half
