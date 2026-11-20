"""Line coder tables built from small state machine descriptions.

A table has 32 entries of 7 bits; entry T[s][b] sits at index 2*s + b with
fields next = e[3:0], out = e[4], xfer = e[5], flag = e[6] (docs/spec.md).

Transmit tables read the next data bit b (without consuming it) and emit one
line symbol; xfer = 1 consumes b. Receive tables read one line symbol and
emit a data bit when xfer = 1. flag = 1 marks a coding error.

Design rule kept by every transmit table here: in a state where both entries
have xfer = 0 (FPEND is true), both entries emit the same symbol and go to
the same next state. The hardware's autopull skips the FIFO in such states and
reads a stale bit from the OSR, so the output must not depend on it.
"""

from dataclasses import dataclass
from typing import Callable, NamedTuple


class Step(NamedTuple):
    """One table entry in terms of machine states rather than indices."""
    next: object
    out: int
    xfer: bool = True
    flag: bool = False


def pack_entry(next_index, out, xfer, flag=False):
    return (next_index & 0xF) | (out & 1) << 4 | int(xfer) << 5 | int(flag) << 6


def unpack_entry(entry):
    """Return (next, out, xfer, flag) of a 7 bit entry."""
    return entry & 0xF, (entry >> 4) & 1, (entry >> 5) & 1, (entry >> 6) & 1


@dataclass
class Machine:
    """A state machine over named states, compiled to a 32 entry table.

    States are numbered in list order. Unused table rows stay 0.
    """
    states: list
    step: Callable

    def __post_init__(self):
        if len(self.states) > 16:
            raise ValueError("a line coder has at most 16 states")

    def index(self, state):
        return self.states.index(state)

    def table(self):
        table = [0] * 32
        for s, state in enumerate(self.states):
            for b in (0, 1):
                st = self.step(state, b)
                table[2 * s + b] = pack_entry(self.index(st.next), st.out, st.xfer, st.flag)
        return table


@dataclass
class LineCode:
    name: str
    doc: str
    tx: list
    tx_init: int
    rx: list
    rx_init: int


# ----- composing transmit tables -----

def compose(first, first_init, second, second_init):
    """Chain two transmit tables: the symbols of `first` become the input bits of `second`.

    `second` must use its input on every step (a one to one code such as NRZI). The result runs both machines in one table, with state (state of first, state of second), and keeps only the pairs reachable from the start. It returns (table, init) and raises ValueError if more than 16 pairs are reachable.
    """
    for entry in second:
        if entry and not unpack_entry(entry)[2]:
            raise ValueError("the second table must use one input bit on every step")
    start = (first_init, second_init)
    index, order, rows = {start: 0}, [start], []
    while len(rows) < len(order):
        s1, s2 = order[len(rows)]
        row = []
        for b in (0, 1):
            n1, o1, x1, f1 = unpack_entry(first[2 * s1 + b])
            n2, o2, _, f2 = unpack_entry(second[2 * s2 + o1])
            nxt = (n1, n2)
            if nxt not in index:
                index[nxt] = len(order)
                order.append(nxt)
            row.append((nxt, o2, x1, f1 | f2))
        rows.append(row)
    if len(order) > 16:
        raise ValueError(f"the composition has {len(order)} reachable states, more than 16")
    table = [0] * 32
    for i, row in enumerate(rows):
        for b, (nxt, out, xfer, flag) in enumerate(row):
            table[2 * i + b] = pack_entry(index[nxt], out, xfer, flag)
    return table, 0


def usb_stuffing():
    """USB bit stuffing on its own: after six 1s, insert a 0 without taking a data bit. 7 states."""
    def step(ones, b):
        if ones == 6:
            return Step(0, 0, xfer=False)
        return Step(ones + 1, 1) if b else Step(0, 0)
    m = Machine(list(range(7)), step)
    return m.table(), m.index(0)


# ----- software runs of a table, with the hardware's peek semantics -----

def run_tx(table, init, bits, flush=True):
    """Encode `bits` with a transmit table. Returns the line symbols.

    With `flush`, steps that need no data (FPEND) after the last bit are run too.
    """
    state, symbols, i = init, [], 0
    while True:
        pending = not ((table[2 * state] >> 5) & 1 or (table[2 * state + 1] >> 5) & 1)
        if i >= len(bits) and not (flush and pending):
            return symbols
        b = bits[i] if i < len(bits) else 0
        state, out, xfer, _ = unpack_entry(table[2 * state + b])
        symbols.append(out)
        i += xfer


def run_rx(table, init, symbols):
    """Decode line symbols with a receive table. Returns (bits, error_flag)."""
    state, bits, err = init, [], False
    for s in symbols:
        state, out, xfer, flag = unpack_entry(table[2 * state + s])
        if xfer:
            bits.append(out)
        err |= bool(flag)
    return bits, err


# ----- the codes -----

def identity():
    """Identity: each data bit is sent as one line symbol, unchanged."""
    m = Machine([0], lambda s, b: Step(0, b))
    t = m.table()
    return LineCode("identity", identity.__doc__, t, 0, t, 0)


def nrzi():
    """NRZI, USB convention: a 0 toggles the line, a 1 keeps it.

    The state is the line level last sent (tx) or seen (rx). Both start at 1.
    """
    tx = Machine([0, 1], lambda level, b: Step(level if b else 1 - level, level if b else 1 - level))
    rx = Machine([0, 1], lambda prev, s: Step(s, int(s == prev)))
    return LineCode("nrzi", nrzi.__doc__, tx.table(), tx.index(1), rx.table(), rx.index(1))


def usb():
    """USB: bit stuffing after six 1s, combined with NRZI (0 = toggle).

    State (level, ones): the line level and the count of 1s since the last 0.
    After six 1s the transmitter sends a stuffed 0 (a toggle) without taking a
    data bit. The receiver drops that stuffed 0 and flags a stuff error if a 1
    arrives where it is expected. 14 states; both start at level 1 (idle J).
    """
    states = [(level, ones) for level in (0, 1) for ones in range(7)]

    def tx_step(state, b):
        level, ones = state
        if ones == 6:
            return Step((1 - level, 0), 1 - level, xfer=False)
        if b:
            return Step((level, ones + 1), level)
        return Step((1 - level, 0), 1 - level)

    def rx_step(state, s):
        prev, ones = state
        b = int(s == prev)
        if ones == 6:
            return Step((s, 0), 0, xfer=False, flag=bool(b))
        return Step((s, ones + 1 if b else 0), b)

    tx, rx = Machine(states, tx_step), Machine(states, rx_step)
    return LineCode("usb", usb.__doc__, tx.table(), tx.index((1, 0)), rx.table(), rx.index((1, 0)))


def can():
    """CAN bit stuffing: after five identical bits insert their complement.

    The line is NRZ. State (value, run) is the last symbol and its run length,
    plus a start state with no history. A stuff bit counts as the first bit of
    the next run, as in ISO 11898. The receiver drops stuff bits and flags a
    stuff error when a sixth identical bit arrives.
    """
    states = ["start"] + [(v, run) for v in (0, 1) for run in range(1, 6)]

    def tx_step(state, b):
        if state != "start" and state[1] == 5:
            v = state[0]
            return Step((1 - v, 1), 1 - v, xfer=False)
        run = state[1] + 1 if state != "start" and state[0] == b else 1
        return Step((b, run), b)

    def rx_step(state, s):
        if state != "start" and state[1] == 5:
            return Step((s, 1), 0, xfer=False, flag=s == state[0])
        run = state[1] + 1 if state != "start" and state[0] == s else 1
        return Step((s, run), s)

    tx, rx = Machine(states, tx_step), Machine(states, rx_step)
    return LineCode("can", can.__doc__, tx.table(), tx.index("start"), rx.table(), rx.index("start"))


def manchester():
    """Manchester, IEEE 802.3 convention: 1 = low then high, 0 = high then low.

    The transmitter takes the bit with the first half symbol and sends the
    second half from state ("second", b), which needs no data. The receiver
    remembers the first half and emits the bit on the second. Two equal halves
    are a code violation: it flags and slips by half a bit to realign.
    """
    tx_states = ["first", ("second", 0), ("second", 1)]

    def tx_step(state, b):
        if state == "first":
            return Step(("second", b), 1 - b)
        return Step("first", state[1], xfer=False)

    rx_states = ["first", ("half", 0), ("half", 1)]

    def rx_step(state, s):
        if state == "first":
            return Step(("half", s), 0, xfer=False)
        if s != state[1]:
            return Step("first", s)
        return Step(("half", s), 0, xfer=False, flag=True)

    tx, rx = Machine(tx_states, tx_step), Machine(rx_states, rx_step)
    return LineCode("manchester", manchester.__doc__, tx.table(), tx.index("first"),
                    rx.table(), rx.index("first"))


def hdlc():
    """HDLC zero insertion: after five 1s insert a 0. The line is NRZ.

    State is the count of 1s since the last 0. The receiver drops the inserted
    0 and flags a sixth 1 (an HDLC flag or abort sequence).
    """
    states = list(range(6))

    def tx_step(ones, b):
        if ones == 5:
            return Step(0, 0, xfer=False)
        return Step(ones + 1 if b else 0, b)

    def rx_step(ones, s):
        if ones == 5:
            return Step(0, 0, xfer=False, flag=bool(s))
        return Step(ones + 1 if s else 0, s)

    tx, rx = Machine(states, tx_step), Machine(states, rx_step)
    return LineCode("hdlc", hdlc.__doc__, tx.table(), 0, rx.table(), 0)


CODES = {code.name: code for code in (identity(), nrzi(), usb(), can(), manchester(), hdlc())}
