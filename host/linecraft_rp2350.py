"""MicroPython driver for Linecraft on the Tiny Tapeout demo board's RP2350.

This has not been run on hardware yet. It follows the host port in docs/spec.md: SPI mode 0, MSB first, SCK at most one eighth of the chip clock, a two byte header {rw, addr[14:8]}, addr[7:0], then data bytes.

    from machine import Pin, SPI
    import linecraft_rp2350 as linecraft

    spi = SPI(0, baudrate=5_000_000, polarity=0, phase=0,
              sck=Pin(SCK_GPIO), mosi=Pin(MOSI_GPIO), miso=Pin(MISO_GPIO))
    chip = linecraft.Linecraft(spi, Pin(CS_GPIO, Pin.OUT, value=1))
    chip.load("uart.json")          # made by: python -m linecraft.export uart_tx ...
    chip.enable(0b0001)
    chip.push(0, b"hello")

The GPIO numbers depend on how the demo board routes ui_in[5..7] and uo_out[7] to the RP2350, so they are left to the caller.
"""

import json

FIFO = 0x008
ENABLE, RESTART, IRQ, IRQ_SET, STATUS, LEVEL01, LEVEL23 = 0x001, 0x002, 0x003, 0x004, 0x005, 0x006, 0x007


class Linecraft:
    def __init__(self, spi, cs):
        self.spi = spi
        self.cs = cs
        self.cs.value(1)

    def write(self, addr, data):
        if isinstance(data, int):
            data = bytes([data])
        self.cs.value(0)
        self.spi.write(bytes([0x80 | ((addr >> 8) & 0x7F), addr & 0xFF]) + bytes(data))
        self.cs.value(1)

    def read(self, addr, n=1):
        self.cs.value(0)
        self.spi.write(bytes([(addr >> 8) & 0x7F, addr & 0xFF]))
        data = self.spi.read(n)
        self.cs.value(1)
        return data

    def ident(self):
        return self.read(0x000)[0]          # 0x4C

    def load(self, path):
        """Replay the writes in a JSON file from `python -m linecraft.export`, merging consecutive addresses."""
        with open(path) as f:
            writes = json.load(f)["writes"]
        i = 0
        while i < len(writes):
            addr, run = writes[i][0], [writes[i][1]]
            j = i + 1
            while j < len(writes) and writes[j][0] == addr + len(run) and not FIFO <= addr <= FIFO + 3:
                run.append(writes[j][1])
                j += 1
            self.write(addr, bytes(run))
            i = j

    def enable(self, mask):
        self.write(ENABLE, mask & 0xF)

    def restart(self, mask):
        self.write(RESTART, mask & 0xF)

    def irq(self):
        return self.read(IRQ)[0]

    def clear_irq(self, mask=0xFF):
        self.write(IRQ, mask)

    def push(self, shuttle, data):
        """Send bytes to a shuttle's TX FIFO, waiting while it is full."""
        for b in data:
            while not (self.read(STATUS)[0] >> shuttle) & 1:
                pass
            self.write(FIFO + shuttle, b)

    def level(self, shuttle):
        v = self.read(LEVEL01 if shuttle < 2 else LEVEL23)[0]
        return (v >> (4 * (shuttle & 1))) & 7

    def pop(self, shuttle):
        """Read everything waiting in a shuttle's RX FIFO."""
        n = self.level(shuttle)
        return self.read(FIFO + shuttle, n) if n else b""
