"""Host side helpers for the cocotb tests.

`Host` drives the chip's SPI port the way the RP2350 on the demo board would, bit by bit through the input pins. Everything the tests do to the chip goes through this port, so the tests also check the SPI target itself.
"""

from cocotb.triggers import ClockCycles, RisingEdge

CSN, SCK, MOSI = 7, 6, 5      # ui_in bits used by the host port
MISO = 7                      # uo_out bit


class Host:
    def __init__(self, dut, half_period=4):
        # half_period is in chip clock cycles. The spec needs SCK <= clk / 8.
        self.dut = dut
        self.half = half_period
        self.ui = 1 << CSN    # CSn idle high, other host pins low
        self.dut.ui_in.value = self.ui

    def _set(self, bit, value):
        if value:
            self.ui |= 1 << bit
        else:
            self.ui &= ~(1 << bit)
        self.dut.ui_in.value = self.ui

    def set_pins(self, value, mask):
        """Drive the non host ui_in pins (bits in mask) for protocol tests."""
        mask &= ~((1 << CSN) | (1 << SCK) | (1 << MOSI))
        self.ui = (self.ui & ~mask) | (value & mask)
        self.dut.ui_in.value = self.ui

    async def _byte(self, out):
        got = 0
        for i in range(7, -1, -1):
            self._set(MOSI, (out >> i) & 1)
            await ClockCycles(self.dut.clk, self.half)
            self._set(SCK, 1)
            # sample MISO at the rising edge, as a mode 0 host does
            got = (got << 1) | ((int(self.dut.uo_out.value) >> MISO) & 1)
            await ClockCycles(self.dut.clk, self.half)
            self._set(SCK, 0)
        return got

    async def _start(self, rw, addr):
        self._set(CSN, 0)
        await ClockCycles(self.dut.clk, self.half)
        await self._byte(((rw & 1) << 7) | ((addr >> 8) & 0x7F))
        await self._byte(addr & 0xFF)

    async def _stop(self):
        await ClockCycles(self.dut.clk, self.half)
        self._set(CSN, 1)
        await ClockCycles(self.dut.clk, self.half * 2)

    async def write(self, addr, data):
        if isinstance(data, int):
            data = [data]
        await self._start(1, addr)
        for b in data:
            await self._byte(b & 0xFF)
        await self._stop()

    async def read(self, addr, n=1):
        await self._start(0, addr)
        out = [await self._byte(0) for _ in range(n)]
        await self._stop()
        return out

    async def write_many(self, writes):
        """Apply a list of (addr, byte) writes, merging runs of consecutive addresses."""
        i = 0
        while i < len(writes):
            a0, _ = writes[i]
            run = [writes[i][1]]
            j = i + 1
            while j < len(writes) and writes[j][0] == a0 + len(run) and not (8 <= a0 <= 11):
                run.append(writes[j][1])
                j += 1
            await self.write(a0, run)
            i = j


async def reset(dut):
    dut.ena.value = 1
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 5)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 2)
