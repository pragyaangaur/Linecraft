"""Model tests: small programs run cycle by cycle."""

import pytest

from weft.asm import assemble
from weft.config import CRC_TX, ShuttleConfig, setup_writes
from weft.crc import CATALOGUE, crc_reference, crc_wire_bits, weft_settings
from weft.linecodes import CODES, run_tx
from weft.model import Chip, run_lockstep
from weft.tests.helpers import enable, load, run


# ----- host register map -----

def test_host_registers():
    chip = Chip()
    assert chip.read(0x000) == 0x4C
    chip.write(0x001, 0x5)
    assert chip.read(0x001) == 0x5
    assert chip.read(0x005) == 0x0F          # all TX FIFOs have room, no RX data
    for v in range(6):
        chip.write(0x009, v)                 # only four fit; the rest are dropped
    assert list(chip.shuttles[1].tx_fifo) == [0, 1, 2, 3]
    assert chip.read(0x005) == 0x0D
    assert chip.read(0x008) == 0             # empty RX FIFO reads 0
    chip.shuttles[0].rx_fifo.extend([1, 2])
    chip.shuttles[3].rx_fifo.extend([1, 2, 3, 4])
    assert (chip.read(0x006), chip.read(0x007)) == (0x02, 0x40)
    chip.write(0x004, 0x81)
    chip.write(0x003, 0x01)
    assert chip.read(0x003) == 0x80


def test_instruction_staging_is_shared():
    chip = Chip()
    chip.write(0x400 + 4 * 5 + 0, 0x11)
    chip.write(0x400 + 4 * 5 + 1, 0x22)
    chip.write(0x400 + 4 * 7 + 3, 0x99)      # byte 3 is ignored
    chip.write(0x400 + 4 * 7 + 2, 0x33)      # commits {33, 22, 11} to instruction 7
    assert chip.imem[7] == 0x332211
    assert chip.imem[5] == 0


def test_restart_keeps_pins_and_clears_state():
    chip = Chip()
    load(chip, 0, "set pins, 1\nset pindirs, 1\nset x, 9\nloop: jmp loop", set_count=1)
    chip.write(0x008, 0xAA)
    enable(chip, 0)
    run(chip, 4)
    sh = chip.shuttles[0]
    assert (sh.x, sh.pv, sh.pe, sh.pc) == (9, 1, 1, 3)
    chip.write(0x002, 0x1)
    assert (sh.x, sh.pv, sh.pe, sh.pc, len(sh.tx_fifo)) == (0, 1, 1, 0, 0)
    chip.reset()
    assert (sh.pv, sh.pe) == (0, 0)


# ----- synchronizer, divider, resync -----

def test_synchronizer_latency_is_two_cycles():
    chip = Chip()
    load(chip, 0, "wait 1 gpio 0\nset pins, 1\nset pindirs, 1\nhold: jmp hold", set_count=1)
    enable(chip, 0)
    outs = [chip.step(0)[0] for _ in range(3)]
    outs += [chip.step(1)[0] for _ in range(5)]
    # Input rises in cycle 3, wait sees it in cycle 5, set pins at 6, pindirs at 7.
    assert outs == [0, 0, 0, 0, 0, 0, 0, 1]
    assert chip.pins == 1 and chip.pins_prev == 1


def test_divider_fractional_timing():
    chip = Chip()
    load(chip, 0, "nop", div_int=2, div_frac=128)
    enable(chip, 0)
    ticks = []
    for cycle in range(100):
        chip.step()
        if chip.shuttles[0].tick:
            ticks.append(cycle)
    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    assert ticks[0] == 0
    assert gaps[:6] == [2, 3, 2, 3, 2, 3]
    assert len(ticks) == 40                   # average period 2.5


def test_divider_int_zero_acts_as_one_and_disable_clears():
    chip = Chip()
    load(chip, 0, "nop", div_int=0)
    enable(chip, 0)
    run(chip, 3)
    assert chip.shuttles[0].tick
    chip.write(0x001, 0)
    chip.step()
    assert (chip.shuttles[0].cnt, chip.shuttles[0].acc, chip.shuttles[0].tick) == (0, 0, False)


def test_resync_resets_the_counter():
    chip = Chip()
    load(chip, 0, "nop", div_int=10, sync_en=True, sync_pin=2, sync_phase=3)
    enable(chip, 0)
    sh = chip.shuttles[0]
    ticks = []
    for cycle in range(30):
        chip.step(0b100 if cycle >= 4 else 0)
        if sh.tick:
            ticks.append(cycle)
    # Tick at 0 and 10, then the edge driven at cycle 4 reaches pins at 6 and
    # pins != pins_prev in cycle 6: no tick, cnt = 3, ticks at 10, 20.
    assert ticks == [0, 10, 20]
    chip2 = Chip()
    load(chip2, 0, "nop", div_int=10, sync_en=True, sync_pin=2, sync_phase=3)
    enable(chip2, 0)
    for cycle in range(7):
        chip2.step(0b100 if cycle >= 4 else 0)
    assert chip2.shuttles[0].cnt == 3 and not chip2.shuttles[0].tick


# ----- execution basics -----

def test_delay_and_wrap():
    chip = Chip()
    load(chip, 0, """
            set pindirs, 1
        .wrap_target
            set pins, 1 [3]
            set pins, 0 [1]
        .wrap
            set pins, 1        ; never reached
    """, set_count=1)
    enable(chip, 0)
    outs = [o & 1 for o in run(chip, 13)]
    assert outs == [0, 1, 1, 1, 1, 0, 0, 1, 1, 1, 1, 0, 0]


def test_jmp_x_decrement_and_jump_skips_wrap():
    chip = Chip()
    load(chip, 0, """
            set x, 3
        loop:
            irq set 0
            jmp x-- loop
        .wrap_target
            jmp end
        end:
            jmp end
        .wrap
    """)
    enable(chip, 0)
    run(chip, 11)
    sh = chip.shuttles[0]
    assert sh.x == 0xFFFF          # decremented even when the jump fails
    assert sh.pc == 4              # "jmp end" at the wrap top jumps, it does not wrap


def test_side_set_on_stall_and_wins_over_main_effect():
    chip = Chip()
    load(chip, 0, """
        .side_set 1
            set pindirs, 3     side 1
            wait 1 gpio 5      side 0
            set pins, 1        side 0      ; same pin as side-set: side wins
            set pins, 1        side 1
        hold: jmp hold         side 1
    """, set_base=0, set_count=2, side_base=1)
    enable(chip, 0)
    outs = run(chip, 4)
    assert [o & 3 for o in outs] == [2, 0, 0, 0]   # the stalled wait still drives side 0
    outs = run(chip, 5, ui_in=1 << 5)
    # wait sees the pin 2 cycles later, then set pins writes 0b01 but side 0 wins on pin 1.
    assert [o & 3 for o in outs] == [0, 0, 0, 1, 3]


def test_side_set_pindirs():
    chip = Chip()
    load(chip, 0, ".side_set 1 pindirs\nset pins, 1 side 1\nhold: jmp hold side 0",
         set_count=1, side_base=0)
    enable(chip, 0)
    assert [chip.step()[0] & 1 for _ in range(2)] == [1, 0]


def test_output_priority_and_uio_oe():
    chip = Chip()
    load(chip, 0, ".origin 0\nset pins, 0xff\nset pindirs, 0xff\nh0: jmp h0",
         set_base=8, set_count=8)
    load(chip, 2, ".origin 10\nset pins, 0x0f\nset pindirs, 0x3c\nh2: jmp h2",
         set_base=8, set_count=8)
    enable(chip, 0, 2)
    uo, uio, oe = run_last(chip, 3)
    assert oe == 0xFF
    # Pins 10 to 13 belong to shuttle 2 (values 1,1,0,0); the rest to shuttle 0 (1).
    assert uio == 0b11001111


def run_last(chip, cycles):
    out = None
    for _ in range(cycles):
        out = chip.step()
    return out


def test_pin_7_is_never_driven():
    chip = Chip()
    load(chip, 0, "set pins, 0xff\nset pindirs, 0xff\nh: jmp h", set_count=8)
    enable(chip, 0)
    assert run(chip, 3)[-1] == 0x7F


def test_irq_set_wins_over_clear_in_the_same_cycle():
    chip = Chip()
    load(chip, 0, ".origin 0\nirq set 2\nh0: jmp h0")
    load(chip, 1, ".origin 4\nirq clear 2\nh1: jmp h1")
    load(chip, 2, ".origin 8\nwait 1 irq 5\nh2: jmp h2")
    chip.write(0x004, 1 << 5)
    enable(chip, 0, 1, 2)
    chip.step()
    assert chip.irq == 1 << 2      # 2 set (set beats clear), 5 cleared by the wait


def test_mov_status_and_reverse():
    chip = Chip()
    load(chip, 0, "mov x, status\nmov isr, ::x\nmov osr, !null\nh: jmp h",
         fst_init=5)
    chip.write(0x008, 1)
    enable(chip, 0)
    run(chip, 3)
    sh = chip.shuttles[0]
    assert sh.x == 0b0000101       # rx not full, tx not empty, no error, fst = 5
    assert sh.isr == 0b1010 << 28
    assert (sh.osr, sh.osr_left) == (0xFFFFFFFF, 32)


def test_more_condition():
    # The identity table has no FPEND state, so MORE depends only on the data.
    table = CODES["identity"].tx
    chip = Chip()
    load(chip, 0, "loop: jmp more yes\nirq set 0\nh: jmp h\nyes: irq set 1\nh2: jmp h2",
         autopull=True, fst_table=table)
    chip.write(0x008, 7)
    enable(chip, 0)
    run(chip, 2)
    assert chip.irq == 2           # autopull with a byte waiting counts as more data
    chip2 = Chip()
    load(chip2, 0, "loop: jmp more yes\nirq set 0\nh: jmp h\nyes: irq set 1\nh2: jmp h2",
         fst_table=table)
    chip2.write(0x008, 7)
    enable(chip2, 0)
    run(chip2, 2)
    assert chip2.irq == 1          # without autopull a FIFO byte is not enough


# ----- FIFOs -----

def test_autopull_autopush_and_stall():
    chip = Chip()
    load(chip, 0, """
        .wrap_target
            out x, 4
            in x, 4
        .wrap
    """, out_right=True, autopull=True, autopush=True)
    for b in (0x12, 0xAB, 0x3C, 0x45):
        chip.write(0x008, b)
    enable(chip, 0)
    run(chip, 40)
    sh = chip.shuttles[0]
    assert list(sh.rx_fifo) == [0x21, 0xBA, 0xC3, 0x54]
    for b in (0x67, 0x89):
        chip.write(0x008, b)
    run(chip, 40)
    # The RX FIFO is full, so the in that would push stalls with nothing changed.
    assert sh.pc == 1 and sh.isr_cnt == 4
    got = [chip.read(0x008) for _ in range(4)]
    run(chip, 40)
    got += [chip.read(0x008) for _ in range(2)]
    assert got == [0x21, 0xBA, 0xC3, 0x54, 0x76, 0x98]


def test_push_noblock_drops_and_pull_noblock_copies_x():
    chip = Chip()
    load(chip, 0, """
            set x, 0x5a
            pull noblock
            in osr, 8
            push noblock
            push iffull
            h: jmp h
    """)
    for _ in range(4):
        chip.shuttles[0].rx_fifo.append(0)
    enable(chip, 0)
    run(chip, 6)
    sh = chip.shuttles[0]
    assert sh.osr == 0x5A and sh.osr_left == 8
    assert list(sh.rx_fifo) == [0, 0, 0, 0] and sh.isr == 0 and sh.pc == 5


# ----- a UART transmitter -----

UART_TX = """
    .program uart_tx
        set pins, 1
        set pindirs, 1
    .wrap_target
        pull block
        set pins, 0 [6]       ; start bit, 8 cycles with the next instruction
        set x, 7
    bitloop:
        out pins, 1 [6]
        jmp x-- bitloop
        set pins, 1 [6]       ; stop bit, 8 cycles with the pull
    .wrap
"""


def uart_frame(byte, cycles_per_bit=8):
    bits = [0] + [(byte >> i) & 1 for i in range(8)] + [1]
    return [b for b in bits for _ in range(cycles_per_bit)]


def test_uart_tx_waveform():
    chip = Chip()
    load(chip, 0, UART_TX, set_count=1, out_right=True)
    chip.write(0x008, 0x55)
    chip.write(0x008, 0xA3)
    enable(chip, 0)
    wave = [o & 1 for o in run(chip, 3 + 2 * 80 + 10)]
    assert wave[:3] == [0, 1, 1]       # pe set in cycle 1, pull in cycle 2
    assert wave[3:3 + 160] == uart_frame(0x55) + uart_frame(0xA3)
    assert set(wave[163:]) == {1}      # idle high while pull stalls


# ----- line coder transmit -----

def usb_reference(data):
    """Independent USB encoder: LSB first, stuff a 0 after six 1s, NRZI from J."""
    level, ones, line = 1, 0, []
    for byte in data:
        for i in range(8):
            b = (byte >> i) & 1
            if b:
                ones += 1
            else:
                level ^= 1
                ones = 0
            line.append(level)
            if ones == 6:
                level ^= 1
                ones = 0
                line.append(level)
    return line


USB_TX = """
        set pins, 1
        set pindirs, 1
    .wrap_target
        out fst, 1
    .wrap
"""


def test_usb_out_fst_stream():
    data = [0x80, 0xFF, 0xFF, 0x3F, 0x00, 0xA5, 0x7E, 0xFC]
    usb = CODES["usb"]
    chip = Chip()
    cfg = ShuttleConfig(div_int=4, set_count=1, out_right=True, autopull=True)
    cfg.use_line_code(usb, "tx")
    chip.write_all(setup_writes(0, cfg, assemble(USB_TX)))
    enable(chip, 0)
    pending = list(data)
    line = []
    for cycle in range(8 + 4 * 90):
        while pending and len(chip.shuttles[0].tx_fifo) < 4:
            chip.write(0x008, pending.pop(0))
        uo = chip.step()[0]
        if cycle >= 8 and cycle % 4 == 0:
            line.append(uo & 1)
    expected = usb_reference(data)
    assert expected == run_tx(usb.tx, usb.tx_init, [(b >> i) & 1 for b in data for i in range(8)])
    assert line[:len(expected)] == expected
    assert set(line[len(expected):]) == {expected[-1]}   # stalls holding the line


def test_fst_diff_drives_the_inverted_pair():
    code = CODES["nrzi"]
    chip = Chip()
    cfg = ShuttleConfig(out_base=2, set_base=2, set_count=2, out_right=True,
                        autopull=True, fst_diff=True)
    cfg.use_line_code(code, "tx")
    chip.write_all(setup_writes(0, cfg, assemble("set pindirs, 3\n.wrap_target\nout fst, 1\n.wrap")))
    chip.write(0x008, 0b0110)
    enable(chip, 0)
    outs = [(o >> 2) & 3 for o in run(chip, 5)]
    # Data 0,1,1,0 (LSB first) in NRZI from level 1 gives symbols 0, 0, 0, 1;
    # pin 3 carries the complement of pin 2.
    assert outs[1:] == [0b10, 0b10, 0b10, 0b01]


@pytest.mark.parametrize("name", list(CODES))
def test_every_tx_table_runs_in_the_model(name):
    """The hardware's out fst with autopull matches the software table run."""
    code = CODES[name]
    data = [0xFF, 0x00, 0xF0, 0x5A]
    chip = Chip()
    cfg = ShuttleConfig(set_count=1, out_right=True, autopull=True)
    cfg.use_line_code(code, "tx")
    chip.write_all(setup_writes(0, cfg, assemble("set pindirs, 1\n.wrap_target\nout fst, 1\n.wrap")))
    chip.write_all([(0x008, b) for b in data])
    enable(chip, 0)
    chip.step()          # set pindirs
    line = [chip.step()[0] & 1 for _ in range(80)]
    expected = run_tx(code.tx, code.tx_init, [(b >> i) & 1 for b in data for i in range(8)])
    assert line[:len(expected)] == expected


# ----- USB loopback through the synchronizer with edge resync -----

USB_RX = """
        wait 1 pin 0
        wait 0 pin 0          ; the first falling edge is data bit 0
    .wrap_target
        in fst, 1
    .wrap
"""


def usb_loopback(rx_frac, sync_en):
    data = [0x80, 0xFF, 0xFF, 0x3F, 0x00, 0xA5, 0x7E, 0xFC]
    usb = CODES["usb"]
    chip = Chip()
    tx = ShuttleConfig(div_int=4, set_count=1, out_right=True, autopull=True)
    tx.use_line_code(usb, "tx")
    chip.write_all(setup_writes(0, tx, assemble(USB_TX)))
    rx = ShuttleConfig(div_int=4, div_frac=rx_frac, in_base=1, in_right=True, autopush=True,
                       sync_en=sync_en, sync_pin=1, sync_phase=1)
    rx.use_line_code(usb, "rx")
    rx.fst_init = usb.rx_init - 7        # level 0, no ones: the wait ate the first 0
    chip.write_all(setup_writes(1, rx, assemble(".origin 8\n" + USB_RX)))
    enable(chip, 0, 1)
    pending, got, wire = list(data), [], 0
    for _ in range(4 * 100):
        while pending and len(chip.shuttles[0].tx_fifo) < 4:
            chip.write(0x008, pending.pop(0))
        wire = chip.step(ui_in=wire << 1)[0] & 1
        if chip.read(0x005) & 0x20:
            got.append(chip.read(0x009))
    bits = [(b >> i) & 1 for b in data for i in range(8)][1:]
    want = [sum(bits[8 * k + i] << i for i in range(8)) for k in range(len(bits) // 8)]
    return got[:len(want)], want


def test_usb_loopback_decodes():
    got, want = usb_loopback(rx_frac=0, sync_en=True)
    assert got == want


def test_usb_loopback_resync_absorbs_clock_error():
    # The receiver runs 2% slow (4.08 cycles per symbol). R = 7 and M = 4 give a
    # guaranteed tolerance of (1/2 - 1/4) / 7 = 3.6%, so resync must save it.
    got, want = usb_loopback(rx_frac=20, sync_en=True)
    assert got == want
    got, want = usb_loopback(rx_frac=20, sync_en=False)
    assert got != want


# ----- CRC -----

CRC_PROGRAM = """
        set pindirs, 1
        set y, 8              ; nine message bytes
    byte:
        pull block
        out null, 8           ; feeds the CRC (CRC_SRC = transmit)
        jmp y-- byte
        mov osr, crc
        set x, {top}
    bit:
        out pins, 1           ; sends the CRC; its bits are fed too
        jmp x-- bit
        jmp crcok good
    bad:
        jmp bad
    good:
        irq set 0
    park:
        jmp park
"""


@pytest.mark.parametrize("name", ["CRC-5/USB", "CRC-16/USB", "CRC-32/ISO-HDLC", "CRC-15/CAN",
                                  "CRC-8/SMBUS", "CRC-7/MMC", "CRC-16/IBM-3740"])
def test_crc_check_values_in_the_model(name):
    spec = CATALOGUE[name]
    settings = weft_settings(spec)
    program = assemble(CRC_PROGRAM.format(top=spec.width - 1))
    cfg = ShuttleConfig(set_count=1, out_right=settings.lsb_first)
    cfg.use_crc(settings, CRC_TX)
    chip = Chip()
    chip.write_all(setup_writes(0, cfg, program))
    message = b"123456789"
    enable(chip, 0)
    sent = []
    for cycle in range(200):
        if message and len(chip.shuttles[0].tx_fifo) < 4:
            chip.write(0x008, message[0])
            message = message[1:]
        at_bit = chip.shuttles[0].pc == program.labels["bit"]
        uo = chip.step()[0]
        if at_bit:
            sent.append(uo & 1)
    assert sent == crc_wire_bits(spec, spec.check)
    assert crc_reference(spec, b"123456789") == spec.check
    assert chip.irq == 1, "CRCOK should hold after the CRC itself went through"


def test_crc_update_is_visible_to_the_next_instruction():
    """The update lands a cycle late, yet the very next instruction sees it.

    With forwarding off (the register alone), the next instruction would still
    read the init value, which contradicts the spec's second CRC sentence.
    """
    spec = CATALOGUE["CRC-8/SMBUS"]
    cfg = ShuttleConfig()
    cfg.use_crc(weft_settings(spec), CRC_TX)
    program = assemble("pull\nout null, 8\nmov x, crc\nh: jmp h")
    for forwarding, want in ((True, crc_reference(spec, b"1")), (False, 0)):
        chip = Chip(crc_forwarding=forwarding)
        chip.write_all(setup_writes(0, cfg, program))
        chip.write(0x008, ord("1"))
        enable(chip, 0)
        run(chip, 3)
        assert chip.shuttles[0].x == want


def test_crc_reset_wins_over_pending_update_and_min8_rule():
    spec = CATALOGUE["CRC-8/SMBUS"]
    cfg = ShuttleConfig()
    cfg.use_crc(weft_settings(spec), CRC_TX)
    chip = Chip()
    chip.write_all(setup_writes(0, cfg, assemble(
        "pull\nout null, 8\nctl crc\nmov x, crc\nmov osr, ::null\nout null, 16\nmov y, crc\nh: jmp h")))
    chip.write(0x008, 0x31)
    enable(chip, 0)
    run(chip, 8)
    sh = chip.shuttles[0]
    assert sh.x == 0                    # reset in the cycle the update was pending
    # out null, 16 of a zero OSR feeds only 8 zero bits, giving CRC of one 0x00 byte.
    assert sh.y == crc_reference(spec, b"\x00") == 0


# ----- lockstep helper -----

def test_run_lockstep_with_list_and_callback():
    program = assemble(UART_TX)
    writes = setup_writes(0, ShuttleConfig(set_count=1, out_right=True), program)
    writes += [(0x008, 0x55), (0x001, 1)]
    trace = run_lockstep(writes, [(0, 0)] * 90)
    assert [t[0] & 1 for t in trace][3:83] == uart_frame(0x55)
    assert run_lockstep(writes, lambda cycle, out: (0, 0), cycles=90) == trace
