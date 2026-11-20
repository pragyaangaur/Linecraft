"""CRC reference code and the mapping from catalogue CRCs to Linecraft settings.

Catalogue CRCs use the Rocksoft parameter model (width, poly, init, refin,
refout, xorout, check, residue), as in the reveng catalogue.

Linecraft's CRC unit is always a normal (MSB first) Galois LFSR fed with bits in
wire order. For a reflected CRC the bytes go out least significant bit first,
and the Linecraft register then holds the reflected register bit-reversed. Because
the catalogue CRCs below all have init and xorout equal to 0 or all ones
(both symmetric under reversal), Linecraft needs only the width and the normal
form polynomial, which is the catalogue `poly` as written.
"""

from dataclasses import dataclass

from .model import crc_advance, reverse_bits


@dataclass(frozen=True)
class CrcSpec:
    name: str
    width: int
    poly: int
    init: int
    refin: bool
    refout: bool
    xorout: int
    check: int      # CRC of the ASCII bytes "123456789"
    residue: int    # register after a message and its CRC, before xorout, reveng convention


CATALOGUE = {
    spec.name: spec
    for spec in [
        CrcSpec("CRC-5/USB", 5, 0x05, 0x1F, True, True, 0x1F, 0x19, 0x06),
        CrcSpec("CRC-16/USB", 16, 0x8005, 0xFFFF, True, True, 0xFFFF, 0xB4C8, 0xB001),
        CrcSpec("CRC-32/ISO-HDLC", 32, 0x04C11DB7, 0xFFFFFFFF, True, True, 0xFFFFFFFF,
                0xCBF43926, 0xDEBB20E3),
        CrcSpec("CRC-15/CAN", 15, 0x4599, 0, False, False, 0, 0x059E, 0),
        CrcSpec("CRC-8/SMBUS", 8, 0x07, 0, False, False, 0, 0xF4, 0),
        CrcSpec("CRC-7/MMC", 7, 0x09, 0, False, False, 0, 0x75, 0),
        CrcSpec("CRC-16/IBM-3740", 16, 0x1021, 0xFFFF, False, False, 0, 0x29B1, 0),
    ]
}
# Common other names.
CATALOGUE["CRC-32"] = CATALOGUE["CRC-32/ISO-HDLC"]
CATALOGUE["CRC-16/CCITT-FALSE"] = CATALOGUE["CRC-16/IBM-3740"]


@dataclass(frozen=True)
class LinecraftCrc:
    """Settings for Linecraft's CRC unit.

    `check` is the value for CRC_CHECK: the raw register after a correct
    message and its CRC have been fed, so that JMP CRCOK tests a received frame.
    `lsb_first` says that the protocol sends each byte least significant bit
    first, so OUT_RIGHT / IN_RIGHT should be 1.
    """
    width: int
    poly: int
    init_ones: bool
    xorout_ones: bool
    check: int
    lsb_first: bool

    @property
    def mask(self):
        return (1 << self.width) - 1


def crc_reference(spec, data):
    """Bitwise Rocksoft CRC of `data` (bytes). Independent of the Linecraft code path."""
    mask = (1 << spec.width) - 1
    top = 1 << (spec.width - 1)
    reg = spec.init
    for byte in data:
        if spec.refin:
            byte = reverse_bits(byte, 8)
        for i in range(7, -1, -1):
            fb = bool(reg & top) ^ bool((byte >> i) & 1)
            reg = (reg << 1) & mask
            if fb:
                reg ^= spec.poly
    if spec.refout:
        reg = reverse_bits(reg, spec.width)
    return reg ^ spec.xorout


def wire_bits(data, lsb_first):
    """Bits of `data` in the order they appear on the wire."""
    order = range(8) if lsb_first else range(7, -1, -1)
    return [(byte >> i) & 1 for byte in data for i in order]


def crc_wire_bits(spec, value):
    """The bits a protocol sends for the CRC `value`: LSB first if refout."""
    order = range(spec.width) if spec.refout else range(spec.width - 1, -1, -1)
    return [(value >> i) & 1 for i in order]


def linecraft_register(settings, bits):
    """Raw Linecraft CRC register after feeding `bits` (wire order) from the init value."""
    init = settings.mask if settings.init_ones else 0
    return crc_advance(init, bits, settings.width, settings.poly) & settings.mask


def linecraft_result(settings, bits):
    """What MOV reads from the CRC: (register XOR xorout), masked."""
    xorout = settings.mask if settings.xorout_ones else 0
    return linecraft_register(settings, bits) ^ xorout


def linecraft_sent_bits(settings, bits):
    """The bits MOV OSR, CRC followed by OUTs sends: r[W-1] first."""
    r = linecraft_result(settings, bits)
    return [(r >> i) & 1 for i in range(settings.width - 1, -1, -1)]


def linecraft_settings(spec):
    """Convert a catalogue CRC into Linecraft settings. Raises ValueError if Linecraft cannot do it."""
    mask = (1 << spec.width) - 1
    if spec.refin != spec.refout:
        raise ValueError(f"{spec.name}: refin != refout is not supported")
    for what, value in (("init", spec.init), ("xorout", spec.xorout)):
        if value not in (0, mask):
            raise ValueError(f"{spec.name}: {what} must be 0 or all ones")
    partial = LinecraftCrc(spec.width, spec.poly, spec.init == mask, spec.xorout == mask, 0, spec.refin)
    # The residue is the same for every correct frame; compute it from an empty one.
    frame = crc_wire_bits(spec, crc_reference(spec, b""))
    return LinecraftCrc(partial.width, partial.poly, partial.init_ones, partial.xorout_ones,
                   linecraft_register(partial, frame), partial.lsb_first)
