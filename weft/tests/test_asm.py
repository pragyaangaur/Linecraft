import random
import subprocess
import sys
from pathlib import Path

import pytest

from weft import isa
from weft.asm import AsmError, assemble, disassemble


def test_basic_program():
    p = assemble("""
        .program blink      ; a comment
        .origin 4
        .define HALF 3
        start:  set pins, 1 [HALF]
                set pins, 0 [HALF]   # another comment
                jmp start
    """)
    assert p.name == "blink"
    assert p.origin == 4
    assert p.labels == {"start": 4}
    assert p.words == [
        isa.encode(isa.OP_SET, 3, 0, 0x001),
        isa.encode(isa.OP_SET, 3, 0, 0x000),
        isa.encode(isa.OP_JMP, 0, 0, 4),
    ]
    assert (p.wrap_bot, p.wrap_top) == (4, 6)


def test_wrap_and_side_set():
    p = assemble("""
        .side_set 2 pindirs
            nop side 1
        .wrap_target
        top: out pins, 1 side 3 [2]
            jmp x-- top side 0
        .wrap
            nop side 2
    """)
    assert (p.wrap_bot, p.wrap_top) == (1, 2)
    assert p.side_count == 2 and p.side_pindir
    assert p.words[1] == isa.encode(isa.OP_OUT, 2, 3, 0x001)
    assert p.words[2] == isa.encode(isa.OP_JMP, 0, 0, (2 << 8) | 1)


@pytest.mark.parametrize("text, word", [
    ("jmp 5", isa.encode(0, arg=5)),
    ("jmp !osre, 7", isa.encode(0, arg=(8 << 8) | 7)),
    ("JMP MORE 1", isa.encode(0, arg=(5 << 8) | 1)),
    ("wait 1 pin 3", isa.encode(1, arg=0x80 | (1 << 5) | 3)),
    ("wait 0 gpio 12", isa.encode(1, arg=12)),
    ("wait 1 irq 7", isa.encode(1, arg=0x80 | (2 << 5) | 7)),
    ("in pins, 32", isa.encode(2, arg=0)),
    ("in fst, 1", isa.encode(2, arg=(6 << 8) | 1)),
    ("out pindirs, 4", isa.encode(3, arg=(4 << 8) | 4)),
    ("push", isa.encode(4, arg=1)),
    ("push iffull noblock", isa.encode(4, arg=2)),
    ("pull ifempty block", isa.encode(5, arg=3)),
    ("mov x, !y", isa.encode(6, arg=(0 << 8) | (1 << 6) | 2)),
    ("mov isr, ~status", isa.encode(6, arg=(2 << 8) | (1 << 6) | 7)),
    ("mov osr, ::pins", isa.encode(6, arg=(3 << 8) | (2 << 6) | 0)),
    ("mov osr, crc", isa.encode(6, arg=(3 << 8) | 6)),
    ("set pindirs, 0b101", isa.encode(7, arg=(4 << 8) | 5)),
    ("set y, 0xff", isa.encode(7, arg=(2 << 8) | 255)),
    ("irq 3", isa.encode(8, arg=3)),
    ("irq clear 3", isa.encode(8, arg=0x13)),
    ("ctl crc fst", isa.encode(9, arg=3)),
    ("ctl fst", isa.encode(9, arg=2)),
    ("nop [31]", isa.encode(10, delay=31)),
])
def test_encodings(text, word):
    assert assemble(text).words == [word]


def _random_canonical(rng, side_count):
    """A random instruction in the assembler's canonical form."""
    choice = rng.randrange(11)
    if choice == 0:
        arg = (rng.randrange(16) << 8) | rng.randrange(64)
    elif choice == 1:
        src = rng.randrange(3)
        arg = (rng.randrange(2) << 7) | (src << 5) | rng.randrange(8 if src == 2 else 16)
    elif choice in (2, 3):
        reg = rng.randrange(7 if choice == 2 else 8)
        fst = 6 if choice == 2 else 7
        count = 1 if reg == fst else rng.randrange(32)
        arg = (reg << 8) | count
    elif choice in (4, 5):
        arg = rng.randrange(4)
    elif choice == 6:
        arg = (rng.randrange(5) << 8) | (rng.randrange(3) << 6) | rng.randrange(8)
    elif choice == 7:
        arg = (rng.choice([0, 1, 2, 4]) << 8) | rng.randrange(256)
    elif choice == 8:
        arg = (rng.randrange(2) << 4) | rng.randrange(8)
    elif choice == 9:
        arg = rng.randrange(1, 4)
    else:
        arg = 0
    side = rng.randrange(1 << side_count)
    return isa.encode(choice, rng.randrange(32), side, arg)


@pytest.mark.parametrize("side_count", [0, 1, 3])
def test_disassemble_round_trip(side_count):
    rng = random.Random(side_count)
    header = f".side_set {side_count}\n" if side_count else ""
    for _ in range(500):
        word = _random_canonical(rng, side_count)
        text = disassemble(word, side_count)
        assert text is not None, hex(word)
        assert assemble(header + text).words == [word], text


@pytest.mark.parametrize("source, message", [
    ("jmp nowhere", "bad jump target"),
    ("jmp 64", "out of range"),
    ("jmp sometimes 3", "bad jmp condition"),
    ("in fst, 2", "count must be 1"),
    ("out fst, 8", "count must be 1"),
    ("out pins, 33", "out of range"),
    ("out pins, 0", "out of range"),
    ("set x, 256", "out of range"),
    ("set pc, 1", "bad set destination"),
    ("nop [32]", "delay 32 out of range"),
    (".side_set 1\nnop side 2", "side-set value 2 out of range"),
    (".side_set 1\nnop", "missing 'side'"),
    ("nop side 1", "without .side_set"),
    ("wait 2 pin 1", "out of range"),
    ("wait 1 irq 8", "out of range"),
    ("irq 9", "out of range"),
    ("mov x, crc, y", "mov takes"),
    ("mov pins, x", "bad mov destination"),
    ("push maybe", "unexpected"),
    ("frob x", "unknown instruction"),
    (".bogus", "unknown directive"),
    ("a:\na: nop", "defined twice"),
    (".origin 63\nnop\nnop", "does not fit"),
    ("nop\n.side_set 1", "before the first instruction"),
])
def test_errors(source, message):
    with pytest.raises(AsmError) as err:
        assemble(source)
    assert message in str(err.value)


def test_error_line_number():
    with pytest.raises(AsmError, match="line 3"):
        assemble("nop\n\nset x, 999\n")


def test_cli(tmp_path):
    src = tmp_path / "t.weft"
    src.write_text("set x, 1\njmp 0\n")
    out = subprocess.run([sys.executable, "-m", "weft.asm", str(src)],
                         capture_output=True, text=True, check=True,
                         cwd=Path(__file__).parents[2]).stdout
    assert out.split() == ["700101", "000000"]
