"""The protocol presets build, and two of them run end to end in the model."""

import json

from weft import protocols
from weft.export import main as export_main
from weft.model import Chip
from weft.crc import CATALOGUE, crc_reference

CLK = 50e6


def test_every_preset_builds():
    builds = {
        "uart_tx": dict(shuttle=0, tx_pin=1, baud=115200, clk_hz=CLK),
        "uart_tx_parity": dict(shuttle=0, tx_pin=1, baud=115200, clk_hz=CLK),
        "uart_rx": dict(shuttle=1, rx_pin=2, baud=115200, clk_hz=CLK),
        "spi_master": dict(shuttle=0, sck_pin=2, miso_pin=0, sck_hz=1e6, clk_hz=CLK, mode=3),
        "spi_target": dict(shuttle=0, sck_pin=1, mosi_pin=2, cs_pin=3, miso_pin=0),
        "i2c": dict(shuttle=2, sda_pin=8, scl_hz=400e3, clk_hz=CLK, read=True),
        "i2c_target": dict(shuttle=1, sda_pin=10, address=0x42),
        "usb_ls_tx": dict(shuttle=0, dminus_pin=8, clk_hz=CLK),
        "usb_ls_rx": dict(shuttle=1, dminus_pin=8, clk_hz=CLK, origin=32),
        "can_tx": dict(shuttle=3, tx_pin=4, bitrate=500e3, clk_hz=CLK),
        "dali_tx": dict(shuttle=2, tx_pin=1, clk_hz=CLK),
        "dshot": dict(shuttle=1, data_pin=2, clk_hz=CLK),
        "ws2812": dict(shuttle=0, data_pin=5, clk_hz=CLK),
    }
    assert set(builds) == set(protocols.PRESETS)
    for name, kwargs in builds.items():
        writes = protocols.PRESETS[name](**kwargs)
        assert writes[-1] == (0x002, 1 << kwargs["shuttle"])
        assert all(0 <= a < 0x8000 and 0 <= b < 256 for a, b in writes)


def test_uart_preset_sends_a_frame_in_the_model():
    chip = Chip()
    chip.write_all(protocols.uart_tx(0, tx_pin=1, baud=CLK / 16, clk_hz=CLK))
    chip.write(0x001, 1)
    chip.write(0x008, 0xA5)
    line = [(chip.step(0x80, 0)[0] >> 1) & 1 for _ in range(400)]
    start = line.index(0, 4)
    bits = [line[start + 8 + 16 * k] for k in range(10)]
    assert bits[0] == 0 and bits[9] == 1
    assert sum(b << i for i, b in enumerate(bits[1:9])) == 0xA5


def test_usb_presets_loop_back_in_the_model():
    chip = Chip()
    chip.write_all(protocols.usb_ls_tx(0, 8, CLK) + protocols.usb_ls_rx(1, 8, CLK, origin=32))
    chip.write(0x001, 0x3)
    payload = [0xC3, 0x12, 0xFF, 0xFF]
    queue, got, out = [0x80] + payload, [], (0, 0, 0)
    for t in range(6000):
        # Give the receiver time to arm before the packet starts.
        if t > 300 and queue and chip.read(0x005) & 1:
            chip.write(0x008, queue.pop(0))
        while (chip.read(0x006) >> 4) & 7:
            got.append(chip.read(0x009))
        _, uio, oe = out
        dm = (uio & 1) if oe & 1 else 1
        dp = ((uio >> 1) & 1) if oe & 2 else 0
        out = chip.step(0x80, dm | (dp << 1))
    crc = crc_reference(CATALOGUE["CRC-16/USB"], bytes(payload[1:]))
    assert got == [0x80] + payload + [crc & 0xFF, crc >> 8]
    assert chip.irq & 3 == 1


def test_export_writes_json(capsys):
    assert export_main(["ws2812", "shuttle=0", "data_pin=5", "clk_hz=50e6"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["preset"] == "ws2812" and data["writes"][-1] == [2, 1]
