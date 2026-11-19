"""Derive a receive table from a transmit table.

A user who adds a new line code only has to write the transmitter. This module builds the matching receiver automatically, minimises it, and checks that it fits in the 16 state line coder. weft.analysis.check_round_trip then proves the pair correct.

Method. The receiver does not know which configuration the transmitter is in, so its state is the set of transmitter configurations that agree with every symbol seen so far (the subset construction for automata). A configuration is (state, held), where held is a data bit the transmitter has looked at but not yet used, as in weft.analysis.tx_graph. When a symbol s arrives, every configuration in the set moves along each of its steps that would have sent s. The receiver can act on the symbol if all those steps agree:

- if none of them used a data bit, it emits nothing;
- if all of them used the same data bit b, it emits b;
- if they disagree, a receiver with no lookahead cannot exist, and synthesis stops with the reason.

A symbol that no configuration could have sent is a line error. The receiver raises its flag and goes back to the start set. After the subset construction, states that behave the same on every symbol sequence are merged by partition refinement (Moore's algorithm), and the start state is placed at index 0.

Run `python -m weft.synth` to synthesise a receiver for every built-in code and compare it with the hand-written one.
"""

import sys
from dataclasses import dataclass

from .analysis import check_round_trip, tx_graph
from .linecodes import CODES, pack_entry


class SynthesisError(Exception):
    pass


@dataclass
class Receiver:
    table: list          # 32 entries for the line coder
    init: int            # FST_INIT for the receiver
    states: int          # states after minimisation
    raw_states: int      # subsets reached before minimisation


def _step(graph, subset, symbol, start):
    """One receiver step: (next subset, emitted bit or None, error flag)."""
    moves = [(b, xfer, nxt) for config in subset for (b, sym, xfer, nxt) in graph[config] if sym == symbol]
    if not moves:
        return start, None, True
    used = {(xfer, b if xfer else None) for b, xfer, _ in moves}
    if len(used) != 1:
        raise SynthesisError(f"after {sorted(subset)} the symbol {symbol} could carry different data, so the receiver needs lookahead")
    xfer, b = used.pop()
    return frozenset(nxt for _, _, nxt in moves), (b if xfer else None), False


def synthesize_rx(tx, tx_init):
    """Build, minimise and check a receiver for the transmit table `tx`."""
    graph = tx_graph(tx, tx_init)
    start = frozenset([(tx_init, None)])
    index = {start: 0}
    order = [start]
    rows = {}
    i = 0
    while i < len(order):
        subset = order[i]
        row = []
        for symbol in (0, 1):
            nxt, bit, flag = _step(graph, subset, symbol, start)
            if nxt not in index:
                index[nxt] = len(order)
                order.append(nxt)
            row.append((index[nxt], bit, flag))
        rows[i] = row
        i += 1
    raw = len(order)

    # Moore minimisation: start with blocks of equal outputs, split until stable.
    def outputs(s):
        return tuple((bit, flag) for _, bit, flag in rows[s])

    block = {s: outputs(s) for s in rows}
    while True:
        signature = {s: (block[s],) + tuple(block[rows[s][sym][0]] for sym in (0, 1)) for s in rows}
        names = {}
        new_block = {s: names.setdefault(signature[s], len(names)) for s in sorted(rows)}
        if len(set(new_block.values())) == len(set(block.values())):
            block = new_block
            break
        block = new_block

    # Renumber so that the start state is 0 and the others follow in first-seen order.
    ids = {}
    for s in sorted(rows):
        ids.setdefault(block[s], len(ids))
    n = len(ids)
    if n > 16:
        raise SynthesisError(f"the receiver needs {n} states, more than the 16 the line coder has")
    table = [0] * 32
    for s in sorted(rows):
        q = ids[block[s]]
        for symbol in (0, 1):
            nxt, bit, flag = rows[s][symbol]
            table[2 * q + symbol] = pack_entry(ids[block[nxt]], bit or 0, bit is not None, flag)
    return Receiver(table, 0, n, raw)


def report(codes=None):
    codes = codes or CODES
    lines = ["code        hand-written rx  synthesised rx  round trip with synthesised rx"]
    for code in codes.values():
        try:
            rx = synthesize_rx(code.tx, code.tx_init)
        except SynthesisError as e:
            lines.append(f"{code.name:<11} -                not possible: {e}")
            continue
        hand = len({e & 0xF for e in code.rx}) if code.rx else 0
        rt = check_round_trip(code.tx, code.tx_init, rx.table, rx.init)
        trip = f"proved, latency {rt.latency}" if rt.ok else f"FAILS: {rt.reason}"
        lines.append(f"{code.name:<11} {_used_states(code.rx, code.rx_init):<16} {rx.states:<15} {trip}")
    return "\n".join(lines)


def _used_states(table, init):
    seen, todo = set(), [init]
    while todo:
        s = todo.pop()
        if s in seen:
            continue
        seen.add(s)
        todo += [table[2 * s + b] & 0xF for b in (0, 1)]
    return len(seen)


def main():
    print(report())
    return 0


if __name__ == "__main__":
    sys.exit(main())
