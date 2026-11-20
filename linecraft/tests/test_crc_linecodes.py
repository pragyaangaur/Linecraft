import random

import pytest

from linecraft.config import ShuttleConfig, config_registers, div_for, setup_writes
from linecraft.crc import (CATALOGUE, CrcSpec, crc_reference, crc_wire_bits, linecraft_register,
                      linecraft_sent_bits, linecraft_settings, wire_bits)
from linecraft.linecodes import CODES, run_rx, run_tx, unpack_entry
from linecraft.model import reverse_bits

NAMES = ["CRC-5/USB", "CRC-16/USB", "CRC-32/ISO-HDLC", "CRC-15/CAN", "CRC-8/SMBUS",
         "CRC-7/MMC", "CRC-16/IBM-3740"]


@pytest.mark.parametrize("name", NAMES)
def test_reference_check_values(name):
    spec = CATALOGUE[name]
    assert crc_reference(spec, b"123456789") == spec.check


@pytest.mark.parametrize("name", NAMES)
def test_linecraft_sends_the_catalogue_crc(name):
    spec = CATALOGUE[name]
    settings = linecraft_settings(spec)
    rng = random.Random(name)
    for length in (0, 1, 9, 30):
        data = bytes(rng.randrange(256) for _ in range(length))
        bits = wire_bits(data, settings.lsb_first)
        sent = linecraft_sent_bits(settings, bits)
        assert sent == crc_wire_bits(spec, crc_reference(spec, data))
        # The receiver's register after message plus CRC is the constant CRC_CHECK.
        assert linecraft_register(settings, bits + sent) == settings.check


@pytest.mark.parametrize("name", NAMES)
def test_check_residue_matches_catalogue(name):
    spec = CATALOGUE[name]
    expected = reverse_bits(spec.residue, spec.width) if spec.refin else spec.residue
    assert linecraft_settings(spec).check == expected


def test_aliases_and_unsupported():
    assert CATALOGUE["CRC-32"] is CATALOGUE["CRC-32/ISO-HDLC"]
    assert CATALOGUE["CRC-16/CCITT-FALSE"].check == 0x29B1
    with pytest.raises(ValueError):
        linecraft_settings(CrcSpec("odd init", 8, 0x07, 0x55, False, False, 0, 0, 0))


# ----- config -----

def test_div_for():
    assert div_for(48e6, 12e6) == (4, 0, 12e6)
    i, f, achieved = div_for(50e6, 115200)
    assert (i, f) == (434, 7)
    assert abs(achieved / 115200 - 1) < 1e-4
    with pytest.raises(ValueError):
        div_for(1e6, 2e6)


def test_config_register_encoding():
    regs = dict(config_registers(ShuttleConfig(div_int=0x1234, pull_thresh=32, crc_width=32,
                                               autopush=True, fst_diff=True, crc_poly=0x04C11DB7)))
    assert (regs[0x00], regs[0x01]) == (0x34, 0x12)
    assert regs[0x0A] == 0 and regs[0x16] == 0          # 32 is written as 0
    assert regs[0x09] == 0b11000
    assert [regs[k] for k in range(0x18, 0x1C)] == [0xB7, 0x1D, 0xC1, 0x04]
    with pytest.raises(ValueError):
        config_registers(ShuttleConfig(set_count=9))
    writes = setup_writes(3, ShuttleConfig(fst_table=[1] * 32))
    assert (0x260, 1) in writes and writes[-1] == (0x002, 8)


# ----- line codes -----

@pytest.mark.parametrize("name", list(CODES))
def test_tables_fit_and_round_trip_in_software(name):
    code = CODES[name]
    for table in (code.tx, code.rx):
        assert len(table) == 32 and all(0 <= e < 128 for e in table)
    rng = random.Random(name)
    bits = [rng.randrange(2) for _ in range(400)] + [1] * 20 + [0] * 20
    decoded, err = run_rx(code.rx, code.rx_init, run_tx(code.tx, code.tx_init, bits))
    assert not err
    assert decoded == bits


@pytest.mark.parametrize("name", list(CODES))
def test_fpend_states_do_not_depend_on_the_stale_bit(name):
    """In states where autopull is skipped, the output must not use the OSR bit."""
    t = CODES[name].tx
    for s in range(16):
        e0, e1 = unpack_entry(t[2 * s]), unpack_entry(t[2 * s + 1])
        if not e0[2] and not e1[2]:
            assert e0 == e1


def test_usb_rx_flags_missing_stuff_bit():
    usb = CODES["usb"]
    line = [1] * 8            # seven 1s after the initial J: no stuffed toggle
    _, err = run_rx(usb.rx, usb.rx_init, line)
    assert err


def test_can_rx_flags_six_identical_bits():
    can = CODES["can"]
    _, err = run_rx(can.rx, can.rx_init, [0] * 6)
    assert err
    _, err = run_rx(can.rx, can.rx_init, [0] * 5 + [1] + [1] * 4)
    assert not err


def test_manchester_waveform():
    m = CODES["manchester"]
    assert run_tx(m.tx, m.tx_init, [1, 0, 0]) == [0, 1, 1, 0, 1, 0]
