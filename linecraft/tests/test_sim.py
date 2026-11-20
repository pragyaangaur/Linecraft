"""The VCD export of linecraft.sim."""

from linecraft import protocols
from linecraft.sim import run, write_vcd


def test_uart_waveform_has_a_start_bit_and_a_vcd(tmp_path):
    samples = run(protocols.uart_tx(0, 1, 1e6, 8e6), 0, [0x55], 200)
    tx = [(uo >> 1) & 1 for uo, _, _, _ in samples]
    assert 0 in tx and tx[-1] == 1
    path = tmp_path / "u.vcd"
    write_vcd(path, samples, 8e6)
    text = path.read_text()
    assert "$enddefinitions $end" in text and "pin1" in text
