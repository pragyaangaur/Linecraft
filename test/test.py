"""Smoke tests for the Weft chip, driven only through its pins."""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

from weft_host import Host, reset


def ins(op, args=0, delay=0, side=0):
    return (op << 20) | (delay << 15) | (side << 12) | args


def imem_writes(words, origin=0):
    out = []
    for i, w in enumerate(words):
        a = 0x400 + 4 * (origin + i)
        out += [(a, w & 0xFF), (a + 1, (w >> 8) & 0xFF), (a + 2, (w >> 16) & 0xFF)]
    return out


def cfg(e, k, v):
    return (0x100 + 0x40 * e + k, v)


async def start(dut):
    clock = Clock(dut.clk, 10, unit="ns")
    cocotb.start_soon(clock.start())
    await reset(dut)
    return Host(dut)


@cocotb.test()
async def test_id_and_enable(dut):
    host = await start(dut)
    assert (await host.read(0x000))[0] == 0x4C
    await host.write(0x001, 0x5)
    assert (await host.read(0x001))[0] == 0x5


@cocotb.test()
async def test_fifo_loopback(dut):
    """A shuttle program that pulls a byte and pushes it straight back."""
    host = await start(dut)
    prog = [
        ins(5, 1),             # pull block
        ins(6, (2 << 8) | 5),  # mov isr, osr
        ins(4, 1),             # push block
    ]
    await host.write_many(imem_writes(prog) + [cfg(0, 0x0D, 2), cfg(0, 0x09, 0x1)])
    await host.write(0x008, [0x12, 0x34, 0xAB])
    await host.write(0x001, 0x1)
    await ClockCycles(dut.clk, 20)
    status = (await host.read(0x005))[0]
    assert status & 0x10, f"RX FIFO should hold data, status {status:#x}"
    got = await host.read(0x008, 3)
    assert got == [0x12, 0x34, 0xAB], got
    assert ((await host.read(0x005))[0] & 0x10) == 0


async def capture_uart(dut, bit, cycles_per_bit, nbytes, timeout=20000):
    """Decode 8N1 frames from uo_out[bit] by sampling the middle of each bit."""
    out = []
    waited = 0
    while len(out) < nbytes:
        await RisingEdge(dut.clk)
        waited += 1
        assert waited < timeout, "UART timeout"
        if (int(dut.uo_out.value) >> bit) & 1:
            continue
        await ClockCycles(dut.clk, cycles_per_bit // 2)
        assert ((int(dut.uo_out.value) >> bit) & 1) == 0, "bad start bit"
        v = 0
        for i in range(8):
            await ClockCycles(dut.clk, cycles_per_bit)
            v |= ((int(dut.uo_out.value) >> bit) & 1) << i
        await ClockCycles(dut.clk, cycles_per_bit)
        assert (int(dut.uo_out.value) >> bit) & 1, "bad stop bit"
        out.append(v)
    return out


@cocotb.test()
async def test_uart_tx_hand_encoded(dut):
    """UART transmit at 8 clocks per bit on uo_out[0]."""
    host = await start(dut)
    prog = [
        ins(5, 1),                 # 0: pull block
        ins(7, (1 << 8) | 7),      # 1: set x, 7
        ins(7, 0, delay=7),        # 2: set pins, 0 [7]     start bit
        ins(3, 1, delay=6),        # 3: out pins, 1 [6]
        ins(0, (2 << 8) | 3),      # 4: jmp x--, 3
        ins(7, 1, delay=7),        # 5: set pins, 1 [7]     stop bit, wrap to 0
        ins(7, 1),                 # 6: set pins, 1         entry point
        ins(7, (4 << 8) | 1),      # 7: set pindirs, 1
        ins(0, 0),                 # 8: jmp 0
    ]
    writes = imem_writes(prog) + [
        cfg(0, 0x06, 1),           # SET_COUNT = 1
        cfg(0, 0x09, 0x1),         # OUT_RIGHT
        cfg(0, 0x0D, 5),           # WRAP_TOP = 5
        cfg(0, 0x0E, 6),           # START_PC = 6
    ]
    await host.write_many(writes)
    await host.write(0x002, 0x1)   # restart so pc = START_PC
    data = [0x55, 0xA3, 0x00, 0xFF]
    await host.write(0x001, 0x1)
    await ClockCycles(dut.clk, 10)
    cap = cocotb.start_soon(capture_uart(dut, 0, 8, len(data)))
    await host.write(0x008, data)
    got = await cap
    assert got == data, got
