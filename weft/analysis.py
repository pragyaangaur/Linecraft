"""Static analysis of line coder tables.

A transmit table is an automaton over bits. Its exact behaviour includes the
hardware's peek rule: a step reads the next data bit b, and if the entry does
not consume it (xfer = 0) the same b is read again on the next step. So the
encoder's configuration is (state, held) where held is the bit already looked
at but not consumed, or None. All graph analyses below run on the graph of
configurations reachable from the initial state, where an edge is one step
(one line symbol) and the adversary picks b whenever held is None.

Run `python -m weft.analysis` for a table of every code in weft.linecodes.
"""

import math
import sys
from collections import deque
from dataclasses import dataclass
from fractions import Fraction

from .linecodes import CODES, unpack_entry

INF = math.inf


# ----- the encoder configuration graph -----

def tx_graph(table, init):
    """Reachable encoder configurations and their edges.

    Returns {config: [(bit, symbol, xfer, next_config), ...]}.
    """
    graph = {}
    todo = [(init, None)]
    while todo:
        config = todo.pop()
        if config in graph:
            continue
        state, held = config
        edges = []
        for b in ((0, 1) if held is None else (held,)):
            nxt, out, xfer, _ = unpack_entry(table[2 * state + b])
            nconfig = (nxt, None if xfer else b)
            edges.append((b, out, xfer, nconfig))
            todo.append(nconfig)
        graph[config] = edges
    return graph


def _edges(graph, weight):
    return [(u, v, weight(sym, xfer)) for u, es in graph.items() for (_, sym, xfer, v) in es]


# ----- (a) round trip -----

@dataclass
class RoundTrip:
    ok: bool
    latency: int | None       # most symbols from the step consuming a bit to its decode
    max_in_flight: int        # most bits consumed by the encoder and not yet decoded
    explored: int             # joint states visited
    counterexample: list | None = None   # input bits that lead to the failure
    reason: str = ""


def check_round_trip(tx, tx_init, rx, rx_init, max_latency=64):
    """Prove decode(encode(s)) == s for every input stream, or find a counterexample.

    The joint state is (encoder state, held bit, decoder state, queue). The
    held bit carries the number of symbols since the encoder first read it,
    and every queued bit the number of symbols since the encoder consumed it.
    Each step the encoder emits one symbol and perhaps consumes the held bit
    into the queue; the decoder reads the symbol and perhaps emits a bit,
    which must equal the head of the queue.

    Failure is any of: a decoder error flag, a wrong bit, a bit emitted before
    the encoder consumed it, or an age above `max_latency` symbols (a held bit
    that is never consumed fails this way too). Since ages are bounded, the
    joint state space is finite and the breadth first search ends. If it ends
    without failure, every bit of every stream comes out correctly, in order,
    within `latency` symbols of being consumed (0 = decoded from the very
    symbol that sent it), which is the round trip property for infinite
    streams.
    """
    start = (tx_init, None, rx_init, ())
    parent = {start: None}
    queue = deque([start])
    latency = 0
    in_flight = 0

    def trace(state, last_bit):
        bits = [] if last_bit is None else [last_bit]
        while parent[state] is not None:
            state, b = parent[state]
            if b is not None:
                bits.append(b)
        return bits[::-1]

    while queue:
        current = queue.popleft()
        enc, held, dec, flight = current
        choices = [(b, (b, 0)) for b in (0, 1)] if held is None else [(None, held)]
        for chosen, (b, held_age) in choices:
            nxt, sym, xfer, _ = unpack_entry(tx[2 * enc + b])
            fl = list(flight)
            new_held = (b, held_age)
            if xfer:
                fl.append((b, 0))      # latency counts from the consuming step
                new_held = None
            dnext, out, emit, flag = unpack_entry(rx[2 * dec + sym])

            def fail(reason):
                return RoundTrip(False, None, in_flight, len(parent), trace(current, chosen), reason)

            if flag:
                return fail("decoder raised its error flag")
            if emit:
                if not fl:
                    return fail("decoder emitted a bit the encoder had not consumed")
                want, bit_age = fl.pop(0)
                if out != want:
                    return fail(f"decoder emitted {out} for a sent {want}")
                latency = max(latency, bit_age)
            in_flight = max(in_flight, len(fl))
            # One symbol passes: everything still waiting gets one step older.
            fl = tuple((v, a + 1) for v, a in fl)
            if new_held is not None:
                new_held = (new_held[0], new_held[1] + 1)
            ages = [a for _, a in fl] + ([new_held[1]] if new_held else [])
            if ages and max(ages) > max_latency:
                return fail(f"a bit waited more than {max_latency} symbols")
            joint = (nxt, new_held, dnext, fl)
            if joint not in parent:
                parent[joint] = (current, chosen)
                queue.append(joint)
    return RoundTrip(True, latency, in_flight, len(parent))


# ----- (b) longest run of identical symbols -----

def max_run_length(table, init):
    """Longest run R of identical consecutive line symbols the encoder can send.

    For each symbol value v, keep only the edges that emit v. A run of v is a
    path in that subgraph, and it may start at any reachable configuration. If
    the subgraph has a cycle the run is unbounded (R = inf). Otherwise R is the
    longest path in the acyclic subgraph. This is the same number as the
    longest path in the graph of (state, last symbol, run length), computed
    without building that larger graph.
    """
    graph = tx_graph(table, init)
    best = 0
    for v in (0, 1):
        succ = {u: [n for (_, sym, _, n) in es if sym == v] for u, es in graph.items()}
        longest = {}
        on_stack = set()

        def visit(u):
            if u in longest:
                return longest[u]
            if u in on_stack:
                raise _Cycle
            on_stack.add(u)
            longest[u] = max((1 + visit(n) for n in succ[u]), default=0)
            on_stack.discard(u)
            return longest[u]

        try:
            best = max(best, max(visit(u) for u in graph))
        except _Cycle:
            return INF
    return best


class _Cycle(Exception):
    pass


# ----- (c) clock tolerance -----

def clock_tolerance(run_length, oversample, q=1.0):
    """Guaranteed tolerance |df/f| between transmitter and receiver clocks.

    Derivation. The receiver ticks every M = `oversample` clock cycles and,
    with SYNC_EN, restarts its bit timer at every edge of the line so that it
    samples near the middle of each symbol. An edge on the wire reaches `pins`
    through the two flop synchronizer and is seen 2 to 3 cycles later, since
    the wire can change anywhere within a clock period. The fixed 2 cycles
    can be absorbed in SYNC_PHASE; what remains, together with rounding
    SYNC_PHASE to whole cycles, puts the first sample within q cycles of the
    true middle of the symbol (q = 1 for even M with a 2-3 cycle synchronizer;
    add 1 if the divider has a fractional part, whose ticks jitter by a cycle).

    Between two edges there are at most R identical symbols, so the k-th
    sample after an edge (k = 1..R) sits at (k - 1/2) symbol times. If the
    clocks differ by a fraction d, sample k drifts by (k - 1/2) * M * d
    cycles. It still falls inside its symbol (a window of M/2 cycles either
    side of the middle) if

        (R - 1/2) * M * |d| + q < M / 2.

    Using R in place of R - 1/2 gives the slightly safer bound returned here:

        |d| < (1/2 - q/M) / R.

    The next edge resynchronises the receiver, so errors do not accumulate.
    With R = inf (no guaranteed edges) no tolerance is guaranteed: 0.
    """
    if run_length == INF or run_length == 0:
        return 0.0
    return max(0.5 - q / oversample, 0.0) / run_length


# ----- (d) running digital sum -----

def _longest_from_anywhere(nodes, edges):
    """Max weight over all paths (any start, any end). INF on a positive cycle.

    Bellman-Ford for longest paths, with every node as a source of weight 0.
    """
    dist = {u: 0 for u in nodes}
    for _ in range(len(nodes)):
        changed = False
        for u, v, w in edges:
            if dist[u] + w > dist[v]:
                dist[v] = dist[u] + w
                changed = True
        if not changed:
            return max(dist.values())
    return INF   # still improving after |V| rounds: a positive cycle exists


def digital_sum_bound(table, init):
    """Largest |running digital sum| over any window of the line, symbols as +-1.

    A window sum is the weight of a path in the configuration graph with
    weight +1 for a 1 symbol and -1 for a 0 symbol. It is bounded exactly when
    no reachable cycle has nonzero weight; Bellman-Ford finds positive cycles,
    and running it on negated weights finds negative ones. Otherwise the
    bound is the larger of the longest path and the longest negated path.
    """
    graph = tx_graph(table, init)
    nodes = list(graph)
    up = _longest_from_anywhere(nodes, _edges(graph, lambda s, x: 1 if s else -1))
    down = _longest_from_anywhere(nodes, _edges(graph, lambda s, x: -1 if s else 1))
    return max(up, down)


# ----- (e) symbols per data bit -----

def min_mean_cycle(nodes, edges):
    """Karp's algorithm: the smallest mean edge weight over all cycles (a Fraction).

    D[k][v] is the least weight of a walk of exactly k edges ending at v,
    starting anywhere (D[0] = 0 everywhere). Then
        min mean = min over v of max over k < n of (D[n][v] - D[k][v]) / (n - k).
    """
    n = len(nodes)
    index = {u: i for i, u in enumerate(nodes)}
    D = [[INF] * n for _ in range(n + 1)]
    D[0] = [0] * n
    for k in range(1, n + 1):
        prev, cur = D[k - 1], D[k]
        for u, v, w in edges:
            iu, iv = index[u], index[v]
            if prev[iu] + w < cur[iv]:
                cur[iv] = prev[iu] + w
    best = None
    for v in range(n):
        if D[n][v] == INF:
            continue
        worst = max(Fraction(D[n][v] - D[k][v], n - k) for k in range(n) if D[k][v] != INF)
        best = worst if best is None else min(best, worst)
    return best


def symbol_rate_bounds(table, init):
    """(min, max) line symbols per data bit over long runs.

    Each edge sends one symbol and consumes xfer data bits. Over a cycle the
    ratio is length / data bits, so its extremes come from the minimum and
    maximum mean of xfer over cycles. A cycle that consumes nothing gives INF.
    """
    graph = tx_graph(table, init)
    nodes = list(graph)
    least = min_mean_cycle(nodes, _edges(graph, lambda s, x: x))
    most = -min_mean_cycle(nodes, _edges(graph, lambda s, x: -x))
    lo = INF if most == 0 else 1 / most
    hi = INF if least == 0 else 1 / least
    return lo, hi


# ----- report -----

def _fmt(value):
    if value == INF:
        return "inf"
    if isinstance(value, Fraction):
        return str(value)
    return str(value)


def report(codes=None, oversamples=(8, 16), q=1.0):
    """The analysis table for the given codes as text."""
    codes = codes or CODES
    head = ["code", "states", "round trip", "R", "max |RDS|", "symbols/bit"] + \
           [f"tol M={m}" for m in oversamples]
    rows = [head]
    for code in codes.values():
        rt = check_round_trip(code.tx, code.tx_init, code.rx, code.rx_init)
        r = max_run_length(code.tx, code.tx_init)
        lo, hi = symbol_rate_bounds(code.tx, code.tx_init)
        used = len({s for s, _ in tx_graph(code.tx, code.tx_init)})
        trip = f"ok, latency {rt.latency}" if rt.ok else f"FAIL: {rt.reason}"
        rows.append([code.name, str(used), trip, _fmt(r),
                     _fmt(digital_sum_bound(code.tx, code.tx_init)),
                     f"{_fmt(lo)} to {_fmt(hi)}"] +
                    [f"{100 * clock_tolerance(r, m, q):.2f}%" for m in oversamples])
    widths = [max(len(row[i]) for row in rows) for i in range(len(head))]
    return "\n".join("  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip() for row in rows)


def main():
    print(report())
    print("\nR: longest run of identical line symbols. max |RDS|: largest running digital")
    print("sum over any window (symbols as +1/-1). tol: guaranteed clock tolerance with")
    print("edge resync, M clock cycles per symbol and q = 1 cycle of sampling uncertainty.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
