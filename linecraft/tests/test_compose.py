"""Composition of transmit tables."""

import pytest

from linecraft import linecodes
from linecraft.analysis import check_round_trip, max_run_length, same_transmitter
from linecraft.synth import synthesize_rx


def used_states(table):
    return sum(1 for s in range(16) if table[2 * s] or table[2 * s + 1])


def test_usb_is_stuffing_then_nrzi():
    stuff, stuff_init = linecodes.usb_stuffing()
    nrzi = linecodes.nrzi()
    table, init = linecodes.compose(stuff, stuff_init, nrzi.tx, nrzi.tx_init)
    usb = linecodes.usb()
    assert used_states(table) == 14
    assert same_transmitter(table, init, usb.tx, usb.tx_init)
    rx = synthesize_rx(table, init)
    assert check_round_trip(table, init, rx.table, rx.init).ok


def test_hdlc_then_nrzi_bounds_the_run_length():
    # Each code alone allows unbounded runs: NRZI holds the line for a run of 1s,
    # and HDLC zero insertion alone passes a long run of 0s straight through.
    nrzi, hdlc = linecodes.nrzi(), linecodes.hdlc()
    assert max_run_length(nrzi.tx, nrzi.tx_init) == float("inf")
    assert max_run_length(hdlc.tx, hdlc.tx_init) == float("inf")
    table, init = linecodes.compose(hdlc.tx, hdlc.tx_init, nrzi.tx, nrzi.tx_init)
    assert max_run_length(table, init) == 6
    rx = synthesize_rx(table, init)
    assert check_round_trip(table, init, rx.table, rx.init).ok


def test_compose_rejects_a_second_code_that_skips_input():
    man = linecodes.manchester()
    nrzi = linecodes.nrzi()
    with pytest.raises(ValueError):
        linecodes.compose(nrzi.tx, nrzi.tx_init, man.tx, man.tx_init)


def test_same_transmitter_sees_a_difference():
    usb, nrzi = linecodes.usb(), linecodes.nrzi()
    assert not same_transmitter(usb.tx, usb.tx_init, nrzi.tx, nrzi.tx_init)
