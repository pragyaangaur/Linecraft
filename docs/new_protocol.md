# Adding a protocol

This walks through adding a protocol that Linecraft was not designed for, using biphase mark code (the line code of S/PDIF and AES3) as the example. The complete version is `test_biphase_mark_from_a_transmit_rule` in `test/test_protocols.py`.

## 1. Write the transmit rule

A line code is a function from (state, data bit) to a step: the next state, the line symbol, and whether the data bit was used. In biphase mark code every bit starts with a transition, and a 1 has a second transition in the middle. With the state (line level, half) that is six lines.

```python
from linecraft.linecodes import Machine, Step

def tx_step(state, b):
    level, half = state
    if half == 0:                                    # start of a bit: always toggle
        return Step((1 - level, 1), 1 - level, xfer=False)
    return Step((level ^ b, 0), level ^ b)           # middle: toggle for a 1, use the bit

tx = Machine([(l, h) for l in (0, 1) for h in (0, 1)], tx_step)
tx_table, tx_init = tx.table(), tx.index((1, 0))
```

The first half does not use the data bit (`xfer=False`), because its symbol does not depend on it. The hardware then shows the same bit again on the next step, where it is used.

## 2. Let the toolchain derive and check the receiver

```python
from linecraft.synth import synthesize_rx
from linecraft.analysis import check_round_trip, max_run_length

rx = synthesize_rx(tx_table, tx_init)                # 4 states, or a reason why not
assert check_round_trip(tx_table, tx_init, rx.table, rx.init).ok
R = max_run_length(tx_table, tx_init)                # 2 for this code
```

`R` gives the clock tolerance of a receiver that resyncs on edges, from the bound in `docs/theory.md`. If synthesis fails, the code needs lookahead and cannot be decoded one symbol at a time by the line coder.

## 3. Pick or write a program

A transmitter whose timing is all in the line coder needs only one instruction per symbol, as in `programs/bmc_tx.linecraft`.

```
    pull block          ; wait for data before the line moves
.wrap_target
    out fst, 1          ; one symbol per tick
.wrap
```

The receiver `programs/bmc_rx.linecraft` waits for the idle level, then for the first edge, and then runs `in fst, 1` once per tick with the divider resynchronised on every edge.

## 4. Configure and load

```python
from linecraft.config import ShuttleConfig, setup_writes

cfg = ShuttleConfig(out_base=9, set_base=9, set_count=1, out_right=True,
                    autopull=True, pull_thresh=8, fst_table=tx_table, fst_init=tx_init)
cfg.set_ratio(16)                                    # 16 clock cycles per symbol
writes = setup_writes(0, cfg, program)               # host writes for shuttle 0
```

The writes go to the chip through the host port, or into the model with `linecraft.model.Chip.write_all` for a quick check before using real hardware. `linecraft/protocols.py` collects ready-made setups for the protocols in `programs/`.
