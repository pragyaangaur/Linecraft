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
    """I2C target on an open drain bus. It ACKs its own address and every data byte.

    The chip pulls a line low by setting its output enable. The target pulls SDA low to ACK. The bus value is fed back on uio_in.
    """

    def __init__(self, dut, sda, scl, address):
        self.dut, self.sda, self.scl, self.address = dut, sda, scl, address
        self.transactions = []
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
        state = "idle"
        cur, nbits, byte = [], 0, 0
        while True:
            await RisingEdge(self.dut.clk)
            sda, scl = self._bus()
            self._feed(sda, scl)
            if scl and prev_scl and prev_sda and not sda:
                self.events.append("START")
                state, cur, nbits, byte = "data", [], 0, 0
            elif scl and prev_scl and not prev_sda and sda:
                self.events.append("STOP")
                if cur:
                    self.transactions.append(cur)
                state, cur = "idle", []
            elif state == "data" and scl and not prev_scl:     # rising SCL: sample
                if nbits < 8:
                    byte = (byte << 1) | sda
                    nbits += 1
                else:
                    nbits = 9                                   # ACK clock
            elif state == "data" and not scl and prev_scl:     # falling SCL
                if nbits == 8:
                    cur.append(byte)
                    ack = (cur[0] >> 1) == self.address
                    self.pull_sda = ack
                elif nbits == 9:
                    self.pull_sda = False
                    nbits, byte = 0, 0
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
