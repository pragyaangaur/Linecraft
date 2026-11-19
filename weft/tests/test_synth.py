"""Tests for receiver synthesis from a transmit table."""

import random

import pytest

from weft.analysis import check_round_trip
from weft.linecodes import CODES, pack_entry, run_rx, run_tx
from weft.synth import SynthesisError, synthesize_rx, _used_states


@pytest.mark.parametrize("name", sorted(CODES))
def test_synthesised_receiver_round_trips(name):
    code = CODES[name]
    rx = synthesize_rx(code.tx, code.tx_init)
    assert check_round_trip(code.tx, code.tx_init, rx.table, rx.init).ok


@pytest.mark.parametrize("name", sorted(CODES))
def test_synthesised_receiver_is_as_small_as_the_hand_written_one(name):
    code = CODES[name]
    rx = synthesize_rx(code.tx, code.tx_init)
    assert rx.states <= _used_states(code.rx, code.rx_init)


@pytest.mark.parametrize("name", sorted(CODES))
def test_synthesised_receiver_decodes_random_streams(name):
    code = CODES[name]
    rx = synthesize_rx(code.tx, code.tx_init)
    rng = random.Random(7)
    for _ in range(50):
        bits = [rng.randrange(2) for _ in range(rng.randrange(1, 200))]
        symbols = run_tx(code.tx, code.tx_init, bits)
        got, err = run_rx(rx.table, rx.init, symbols)
        assert not err
        assert got == bits


def test_usb_receiver_flags_a_missing_stuff_bit():
    code = CODES["usb"]
    rx = synthesize_rx(code.tx, code.tx_init)
    # From idle J, six 1s keep the line at J; a seventh J where the stuffed
    # toggle should be is impossible for a transmitter and must raise the flag.
    _, err = run_rx(rx.table, rx.init, [1] * 8)
    assert err


def test_a_code_that_needs_lookahead_is_rejected():
    # Each bit b is sent as the symbols 0 then b, but the transmitter uses the
    # bit on the first symbol, so a receiver would have to emit b before it
    # has seen it.
    table = [0] * 32
    table[0] = pack_entry(1, 0, True)       # state 0, b = 0: send 0, use the bit
    table[1] = pack_entry(2, 0, True)       # state 0, b = 1: send 0, use the bit
    table[2] = table[3] = pack_entry(0, 0, False)   # state 1: send 0
    table[4] = table[5] = pack_entry(0, 1, False)   # state 2: send 1
    with pytest.raises(SynthesisError):
        synthesize_rx(table, 0)
