import subprocess
import sys
from fractions import Fraction
from pathlib import Path

import pytest

from linecraft.analysis import (INF, check_round_trip, clock_tolerance, digital_sum_bound,
                           max_run_length, min_mean_cycle, symbol_rate_bounds, tx_graph)
from linecraft.linecodes import CODES, Machine, Step, run_rx, run_tx, usb


@pytest.mark.parametrize("name", list(CODES))
def test_every_code_round_trips(name):
    code = CODES[name]
    result = check_round_trip(code.tx, code.tx_init, code.rx, code.rx_init)
    assert result.ok, result.reason


def test_usb_round_trip_details():
    code = CODES["usb"]
    result = check_round_trip(code.tx, code.tx_init, code.rx, code.rx_init)
    assert result.ok and result.latency == 0 and result.max_in_flight == 0


def test_mismatched_pair_gives_a_real_counterexample():
    nrzi, ident = CODES["nrzi"], CODES["identity"]
    result = check_round_trip(nrzi.tx, nrzi.tx_init, ident.rx, ident.rx_init)
    assert not result.ok
    bits = result.counterexample
    decoded, _ = run_rx(ident.rx, ident.rx_init, run_tx(nrzi.tx, nrzi.tx_init, bits))
    assert decoded != bits


def test_stuffing_mismatch_is_caught_by_the_error_flag():
    # A USB receiver fed by a transmitter that stuffs after seven ones.
    states = [(level, ones) for level in (0, 1) for ones in range(8)]

    def late(state, b):
        level, ones = state
        if ones == 7:
            return Step((1 - level, 0), 1 - level, xfer=False)
        return Step((level, ones + 1), level) if b else Step((1 - level, 0), 1 - level)

    m = Machine(states, late)
    code = usb()
    result = check_round_trip(m.table(), m.index((1, 0)), code.rx, code.rx_init)
    assert not result.ok
    assert "error flag" in result.reason
    assert result.counterexample[-7:] == [1] * 7


def test_starving_encoder_fails_on_latency():
    # An encoder that never consumes its bit: every step has xfer = 0.
    m = Machine([0], lambda s, b: Step(0, 0, xfer=False))
    ident = CODES["identity"]
    result = check_round_trip(m.table(), 0, ident.rx, ident.rx_init, max_latency=10)
    assert not result.ok


def test_run_lengths():
    # USB: a 0 bit toggles into a level, six 1s hold it, then the stuffed bit
    # toggles: 1 + 6 = 7 identical symbols.
    assert max_run_length(CODES["usb"].tx, CODES["usb"].tx_init) == 7
    # Manchester: ...01 | 10... puts the two 1 halves of adjacent bits together.
    assert max_run_length(CODES["manchester"].tx, CODES["manchester"].tx_init) == 2
    # CAN: five equal bits, then a complement stuff bit that also starts the next
    # run, so the coded stream never has six. (Six only appears in the unstuffed
    # frame tail and in error frames, outside the line coder.)
    assert max_run_length(CODES["can"].tx, CODES["can"].tx_init) == 5
    # NRZI with all ones and HDLC with all zeros keep the line constant.
    assert max_run_length(CODES["nrzi"].tx, CODES["nrzi"].tx_init) == INF
    assert max_run_length(CODES["hdlc"].tx, CODES["hdlc"].tx_init) == INF


def test_digital_sum():
    assert digital_sum_bound(CODES["manchester"].tx, CODES["manchester"].tx_init) == 2
    assert digital_sum_bound(CODES["usb"].tx, CODES["usb"].tx_init) == INF


def test_symbol_rates():
    assert symbol_rate_bounds(CODES["usb"].tx, CODES["usb"].tx_init) == (1, Fraction(7, 6))
    assert symbol_rate_bounds(CODES["can"].tx, CODES["can"].tx_init) == (1, Fraction(5, 4))
    assert symbol_rate_bounds(CODES["hdlc"].tx, CODES["hdlc"].tx_init) == (1, Fraction(6, 5))
    assert symbol_rate_bounds(CODES["manchester"].tx, CODES["manchester"].tx_init) == (2, 2)


def test_karp_on_a_small_graph():
    edges = [("a", "b", 1), ("b", "a", 3), ("b", "c", 0), ("c", "c", 5)]
    assert min_mean_cycle(["a", "b", "c"], edges) == 2


def test_peek_semantics_in_the_graph():
    # Held bits are part of the configuration: Manchester's second-half step
    # peeks at the next bit without consuming it, so it is held into "first".
    graph = tx_graph(CODES["manchester"].tx, CODES["manchester"].tx_init)
    assert {held for state, held in graph if state == 0} == {None, 0, 1}
    assert all(held is None for state, held in graph if state != 0)


def test_clock_tolerance():
    assert clock_tolerance(7, 8) == pytest.approx((0.5 - 1 / 8) / 7)
    assert clock_tolerance(INF, 16) == 0.0
    assert clock_tolerance(2, 2) == 0.0


def test_cli_prints_a_row_per_code():
    out = subprocess.run([sys.executable, "-m", "linecraft.analysis"], capture_output=True, text=True,
                         check=True, cwd=Path(__file__).parents[2]).stdout
    for name in CODES:
        assert name in out
