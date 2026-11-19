"""Protocol tests. Each test loads a program from programs/ through the SPI port, runs it, and checks the pins against an independent peer model or decoder."""

import os
import sys

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from weft.asm import assemble                      # noqa: E402
from weft.config import ShuttleConfig, setup_writes, CRC_TX, CRC_RX   # noqa: E402
from weft import linecodes                          # noqa: E402
from weft.crc import CATALOGUE, weft_settings       # noqa: E402

from weft_host import Host, reset                  # noqa: E402
from peers import SpiTarget, I2cTarget, usb_ls_decode, crc16_usb   # noqa: E402
from test import capture_uart                      # noqa: E402

CLK_HZ = 50e6


def program(name, origin=0):
    with open(os.path.join(ROOT, "programs", name + ".weft")) as f:
        return assemble(f".origin {origin}\n" + f.read())


async def start(dut):
    cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
    await reset(dut)
    return Host(dut)


async def load(host, shuttle, cfg, prog):
    await host.write_many(setup_writes(shuttle, cfg, prog))
    await host.write(0x002, 1 << shuttle)      # restart: pc = START_PC


async def push_paced(host, shuttle, data):
    """Push bytes into a TX FIFO, waiting while it is full, as a real host would."""
    for b in data:
        while not ((await host.read(0x005))[0] >> shuttle) & 1:
            pass
        await host.write(0x008 + shuttle, b)


# ----------------------------------------------------------------------
# UART
# ----------------------------------------------------------------------

@cocotb.test()
async def test_uart_tx(dut):
    host = await start(dut)
    cfg = ShuttleConfig(out_base=1, set_base=1, set_count=1, out_right=True)
    cfg.set_ratio(2)                       # 2 cycles per tick, 16 cycles per bit
    await load(host, 0, cfg, program("uart_tx"))
    await host.write(0x001, 0x1)
    await ClockCycles(dut.clk, 20)
    data = [0x00, 0xFF, 0x5A, 0x81, 0x3C, 0xC3]
    cap = cocotb.start_soon(capture_uart(dut, 1, 16, len(data)))
    await push_paced(host, 0, data)
    assert await cap == data


async def drive_uart(dut, host, pin, cycles_per_bit, data, bad_stop=False):
    for i, b in enumerate(data):
        frame = [0] + [(b >> k) & 1 for k in range(8)] + [0 if (bad_stop and i == 0) else 1]
        for v in frame:
            host.set_pins(v << pin, 1 << pin)
            await ClockCycles(dut.clk, cycles_per_bit)
        host.set_pins(1 << pin, 1 << pin)
        await ClockCycles(dut.clk, cycles_per_bit)


@cocotb.test()
async def test_uart_rx(dut):
    host = await start(dut)
    host.set_pins(1 << 2, 1 << 2)          # line idles high
    cfg = ShuttleConfig(in_base=2, jmp_pin=2, in_right=True)
    cfg.set_ratio(2)
    await load(host, 0, cfg, program("uart_rx"))
    await host.write(0x001, 0x1)
    data = [0x00, 0xFF, 0xA5, 0x42]
    await drive_uart(dut, host, 2, 16, data)
    got = await host.read(0x008, 4)
    assert got == data, got
    assert (await host.read(0x003))[0] == 0, "no framing error expected"


@cocotb.test()
async def test_uart_rx_framing_error(dut):
    host = await start(dut)
    host.set_pins(1 << 2, 1 << 2)
    cfg = ShuttleConfig(in_base=2, jmp_pin=2, in_right=True)
    cfg.set_ratio(2)
    await load(host, 0, cfg, program("uart_rx"))
    await host.write(0x001, 0x1)
    await drive_uart(dut, host, 2, 16, [0x11, 0x22], bad_stop=True)
    assert (await host.read(0x003))[0] & 1, "framing error should set IRQ 0"
    assert (await host.read(0x008))[0] == 0x22


# ----------------------------------------------------------------------
# SPI
# ----------------------------------------------------------------------

SPI_MODES = {0: (0, 0, 1), 1: (0, 1, 0), 2: (1, 1, 0), 3: (1, 0, 1)}   # IDLE, SHIFT, SAMPLE


def spi_program(mode):
    idle, shift, sample = SPI_MODES[mode]
    with open(os.path.join(ROOT, "programs", "spi_master.weft")) as f:
        text = f.read()
    text = text.replace(".define IDLE 0", f".define IDLE {idle}")
    text = text.replace(".define SHIFT 0", f".define SHIFT {shift}")
    text = text.replace(".define SAMPLE 1", f".define SAMPLE {sample}")
    return assemble(text)


@cocotb.test()
@cocotb.parametrize(mode=[0, 1, 2, 3])
async def test_spi_master(dut, mode):
    host = await start(dut)
    # SCK = pin 2, MOSI = pin 3, CS = pin 4 on uo_out; MISO = ui_in[0]
    cfg = ShuttleConfig(set_base=2, set_count=3, side_base=2, out_base=3, in_base=0)
    cfg.set_ratio(2)
    await load(host, 0, cfg, spi_program(mode))
    reply = [0x9F, 0x01, 0xEE, 0x70]
    target = SpiTarget(dut, sck=2, mosi=3, cs=4, miso=0, reply=reply, drive=host.set_pins, mode=mode)
    await host.write(0x001, 0x1)
    await ClockCycles(dut.clk, 20)
    cocotb.start_soon(target.run())
    sent = [0xA5, 0x3C, 0xFF, 0x00]
    await host.write(0x008, sent)
    await ClockCycles(dut.clk, 600)
    assert target.received == sent, target.received
    assert await host.read(0x008, 4) == reply
    assert target.cs_frames == 1, target.cs_frames
    assert (int(dut.uo_out.value) >> 4) & 1, "CS should be high again"
    assert ((int(dut.uo_out.value) >> 2) & 1) == mode >> 1, "SCK should rest at CPOL"


# ----------------------------------------------------------------------
# I2C
# ----------------------------------------------------------------------

async def i2c_run(dut, address, payload):
    host = await start(dut)
    cfg = ShuttleConfig(out_base=8, set_base=8, set_count=1, in_base=8, side_base=9)
    cfg.set_ratio(4)                      # 16 cycles per SCL period
    await load(host, 0, cfg, program("i2c_write"))
    target = I2cTarget(dut, sda=0, scl=1, address=address)
    cocotb.start_soon(target.run())
    await host.write(0x001, 0x1)
    await host.write(0x008, payload)
    await ClockCycles(dut.clk, 900)
    acks = await host.read(0x008, len(payload))
    return target, acks


@cocotb.test()
async def test_i2c_write_ack(dut):
    target, acks = await i2c_run(dut, 0x50, [0x50 << 1, 0x12, 0xFE])
    assert target.events == ["START", "STOP"], target.events
    assert target.transactions == [[0xA0, 0x12, 0xFE]], target.transactions
    assert acks == [0, 0, 0], acks


@cocotb.test()
async def test_i2c_write_nack(dut):
    target, acks = await i2c_run(dut, 0x50, [0x51 << 1, 0x12])
    assert target.events == ["START", "STOP"], target.events
    assert acks == [1, 1], acks


# ----------------------------------------------------------------------
# USB low speed
# ----------------------------------------------------------------------

CPB = CLK_HZ / 1.5e6           # 33.33 clock cycles per USB low speed bit
USB_CRC = weft_settings(CATALOGUE["CRC-16/USB"])


def usb_tx_config(rate_scale=1.0):
    cfg = ShuttleConfig(out_base=8, set_base=8, set_count=2, fst_diff=True,
                        out_right=True, autopull=True, pull_thresh=8)
    cfg.set_ratio(CPB * rate_scale / 4)
    cfg.use_line_code(linecodes.usb(), "tx")
    cfg.use_crc(USB_CRC, CRC_TX)
    return cfg


def usb_rx_config(sync_phase=7, synthesised=False):
    cfg = ShuttleConfig(in_base=8, in_right=True, autopush=True, push_thresh=8,
                        sync_en=True, sync_pin=8, sync_phase=sync_phase,
                        match_mask=0x0300, match_val=0)
    cfg.set_ratio(CPB / 2)
    cfg.use_line_code(linecodes.usb(), "rx")
    if synthesised:
        # Replace the hand-written receiver with one derived from the transmitter.
        from weft.synth import synthesize_rx
        code = linecodes.usb()
        rx = synthesize_rx(code.tx, code.tx_init)
        cfg.fst_table, cfg.fst_init = rx.table, rx.init
    cfg.use_crc(USB_CRC, CRC_RX)
    return cfg


async def record_usb(dut, cycles, loopback):
    samples = []
    for _ in range(cycles):
        await RisingEdge(dut.clk)
        out, oe = int(dut.uio_out.value), int(dut.uio_oe.value)
        dm = (out & 1) if oe & 1 else 1
        dp = ((out >> 1) & 1) if oe & 2 else 0
        if loopback:
            dut.uio_in.value = dm | (dp << 1)
        samples.append((dm, dp))
    return samples


@cocotb.test()
async def test_usb_ls_tx(dut):
    """The packet on the wire decodes, with the right CRC16, using a decoder written from the USB spec."""
    host = await start(dut)
    await load(host, 0, usb_tx_config(), program("usb_ls_tx"))
    await host.write(0x001, 0x1)
    payload = [0xC3, 0xFF, 0xFF, 0x7E]         # DATA0 PID, then bytes that force bit stuffing
    rec = cocotb.start_soon(record_usb(dut, 4500, loopback=False))
    # One burst: the shuttle takes SYNC at once and the FIFO holds four more.
    await host.write(0x008, [0x80] + payload)
    samples = await rec
    data, se0_bits, extra = usb_ls_decode(samples, CPB)
    crc = crc16_usb(payload[1:])
    assert data == payload + [crc & 0xFF, crc >> 8], [hex(b) for b in data]
    assert extra == 0
    assert 1 <= se0_bits <= 2


@cocotb.test()
@cocotb.parametrize(synthesised=[False, True])
async def test_usb_ls_loopback(dut, synthesised):
    """Shuttle 0 sends a packet and shuttle 1 receives it on the same pins, checking the CRC.

    With `synthesised`, the receiver's line coder table is the one weft.synth derives from the transmit table.
    """
    host = await start(dut)
    await load(host, 0, usb_tx_config(), program("usb_ls_tx"))
    await load(host, 1, usb_rx_config(synthesised=synthesised), program("usb_ls_rx", origin=32))
    await host.write(0x001, 0x3)
    payload = [0x4B, 0x01, 0x80, 0xFF]        # DATA1 PID and three bytes
    rec = cocotb.start_soon(record_usb(dut, 4000, loopback=True))
    await host.write(0x008, [0x80] + payload)
    # Drain the receiver while the packet arrives: read the RX level, then
    # pop that many bytes in one burst.
    got = []
    while not rec.done():
        level = ((await host.read(0x006))[0] >> 4) & 7
        if level:
            got += await host.read(0x009, level)
    level = ((await host.read(0x006))[0] >> 4) & 7
    if level:
        got += await host.read(0x009, level)
    irq = (await host.read(0x003))[0]
    crc = crc16_usb(payload[1:])
    assert got == [0x80] + payload + [crc & 0xFF, crc >> 8], [hex(b) for b in got]
    assert irq & 1 and not irq & 2, f"IRQ {irq:#x}: CRC check failed"


# ----------------------------------------------------------------------
# CAN
# ----------------------------------------------------------------------

def can_host_bytes(ident, data):
    """Pack the five pad bits and the frame up to the end of the data into bytes, MSB first."""
    bits = [1] * 5 + [0] + [(ident >> (10 - k)) & 1 for k in range(11)] + [0, 0, 0]
    bits += [(len(data) >> (3 - k)) & 1 for k in range(4)]
    bits += [(d >> (7 - k)) & 1 for d in data for k in range(8)]
    assert len(bits) % 8 == 0
    return [sum(bits[8 * i + k] << (7 - k) for k in range(8)) for i in range(len(bits) // 8)]


@cocotb.test()
async def test_can_tx(dut):
    """A CAN data frame with stuffing and CRC-15, checked by a decoder written from ISO 11898."""
    from peers import can_decode
    host = await start(dut)
    cfg = ShuttleConfig(out_base=1, set_base=1, set_count=1, autopull=True, pull_thresh=8)
    cfg.set_ratio(8)                                   # 32 cycles per bit
    cfg.use_line_code(linecodes.can(), "tx")
    cfg.use_crc(weft_settings(CATALOGUE["CRC-15/CAN"]), CRC_TX)
    await load(host, 0, cfg, program("can_tx"))
    await host.write(0x001, 0x1)
    await ClockCycles(dut.clk, 20)
    # Long runs force stuff bits. The frame (5 bytes) fits in the OSR and the
    # FIFO, so the host can push it in one burst and never falls behind.
    ident, data = 0x0F0, [0x00, 0xFF]
    frame = can_host_bytes(ident, data)

    async def record(n):
        out = []
        for _ in range(n):
            await RisingEdge(dut.clk)
            out.append((int(dut.uo_out.value) >> 1) & 1)
        return out

    rec = cocotb.start_soon(record(3500))
    await host.write(0x008, frame)
    samples = await rec
    got_id, got_data = can_decode(samples, 32)
    assert (got_id, got_data) == (ident, data), (hex(got_id), got_data)


# ----------------------------------------------------------------------
# WS2812
# ----------------------------------------------------------------------

@cocotb.test()
async def test_ws2812(dut):
    """Pulse widths on the data pin decode to the pushed colour bytes."""
    host = await start(dut)
    cfg = ShuttleConfig(side_base=5, set_base=5, set_count=1, autopull=True, pull_thresh=8)
    cfg.set_ratio(CLK_HZ / 8e6)                         # 6.25 cycles per tick
    await load(host, 0, cfg, program("ws2812"))
    await host.write(0x001, 0x1)
    colours = [0x12, 0xFF, 0x00, 0xA5]

    async def pulses(n_bits):
        widths, high, prev = [], 0, 0
        while len(widths) < n_bits:
            await RisingEdge(dut.clk)
            v = (int(dut.uo_out.value) >> 5) & 1
            if v:
                high += 1
            elif prev:
                widths.append(high)
                high = 0
            prev = v
        return widths

    cap = cocotb.start_soon(pulses(8 * len(colours)))
    await host.write(0x008, colours)
    widths = await cap
    # 2 ticks is 12.5 cycles and 7 ticks is 43.75 cycles; split at the middle.
    bits = [1 if w > 28 else 0 for w in widths]
    got = [sum(bits[8 * i + k] << (7 - k) for k in range(8)) for i in range(len(colours))]
    assert got == colours, [hex(g) for g in got]
    assert all(11 <= w <= 14 or 42 <= w <= 46 for w in widths), widths


@cocotb.test()
async def test_spi_target(dut):
    """The chip as an SPI peripheral, clocked by a controller model on its input pins."""
    host = await start(dut)
    cfg = ShuttleConfig(in_base=2, out_base=0, set_base=0, set_count=1,
                        autopull=True, autopush=True)
    await load(host, 0, cfg, program("spi_target"))
    reply = [0xC3, 0x5A, 0x01, 0xF0]
    await host.write(0x008, reply)            # the reply is ready before the transaction
    host.set_pins(1 << 3, 0b1110)             # CS high, SCK low
    await host.write(0x001, 0x1)
    await ClockCycles(dut.clk, 10)

    half = 8                                  # SCK = clk / 16
    sent = [0x3C, 0xA5, 0xFF, 0x00]
    got = []
    host.set_pins(0, 1 << 3)                  # CS low
    await ClockCycles(dut.clk, half)
    for byte in sent:
        v = 0
        for i in range(7, -1, -1):
            host.set_pins(((byte >> i) & 1) << 2, 1 << 2)    # MOSI
            await ClockCycles(dut.clk, half)
            host.set_pins(1 << 1, 1 << 1)                    # SCK rises
            v = (v << 1) | (int(dut.uo_out.value) & 1)       # sample MISO
            await ClockCycles(dut.clk, half)
            host.set_pins(0, 1 << 1)                         # SCK falls
        got.append(v)
    await ClockCycles(dut.clk, half)
    host.set_pins(1 << 3, 1 << 3)             # CS high
    await ClockCycles(dut.clk, 20)
    assert got == reply, [hex(g) for g in got]
    assert await host.read(0x008, 4) == sent


@cocotb.test()
async def test_uart_tx_115200_edges(dut):
    """At 115200 baud from 50 MHz a bit is 434.03 cycles, which needs the fractional divider.

    Every edge on the line must land within one clock of its ideal time, measured from the start bit, which shows that the error does not build up across a frame.
    """
    host = await start(dut)
    cfg = ShuttleConfig(out_base=1, set_base=1, set_count=1, out_right=True)
    achieved = cfg.set_rate(CLK_HZ, 8 * 115200)
    assert abs(achieved / 8 / 115200 - 1) < 1e-4
    await load(host, 0, cfg, program("uart_tx"))
    await host.write(0x001, 0x1)
    await ClockCycles(dut.clk, 300)           # two setup instructions at 54 cycles per tick
    cycles_per_bit = CLK_HZ / 115200

    async def edges(n):
        out, prev, t = [], 1, 0
        while len(out) < n:
            await RisingEdge(dut.clk)
            t += 1
            v = (int(dut.uo_out.value) >> 1) & 1
            if v != prev:
                out.append(t)
            prev = v
        return out

    cap = cocotb.start_soon(edges(10))
    await host.write(0x008, [0x55, 0x55])          # 0x55 toggles on every bit
    times = await cap
    t0 = times[0]
    for k, t in enumerate(times[:10]):
        ideal = t0 + k * cycles_per_bit
        assert abs(t - ideal) <= 1.0 + 1e-9, f"edge {k} at {t - t0} cycles, ideal {ideal - t0:.2f}"


@cocotb.test()
@cocotb.parametrize(odd=[True, False])
async def test_uart_parity_from_crc(dut, odd):
    """8O1 and 8E1 frames where the parity bit is a 1 bit CRC with polynomial 1."""
    host = await start(dut)
    cfg = ShuttleConfig(out_base=1, set_base=1, set_count=1, out_right=True,
                        crc_src=CRC_TX, crc_width=1, crc_poly=1, crc_xorout_ones=odd)
    cfg.set_ratio(2)                                   # 16 cycles per bit
    await load(host, 0, cfg, program("uart_tx_parity"))
    await host.write(0x001, 0x1)
    await ClockCycles(dut.clk, 20)
    data = [0x00, 0x01, 0x7F, 0xFF, 0xA5]

    async def frames(n):
        out = []
        while len(out) < n:
            await RisingEdge(dut.clk)
            if (int(dut.uo_out.value) >> 1) & 1:
                continue
            await ClockCycles(dut.clk, 8)
            bits = []
            for _ in range(10):
                await ClockCycles(dut.clk, 16)
                bits.append((int(dut.uo_out.value) >> 1) & 1)
            out.append(bits)
        return out

    cap = cocotb.start_soon(frames(len(data)))
    await push_paced(host, 0, data)
    for byte, bits in zip(data, await cap):
        value = sum(b << i for i, b in enumerate(bits[:8]))
        assert value == byte
        ones = sum(bits[:9])
        assert ones % 2 == (1 if odd else 0), f"{byte:#x}: parity bit {bits[8]}"
        assert bits[9] == 1, "stop bit"


@cocotb.test()
async def test_i2c_read(dut):
    host = await start(dut)
    cfg = ShuttleConfig(out_base=8, set_base=8, set_count=1, in_base=8, side_base=9)
    cfg.set_ratio(4)
    await load(host, 0, cfg, program("i2c_read"))
    target = I2cTarget(dut, sda=0, scl=1, address=0x48, read_data=[0x5A, 0x00, 0xFF])
    cocotb.start_soon(target.run())
    await host.write(0x001, 0x1)
    await host.write(0x008, [(0x48 << 1) | 1, 0, 0, 0])
    await ClockCycles(dut.clk, 1200)
    got = await host.read(0x008, 4)
    assert got == [0, 0x5A, 0x00, 0xFF], [hex(g) for g in got]
    assert target.read_acks == [0, 0, 1], target.read_acks
    assert target.events == ["START", "STOP"], target.events


# ----------------------------------------------------------------------
# A line code the chip was not designed for
# ----------------------------------------------------------------------

@cocotb.test()
async def test_biphase_mark_from_a_transmit_rule(dut):
    """Biphase mark code, defined here as a 4 state transmit rule in a few lines.

    weft.synth derives the receiver, weft.analysis proves the round trip and the run length, and the chip runs the pair in loopback with edge resync.
    """
    from weft.linecodes import Machine, Step
    from weft.synth import synthesize_rx
    from weft.analysis import check_round_trip, max_run_length

    # State (level, half): every bit starts with a transition, and a 1 has a
    # second transition in the middle. The data bit is used on the second half.
    states = [(lvl, half) for lvl in (0, 1) for half in (0, 1)]

    def tx_step(state, b):
        lvl, half = state
        if half == 0:
            return Step((1 - lvl, 1), 1 - lvl, xfer=False)
        return Step((lvl ^ b, 0), lvl ^ b)

    tx = Machine(states, tx_step)
    tx_table, tx_init = tx.table(), tx.index((1, 0))
    rx = synthesize_rx(tx_table, tx_init)
    assert check_round_trip(tx_table, tx_init, rx.table, rx.init).ok
    assert max_run_length(tx_table, tx_init) == 2
    rx_after_first = rx.table[2 * rx.init + 0] & 0xF       # the first symbol is a 0

    host = await start(dut)
    sym = 16                                               # cycles per half-bit symbol
    txc = ShuttleConfig(out_base=9, set_base=9, set_count=1, out_right=True, autopull=True,
                        pull_thresh=8, fst_table=tx_table, fst_init=tx_init)
    txc.set_ratio(sym)
    rxc = ShuttleConfig(in_base=9, in_right=True, autopush=True, push_thresh=8, sync_en=True,
                        sync_pin=9, sync_phase=sym // 2 - 3, fst_table=rx.table, fst_init=rx_after_first)
    rxc.set_ratio(sym)
    await load(host, 0, txc, program("bmc_tx"))
    await load(host, 1, rxc, program("bmc_rx", origin=16))
    await host.write(0x001, 0x3)

    async def loopback(n):
        for _ in range(n):
            await RisingEdge(dut.clk)
            oe, out = int(dut.uio_oe.value), int(dut.uio_out.value)
            line = ((out >> 1) & 1) if (oe >> 1) & 1 else 1
            dut.uio_in.value = line << 1

    data = [0x00, 0xFF, 0xA5, 0x3C]
    run = cocotb.start_soon(loopback(1500))
    await host.write(0x008, data)
    await run
    got = await host.read(0x009, 4)
    assert got == data, [hex(g) for g in got]


# ----------------------------------------------------------------------
# JTAG
# ----------------------------------------------------------------------

def jtag_stream(cycles):
    """Pack (tms, tdi) pairs into bytes, four TCK cycles per byte, first cycle in bits 1:0."""
    cycles = cycles + [(0, 0)] * (-len(cycles) % 4)
    return [sum((tms | tdi << 1) << (2 * k) for k, (tms, tdi) in enumerate(cycles[i:i + 4]))
            for i in range(0, len(cycles), 4)]


@cocotb.test()
async def test_jtag_idcode(dut):
    """Reset the TAP, go to Shift-DR and read the 32 bit IDCODE, checked by a TAP model from IEEE 1149.1."""
    from peers import JtagTap
    host = await start(dut)
    cfg = ShuttleConfig(side_base=2, set_base=2, set_count=3, out_base=3, in_base=0,
                        out_right=True, in_right=True, autopull=True, autopush=True)
    cfg.set_ratio(2)
    await load(host, 0, cfg, program("jtag"))
    idcode = 0x4BA00477
    tap = JtagTap(dut, tck=2, tms=3, tdi=4, tdo=0, drive=host.set_pins, idcode=idcode)
    cocotb.start_soon(tap.run())
    await host.write(0x001, 0x1)
    # Test-Logic-Reset, Run-Test/Idle, Select-DR, Capture-DR, Shift-DR, then 32 shifts
    # with TMS high on the last one (to Exit1-DR), then Update-DR and Run-Test/Idle.
    cycles = [(1, 0)] * 5 + [(0, 0), (1, 0), (0, 0), (0, 0)] + [(0, 0)] * 31 + [(1, 0), (1, 0), (0, 0)]
    cycles += [(0, 0)] * (-len(cycles) % 8)        # whole TDO bytes, staying in Run-Test/Idle
    stream = jtag_stream(cycles)
    tdo_bytes = []
    for i in range(0, len(stream), 4):
        await host.write(0x008, stream[i:i + 4])
        await ClockCycles(dut.clk, 140)
        level = (await host.read(0x006))[0] & 7
        tdo_bytes += await host.read(0x008, level) if level else []
    assert len(tdo_bytes) == len(cycles) // 8
    tdo_bits = [(b >> k) & 1 for b in tdo_bytes for k in range(8)]
    shifted = tdo_bits[9:9 + 32]
    assert sum(b << k for k, b in enumerate(shifted)) == idcode, hex(sum(b << k for k, b in enumerate(shifted)))
    assert tap.state == "RTI" and tap.dr_captures, tap.state


# ----------------------------------------------------------------------
# PS/2
# ----------------------------------------------------------------------

@cocotb.test()
async def test_ps2_rx(dut):
    """A PS/2 device model sends frames; the chip checks odd parity with a 1 bit CRC."""
    host = await start(dut)
    cfg = ShuttleConfig(in_base=2, in_right=True, crc_src=CRC_RX, crc_width=1, crc_poly=1,
                        crc_check=1)
    await load(host, 0, cfg, program("ps2_rx"))
    host.set_pins(0b0110, 0b0110)                      # CLK (pin 1) and DATA (pin 2) idle high
    await host.write(0x001, 0x1)
    await ClockCycles(dut.clk, 20)
    half = 40

    async def send(byte, good_parity=True):
        parity = 1 - (bin(byte).count("1") & 1)
        if not good_parity:
            parity ^= 1
        for b in [0] + [(byte >> k) & 1 for k in range(8)] + [parity, 1]:
            host.set_pins(b << 2, 1 << 2)              # DATA changes while CLK is high
            await ClockCycles(dut.clk, half // 2)
            host.set_pins(0, 1 << 1)                   # CLK low: DATA is valid
            await ClockCycles(dut.clk, half)
            host.set_pins(1 << 1, 1 << 1)
            await ClockCycles(dut.clk, half // 2)
        await ClockCycles(dut.clk, 2 * half)

    for b in (0x1C, 0xF0):                             # 'A' make code, then a break prefix
        await send(b)
    assert (await host.read(0x003))[0] == 0, "no parity error expected"
    await send(0x32, good_parity=False)
    assert (await host.read(0x003))[0] & 1, "parity error should set IRQ 0"
    assert await host.read(0x008, 3) == [0x1C, 0xF0, 0x32]


# ----------------------------------------------------------------------
# Four protocols at once
# ----------------------------------------------------------------------

@cocotb.test()
async def test_four_protocols_at_once(dut):
    """UART, SPI, I2C and WS2812 run together on the four shuttles from one shared instruction memory.

    Each one is checked by its own peer model while the others run, which shows that the shuttles do not disturb each other's timing or pins.
    """
    host = await start(dut)
    uart = ShuttleConfig(out_base=1, set_base=1, set_count=1, out_right=True)
    uart.set_ratio(2)
    spi = ShuttleConfig(set_base=2, set_count=3, side_base=2, out_base=3, in_base=0)
    spi.set_ratio(3)
    i2c = ShuttleConfig(out_base=8, set_base=8, set_count=1, in_base=8, side_base=9)
    i2c.set_ratio(4)
    ws = ShuttleConfig(side_base=5, set_base=5, set_count=1, autopull=True, pull_thresh=8)
    ws.set_ratio(CLK_HZ / 8e6)
    await load(host, 0, uart, program("uart_tx", origin=0))
    await load(host, 1, spi, program("spi_master", origin=8))
    await load(host, 2, i2c, program("i2c_write", origin=21))
    await load(host, 3, ws, program("ws2812", origin=40))

    spi_target = SpiTarget(dut, sck=2, mosi=3, cs=4, miso=0, reply=[0x11, 0x22, 0x33],
                           drive=host.set_pins, mode=0)
    i2c_target = I2cTarget(dut, sda=0, scl=1, address=0x3C)
    await host.write(0x001, 0xF)
    await ClockCycles(dut.clk, 40)
    cocotb.start_soon(spi_target.run())
    cocotb.start_soon(i2c_target.run())

    async def ws_pulses(n):
        widths, high, prev = [], 0, 0
        while len(widths) < n:
            await RisingEdge(dut.clk)
            v = (int(dut.uo_out.value) >> 5) & 1
            if v:
                high += 1
            elif prev:
                widths.append(high)
                high = 0
            prev = v
        return widths

    uart_data, spi_data, i2c_data, ws_data = [0x41, 0x42, 0x43], [0xDE, 0xAD, 0xBE], [0x3C << 1, 0x00, 0xAF], [0x0F, 0xF0]
    uart_cap = cocotb.start_soon(capture_uart(dut, 1, 16, len(uart_data)))
    ws_cap = cocotb.start_soon(ws_pulses(16))
    await host.write(0x008, uart_data)
    await host.write(0x009, spi_data)
    await host.write(0x00A, i2c_data)
    await host.write(0x00B, ws_data)
    await ClockCycles(dut.clk, 1500)

    assert await uart_cap == uart_data
    assert spi_target.received == spi_data, spi_target.received
    assert await host.read(0x009, 3) == [0x11, 0x22, 0x33]
    assert i2c_target.transactions == [i2c_data], i2c_target.transactions
    assert await host.read(0x00A, 3) == [0, 0, 0]
    bits = [1 if w > 28 else 0 for w in await ws_cap]
    assert [sum(bits[8 * i + k] << (7 - k) for k in range(8)) for i in range(2)] == ws_data
