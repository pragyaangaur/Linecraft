"""Instruction set constants shared by the model, the assembler and the disassembler.

Every number here comes from the "Instruction format" section of docs/spec.md.
"""

OP_JMP = 0
OP_WAIT = 1
OP_IN = 2
OP_OUT = 3
OP_PUSH = 4
OP_PULL = 5
OP_MOV = 6
OP_SET = 7
OP_IRQ = 8
OP_CTL = 9
OP_NOP = 10  # opcodes 10 to 15 all behave as NOP; the assembler emits 10

# JMP conditions, bits [11:8]. The empty string is "always".
JMP_CONDS = {
    "": 0, "!x": 1, "x--": 2, "!y": 3, "y--": 4, "more": 5, "pin": 6, "!pin": 7,
    "!osre": 8, "txne": 9, "match": 10, "!match": 11, "crcok": 12, "ferr": 13,
    "fpend": 14, "rxnf": 15,
}

# WAIT sources, bits [6:5]. "pin" is relative to IN_BASE, "gpio" is absolute.
WAIT_GPIO = 0
WAIT_PIN = 1
WAIT_IRQ = 2
WAIT_ALWAYS = 3
WAIT_SOURCES = {"gpio": WAIT_GPIO, "pin": WAIT_PIN, "irq": WAIT_IRQ}

IN_SOURCES = {"pins": 0, "x": 1, "y": 2, "null": 3, "isr": 4, "osr": 5, "fst": 6}
IN_FST = 6

OUT_DESTS = {"pins": 0, "x": 1, "y": 2, "null": 3, "pindirs": 4, "pc": 5, "isr": 6, "fst": 7}
OUT_FST = 7

MOV_DESTS = {"x": 0, "y": 1, "isr": 2, "osr": 3, "pc": 4}
MOV_SOURCES = {"pins": 0, "x": 1, "y": 2, "null": 3, "isr": 4, "osr": 5, "crc": 6, "status": 7}
MOV_OP_NONE = 0
MOV_OP_INVERT = 1
MOV_OP_REVERSE = 2

SET_DESTS = {"pins": 0, "x": 1, "y": 2, "pindirs": 4}

CTL_CRC = 1
CTL_FST = 2

MAX_DELAY = 31
IMEM_SIZE = 64


def encode(op, delay=0, side=0, arg=0):
    """Pack the four instruction fields into a 24 bit word."""
    return (op << 20) | (delay << 15) | (side << 12) | arg


def fields(word):
    """Split a 24 bit word into (opcode, delay, side, arguments)."""
    return (word >> 20) & 0xF, (word >> 15) & 0x1F, (word >> 12) & 0x7, word & 0xFFF


def count_of(field):
    """Decode a 6 bit count field: 0 means 32.

    Values 33 to 63 are not described by the spec; they are clamped to 32.
    """
    n = field & 0x3F
    return 32 if n == 0 else min(n, 32)
