# The mathematics behind Linecraft

This page gives the arguments behind the mathematical claims in the README: the clock tolerance bound for a resynchronising receiver, receiver synthesis, address matching with a CRC, and zero drift in the fractional divider. The code that checks each claim is named in its section.

## 1. Line coder tables as automata

A line coder table maps a state q and an input bit b to an entry (q', o, t, f): a next state, an output bit, a transfer bit and a flag. On transmit, b is the next data bit in the OSR, o is the line symbol, and t says whether b was used. If t = 0, the hardware reads the same b again on the next step, because the OSR did not shift. So the true state of a transmitter is a configuration (q, h), where h is either a data bit that has been read but not used, or nothing. From (q, nothing) both values of b are possible. From (q, h) only b = h is possible. This configuration graph is finite, with at most 48 nodes, and every analysis in `linecraft/analysis.py` runs on it.

Each edge of the graph emits exactly one symbol. So paths in the graph are exactly the symbol sequences the transmitter can send, and properties of all possible line signals become path properties of a small graph.

- The longest run R of equal symbols is the longest path in the subgraph of edges whose symbol equals the previous symbol. If that subgraph has a cycle, R is unbounded.
- The worst running digital sum is a longest path with weights of plus and minus one, and it is unbounded exactly when there is a positive cycle (Bellman-Ford).
- The range of symbols per data bit is the minimum and maximum of a ratio over cycles, which is a mean cycle problem (Karp's algorithm on the parametrised weights).

## 2. Clock tolerance of a resynchronising receiver

**Setting.** The receiver's divider ticks k times per symbol. On each edge of the line it restarts, so that its next ticks fall at s, s + 1/k, s + 2/k and so on, measured in symbol times from that edge. The program samples on one of its k ticks per symbol. The transmitter's symbols are longer than the receiver expects by a factor 1 + d. Between two edges there are at most R equal symbols.

**Claim.** With ideal timing, every symbol is sampled exactly once and inside its own symbol time if

```
-(1/k - s) / R  <  d  <  s / (R - 1).
```

**Argument.** Fix an edge and the run of r ≤ R equal symbols that follows it. In receiver symbol times, symbol j of the run (j = 0, ..., r - 1) occupies [j(1 + d), (j + 1)(1 + d)). The receiver's tick positions after the edge are s + i/k for i = 0, 1, 2, and so on, and the sampling instruction runs on every k-th of them, starting at some position p in {s, s + 1/k, ..., s + (k - 1)/k}. The program does not control which, because after a resync the next tick may belong to any of the k instructions in its loop. So sample j sits at p + j, and it lies inside symbol j when

```
j(1 + d) < p + j   and   p + j < (j + 1)(1 + d).
```

The first inequality is j d < p. It can only fail for d > 0, worst at j = r - 1 ≤ R - 1 and at the earliest position p = s, which gives d < s / (R - 1). The second inequality is p - 1 < (j + 1) d. It can only fail for d < 0, worst at j + 1 = r ≤ R and at the latest position p = s + (k - 1)/k, which gives d > -(1/k - s) / R. The next edge restarts the ticks, so the error never builds up past one run, and the same holds for every run.

Real timing costs a little on each side. The edge reaches the divider through a two flop synchronizer, and the transmitter's and the receiver's fractional dividers each jitter by up to a cycle, so with M clock cycles per symbol each margin loses q/M for a q of one or two cycles. The position s is not set by `SYNC_PHASE` alone either. After an edge the first tick comes `SYNC_PHASE` + 3 cycles later, and an `IN` reads pins that are two cycles old, so s = (P + 1)/M for `SYNC_PHASE` = P. The README and `linecraft.analysis` use the simpler and safer form with R on both sides,

```
-(1/k - s - q/M) / R  <  d  <  (s - q/M) / R.
```

**Check.** `experiments/cdr_sweep.py` runs a USB low speed loopback (R = 7, k = 2, M = 33.3) at six sync phases and finds the edges of the working range by detuning the transmitter in 0.1% steps. Every measured edge is between 0.20% and 0.37% outside the safe form with q = 1, on both sides, for every P. On the slow side the exact R - 1 form fits closely: for P = 7 it gives 3.5% with q = 1 and 3.0% with q = 2, and the measured edge is at 3.3%, between the two, as expected when both dividers can each be a cycle off. The first version of this experiment used a symmetric guess and failed on one side, which is how the role of p and of the synchronizer delay showed up.

## 3. Receiver synthesis

**Construction.** `linecraft/synth.py` builds a receiver from a transmit table by the subset construction. A receiver state is a set S of transmitter configurations, starting with {(q0, nothing)}. On a symbol o, collect every edge from a configuration in S whose symbol is o.

- If there are none, no transmitter could have sent o. The receiver raises its flag and returns to the start set.
- If all of them have t = 0, the receiver emits nothing.
- If all of them have t = 1 and use the same bit b, the receiver emits b.
- Otherwise synthesis fails.

The next set is the set of targets of those edges. Partition refinement (Moore's algorithm) then merges states that no symbol sequence can tell apart.

**Why it is correct.** By induction on the number of symbols, S is exactly the set of configurations the transmitter can be in after sending the symbols seen so far. Every transmitter path consistent with the symbols uses the same data bits at the same steps, because the receiver checked at every step that all consistent edges agree. So the bits the receiver emits are the bits the transmitter used, in order and on the same symbol. `check_round_trip` proves this independently by exploring the product of the transmitter and the synthesised receiver.

**When it fails.** It fails exactly when two transmitter paths that agree on every symbol so far use different data bits on the current symbol. Then no receiver that decides on each symbol as it arrives can be correct, so the failure is real and not a weakness of the method. A code like that needs lookahead, which the line coder does not have. The test suite includes such a code, in which each bit is used on a symbol that does not depend on it.

For the six built-in codes, the synthesised receivers have the same number of states as the hand-written ones. For biphase mark code, which was never written by hand, the synthesised receiver runs on the chip.

## 4. Address matching with a CRC

A CRC of width W with a polynomial g that has a constant term maps a W bit message m(x) to m(x) x^W mod g(x). Since g(0) = 1, x is invertible modulo g, so this map is a bijection on messages of W bits or fewer. Two different 8 bit address bytes therefore always leave different values in an 8 bit CRC register, and `JMP CRCOK` with `CRC_CHECK` set to the CRC of the expected byte is an exact equality test. `programs/i2c_target.linecraft` uses this to recognise its own I2C address without a compare instruction.

## 5. Zero drift in the fractional divider

The divider holds a count `cnt` and an 8 bit accumulator `acc`. On a tick it adds `DIV_FRAC` to `acc`, and the next period is N + c cycles, where c is the carry out. Let E be the number of cycles since reset and T the sum of 256N + `DIV_FRAC` over all ticks so far. Then on every cycle

```
256 (E + cnt) + acc = T.
```

At reset all four quantities are 0. On a cycle without a tick, E grows by one and `cnt` falls by one, so the left side does not change. On a tick, `cnt` is 0 before the step and N - 1 + c after it, E grows by one, `acc` changes by `DIV_FRAC` - 256c, and T grows by 256N + `DIV_FRAC`. The left side therefore grows by 256(1 + N - 1 + c) + `DIV_FRAC` - 256c = 256N + `DIV_FRAC`, the same as T.

At the moment of tick k, `cnt` = 0 and T = k(256N + `DIV_FRAC`), so E = (k(256N + `DIV_FRAC`) - `acc`)/256 with 0 ≤ `acc` < 256. That is E = floor(k(N + `DIV_FRAC`/256)), so tick k lands within one cycle of its ideal time for every k, and the error never grows. `formal/run.sh` proves the invariant by induction for every `DIV_INT` and `DIV_FRAC`, and `test_uart_tx_115200_edges` checks it on the RTL at 434.03 cycles per bit.
