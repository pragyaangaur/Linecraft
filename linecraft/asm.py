"""Assembler for Linecraft shuttle programs, with a PIO-like syntax.

    .program uart_tx
    .side_set 1            ; optional; add "pindirs" to drive pe instead of pv
    .define BITS 7
    .wrap_target
        pull block     side 1
        set x, BITS    side 0 [7]
    loop:
        out pins, 1    side 0
        jmp x-- loop   side 0 [6]
    .wrap

Mnemonics, register names and conditions are case-insensitive. Labels and
.define names are case-sensitive. Numbers may be decimal, 0x hex or 0b binary.
Labels resolve to absolute addresses (origin + offset). Numeric jump targets
are taken as absolute addresses too.

Because the hardware applies side-set bits on every instruction, a program
that declares `.side_set N` with N > 0 must give `side` on every instruction.

Run `python -m linecraft.asm file.linecraft` to print the words in hex.
"""

import re
import sys
from dataclasses import dataclass, field

from . import isa


class AsmError(Exception):
    """A syntax or range error, tagged with the source line number."""

    def __init__(self, line, message):
        super().__init__(f"line {line}: {message}")
        self.line = line


@dataclass
class Program:
    words: list
    origin: int = 0
    wrap_bot: int = 0
    wrap_top: int = 0
    labels: dict = field(default_factory=dict)
    side_count: int = 0
    side_pindir: bool = False
    name: str = ""


_DELAY_RE = re.compile(r"\[([^\]]*)\]\s*$")
_SIDE_RE = re.compile(r"\bside\s+(\S+)", re.IGNORECASE)
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _strip_comment(text):
    for mark in (";", "#"):
        pos = text.find(mark)
        if pos >= 0:
            text = text[:pos]
    return text.strip()


class _Assembler:
    def __init__(self):
        self.defines = {}
        self.labels = {}
        self.origin = None
        self.side_count = 0
        self.side_pindir = False
        self.name = ""
        self.lines = []          # (line number, instruction text) in program order
        self.wrap_target = None  # offset of the instruction after .wrap_target
        self.wrap = None         # offset of the instruction before .wrap

    # ----- values -----

    def value(self, token, lineno, what, lo, hi, labels=False):
        """Parse a number, a .define name or (for jump targets) a label."""
        token = token.strip()
        if token in self.defines:
            v = self.defines[token]
        elif labels and token in self.labels:
            v = self.labels[token]
        else:
            try:
                v = int(token, 0)
            except ValueError:
                raise AsmError(lineno, f"bad {what} '{token}'") from None
        if not lo <= v <= hi:
            raise AsmError(lineno, f"{what} {v} out of range {lo} to {hi}")
        return v

    # ----- pass 1: directives, labels, instruction lines -----

    def first_pass(self, text):
        for lineno, raw in enumerate(text.splitlines(), start=1):
            line = _strip_comment(raw)
            while True:
                m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:(.*)$", line)
                if not m:
                    break
                label = m.group(1)
                if label in self.labels:
                    raise AsmError(lineno, f"label '{label}' defined twice")
                self.labels[label] = len(self.lines)   # offset for now
                line = m.group(2).strip()
            if not line:
                continue
            if line.startswith("."):
                self.directive(line, lineno)
            else:
                if self.origin is None:
                    self.origin = 0
                self.lines.append((lineno, line))

        if self.origin is None:
            self.origin = 0
        if not self.lines:
            raise AsmError(1, "program has no instructions")
        if self.wrap_target is not None and self.wrap_target >= len(self.lines):
            raise AsmError(len(text.splitlines()), ".wrap_target has no instruction after it")
        if self.origin + len(self.lines) > isa.IMEM_SIZE:
            raise AsmError(self.lines[-1][0],
                           f"program of {len(self.lines)} words does not fit at origin {self.origin}")
        self.labels = {k: self.origin + v for k, v in self.labels.items()}

    def directive(self, line, lineno):
        parts = line.split()
        name = parts[0].lower()
        args = parts[1:]
        if name == ".program":
            if self.name:
                raise AsmError(lineno, "only one .program per file")
            if len(args) != 1:
                raise AsmError(lineno, ".program needs one name")
            self.name = args[0]
        elif name == ".origin":
            if self.lines:
                raise AsmError(lineno, ".origin must come before the first instruction")
            if len(args) != 1:
                raise AsmError(lineno, ".origin needs one value")
            self.origin = self.value(args[0], lineno, "origin", 0, isa.IMEM_SIZE - 1)
        elif name == ".side_set":
            if self.lines:
                raise AsmError(lineno, ".side_set must come before the first instruction")
            if not 1 <= len(args) <= 2 or (len(args) == 2 and args[1].lower() != "pindirs"):
                raise AsmError(lineno, ".side_set needs a count and optionally 'pindirs'")
            self.side_count = self.value(args[0], lineno, "side-set count", 0, 3)
            self.side_pindir = len(args) == 2
        elif name == ".wrap_target":
            if args or self.wrap_target is not None:
                raise AsmError(lineno, "bad or repeated .wrap_target")
            self.wrap_target = len(self.lines)
        elif name == ".wrap":
            if args or self.wrap is not None:
                raise AsmError(lineno, "bad or repeated .wrap")
            if not self.lines:
                raise AsmError(lineno, ".wrap must follow an instruction")
            self.wrap = len(self.lines) - 1
        elif name == ".define":
            if len(args) != 2 or not _NAME_RE.match(args[0]):
                raise AsmError(lineno, ".define needs a name and a value")
            self.defines[args[0]] = self.value(args[1], lineno, "value", 0, 2**32 - 1)
        else:
            raise AsmError(lineno, f"unknown directive '{parts[0]}'")

    # ----- pass 2: encode -----

    def encode_line(self, lineno, line):
        delay = 0
        m = _DELAY_RE.search(line)
        if m:
            delay = self.value(m.group(1), lineno, "delay", 0, isa.MAX_DELAY)
            line = line[:m.start()].strip()
        side = 0
        m = _SIDE_RE.search(line)
        if m:
            if self.side_count == 0:
                raise AsmError(lineno, "'side' used without .side_set")
            side = self.value(m.group(1), lineno, "side-set value", 0, (1 << self.side_count) - 1)
            line = (line[:m.start()] + line[m.end():]).strip()
        elif self.side_count > 0:
            raise AsmError(lineno, "missing 'side' value (the hardware drives side-set pins on every instruction)")
        if "[" in line or "]" in line:
            raise AsmError(lineno, "delay must be written last, as [n]")

        mnemonic, _, rest = line.partition(" ")
        mnemonic = mnemonic.lower()
        rest = rest.strip()
        handler = getattr(self, "op_" + mnemonic, None)
        if handler is None:
            raise AsmError(lineno, f"unknown instruction '{mnemonic}'")
        op, arg = handler(rest, lineno)
        return isa.encode(op, delay, side, arg)

    @staticmethod
    def operands(rest):
        """Split 'a, b c' into ['a', 'b', 'c'], lowercased."""
        return rest.replace(",", " ").split()

    def lookup(self, table, token, lineno, what):
        key = token.lower()
        if key not in table:
            raise AsmError(lineno, f"bad {what} '{token}'")
        return table[key]

    def op_jmp(self, rest, lineno):
        ops = rest.replace(",", " ").split()
        if len(ops) == 1:
            cond = 0
        elif len(ops) == 2:
            cond = self.lookup(isa.JMP_CONDS, ops[0], lineno, "jmp condition")
        else:
            raise AsmError(lineno, "jmp takes [condition] target")
        target = self.value(ops[-1], lineno, "jump target", 0, isa.IMEM_SIZE - 1, labels=True)
        return isa.OP_JMP, (cond << 8) | target

    def op_wait(self, rest, lineno):
        ops = self.operands(rest)
        if len(ops) != 3:
            raise AsmError(lineno, "wait takes <0|1> pin|gpio|irq <n>")
        polarity = self.value(ops[0], lineno, "wait polarity", 0, 1)
        source = self.lookup(isa.WAIT_SOURCES, ops[1], lineno, "wait source")
        top = 7 if source == isa.WAIT_IRQ else 15
        index = self.value(ops[2], lineno, "wait index", 0, top)
        return isa.OP_WAIT, (polarity << 7) | (source << 5) | index

    def _shift(self, rest, lineno, table, what):
        ops = [o.strip() for o in rest.split(",")]
        if len(ops) != 2:
            raise AsmError(lineno, f"expected <{what}>, <count>")
        reg = self.lookup(table, ops[0], lineno, what)
        count = self.value(ops[1], lineno, "bit count", 1, 32)
        if ops[0].lower() == "fst" and count != 1:
            raise AsmError(lineno, "the line coder moves one bit at a time: count must be 1")
        return reg, count & 0x1F   # 32 is encoded as 0

    def op_in(self, rest, lineno):
        src, count = self._shift(rest, lineno, isa.IN_SOURCES, "source")
        return isa.OP_IN, (src << 8) | count

    def op_out(self, rest, lineno):
        dst, count = self._shift(rest, lineno, isa.OUT_DESTS, "destination")
        return isa.OP_OUT, (dst << 8) | count

    def _fifo(self, rest, lineno, conditional):
        cond, block = 0, 1
        for word in self.operands(rest):
            w = word.lower()
            if w == conditional:
                cond = 1
            elif w == "block":
                block = 1
            elif w == "noblock":
                block = 0
            else:
                raise AsmError(lineno, f"unexpected '{word}'")
        return (cond << 1) | block

    def op_push(self, rest, lineno):
        return isa.OP_PUSH, self._fifo(rest, lineno, "iffull")

    def op_pull(self, rest, lineno):
        return isa.OP_PULL, self._fifo(rest, lineno, "ifempty")

    def op_mov(self, rest, lineno):
        ops = [o.strip() for o in rest.split(",")]
        if len(ops) != 2:
            raise AsmError(lineno, "mov takes <dest>, <source>")
        dst = self.lookup(isa.MOV_DESTS, ops[0], lineno, "mov destination")
        src = ops[1]
        operation = isa.MOV_OP_NONE
        if src.startswith("::"):
            operation, src = isa.MOV_OP_REVERSE, src[2:]
        elif src[:1] in ("!", "~"):
            operation, src = isa.MOV_OP_INVERT, src[1:]
        src = self.lookup(isa.MOV_SOURCES, src.strip(), lineno, "mov source")
        return isa.OP_MOV, (dst << 8) | (operation << 6) | src

    def op_set(self, rest, lineno):
        ops = [o.strip() for o in rest.split(",")]
        if len(ops) != 2:
            raise AsmError(lineno, "set takes <dest>, <value>")
        dst = self.lookup(isa.SET_DESTS, ops[0], lineno, "set destination")
        return isa.OP_SET, (dst << 8) | self.value(ops[1], lineno, "set value", 0, 255)

    def op_irq(self, rest, lineno):
        ops = self.operands(rest)
        clear = 0
        if ops and ops[0].lower() in ("set", "clear"):
            clear = int(ops[0].lower() == "clear")
            ops = ops[1:]
        if len(ops) != 1:
            raise AsmError(lineno, "irq takes [set|clear] <n>")
        return isa.OP_IRQ, (clear << 4) | self.value(ops[0], lineno, "irq index", 0, 7)

    def op_ctl(self, rest, lineno):
        arg = 0
        for word in self.operands(rest):
            arg |= self.lookup({"crc": isa.CTL_CRC, "fst": isa.CTL_FST}, word, lineno, "ctl target")
        if arg == 0:
            raise AsmError(lineno, "ctl takes crc, fst or both")
        return isa.OP_CTL, arg

    def op_nop(self, rest, lineno):
        if rest:
            raise AsmError(lineno, "nop takes no operands")
        return isa.OP_NOP, 0


def assemble(text):
    """Assemble program text into a Program. Raises AsmError on any problem."""
    a = _Assembler()
    a.first_pass(text)
    words = [a.encode_line(n, line) for n, line in a.lines]
    bot = a.wrap_target if a.wrap_target is not None else 0
    top = a.wrap if a.wrap is not None else len(words) - 1
    return Program(
        words=words, origin=a.origin,
        wrap_bot=a.origin + bot, wrap_top=a.origin + top,
        labels=a.labels, side_count=a.side_count, side_pindir=a.side_pindir, name=a.name,
    )


def _name(table, value):
    for k, v in table.items():
        if v == value:
            return k
    return None


def disassemble(word, side_count=0):
    """Turn one word back into assembler text (canonical encodings only).

    Returns None for encodings the assembler cannot produce (reserved bits set
    or undefined register numbers).
    """
    op, delay, side, arg = isa.fields(word)
    text = None
    if op == isa.OP_JMP and not arg & 0xC0:
        cond = _name(isa.JMP_CONDS, arg >> 8)
        text = f"jmp {cond + ' ' if cond else ''}{arg & 0x3F}"
    elif op == isa.OP_WAIT and not arg & 0xF10 and (arg >> 5) & 3 != isa.WAIT_ALWAYS:
        src = _name(isa.WAIT_SOURCES, (arg >> 5) & 3)
        if src != "irq" or not arg & 0x8:
            text = f"wait {arg >> 7} {src} {arg & 0xF}"
    elif op in (isa.OP_IN, isa.OP_OUT) and not arg & 0xC0:
        table = isa.IN_SOURCES if op == isa.OP_IN else isa.OUT_DESTS
        reg = _name(table, arg >> 8)
        count = (arg & 0x3F) or 32
        if reg and count <= 32 and (reg != "fst" or count == 1):
            text = f"{'in' if op == isa.OP_IN else 'out'} {reg}, {count}"
    elif op in (isa.OP_PUSH, isa.OP_PULL) and not arg & 0xFFC:
        cond = ("iffull" if op == isa.OP_PUSH else "ifempty") if arg & 2 else ""
        mode = "block" if arg & 1 else "noblock"
        text = " ".join(filter(None, ["push" if op == isa.OP_PUSH else "pull", cond, mode]))
    elif op == isa.OP_MOV and not arg & 0x30:
        dst = _name(isa.MOV_DESTS, arg >> 8)
        src = _name(isa.MOV_SOURCES, arg & 0xF)
        prefix = {0: "", 1: "!", 2: "::"}.get((arg >> 6) & 3)
        if dst and src and prefix is not None:
            text = f"mov {dst}, {prefix}{src}"
    elif op == isa.OP_SET:
        dst = _name(isa.SET_DESTS, arg >> 8)
        if dst:
            text = f"set {dst}, {arg & 0xFF}"
    elif op == isa.OP_IRQ and not arg & 0xFE8:
        text = f"irq {'clear' if arg & 0x10 else 'set'} {arg & 7}"
    elif op == isa.OP_CTL and 0 < arg <= 3:
        text = "ctl " + " ".join(n for n, b in (("crc", 1), ("fst", 2)) if arg & b)
    elif op == isa.OP_NOP and arg == 0:
        text = "nop"
    if text is None:
        return None
    if side_count:
        if side >> side_count:
            return None
        text += f" side {side}"
    elif side:
        return None
    if delay:
        text += f" [{delay}]"
    return text


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: python -m linecraft.asm file.linecraft", file=sys.stderr)
        return 2
    with open(argv[0]) as f:
        source = f.read()
    try:
        program = assemble(source)
    except AsmError as e:
        print(f"{argv[0]}: {e}", file=sys.stderr)
        return 1
    for word in program.words:
        print(f"{word:06x}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
