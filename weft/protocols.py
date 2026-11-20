"""Ready-made protocol setups.

Each function returns the list of (address, byte) host writes that loads one protocol onto one shuttle: the program at a chosen origin, the configuration, the line coder table and the CRC settings. The writes end with a restart of that shuttle. They can be replayed by the cocotb host driver, by `weft.model.Chip.write_all`, or by the RP2350 driver in host/ after `python -m weft.export` has saved them as JSON.

Pin numbers are Weft pin indices: 0 to 7 are ui_in and uo_out, 8 to 15 are the bidirectional uio pins. Pins 5, 6 and 7 of ui_in and pin 7 of uo_out belong to the host port.
"""

import os

from .asm import assemble
from .config import CRC_RX, CRC_TX, ShuttleConfig, setup_writes
from .crc import CATALOGUE, weft_settings
from . import linecodes

PROGRAMS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "programs")


def program(name, origin=0, defines=None):
    with open(os.path.join(PROGRAMS, name + ".weft")) as f:
        text = f.read()
    for key, value in (defines or {}).items():
        lines = [f".define {key} {value}" if line.strip().startswith(f".define {key} ") else line
                 for line in text.splitlines()]
        text = "\n".join(lines)
    return assemble(f".origin {origin}\n" + text)


def _done(shuttle, cfg, prog):
    return setup_writes(shuttle, cfg, prog) + [(0x002, 1 << shuttle)]


def uart_tx(shuttle, tx_pin, baud, clk_hz, origin=0):
    cfg = ShuttleConfig(out_base=tx_pin, set_base=tx_pin, set_count=1, out_right=True)
    cfg.set_rate(clk_hz, 8 * baud)
    return _done(shuttle, cfg, program("uart_tx", origin))


def uart_tx_parity(shuttle, tx_pin, baud, clk_hz, odd=True, origin=0):
    cfg = ShuttleConfig(out_base=tx_pin, set_base=tx_pin, set_count=1, out_right=True,
                        crc_src=CRC_TX, crc_width=1, crc_poly=1, crc_xorout_ones=odd)
    cfg.set_rate(clk_hz, 8 * baud)
    return _done(shuttle, cfg, program("uart_tx_parity", origin))


def uart_rx(shuttle, rx_pin, baud, clk_hz, origin=0):
    cfg = ShuttleConfig(in_base=rx_pin, jmp_pin=rx_pin, in_right=True)
    cfg.set_rate(clk_hz, 8 * baud)
    return _done(shuttle, cfg, program("uart_rx", origin))


SPI_MODES = {0: (0, 0, 1), 1: (0, 1, 0), 2: (1, 1, 0), 3: (1, 0, 1)}   # IDLE, SHIFT, SAMPLE


def spi_master(shuttle, sck_pin, miso_pin, sck_hz, clk_hz, mode=0, origin=0):
    """MOSI is sck_pin + 1 and CS is sck_pin + 2."""
    idle, shift, sample = SPI_MODES[mode]
    cfg = ShuttleConfig(set_base=sck_pin, set_count=3, side_base=sck_pin, out_base=sck_pin + 1,
                        in_base=miso_pin)
    cfg.set_rate(clk_hz, 4 * sck_hz)
    prog = program("spi_master", origin, {"IDLE": idle, "SHIFT": shift, "SAMPLE": sample})
    return _done(shuttle, cfg, prog)


def spi_target(shuttle, sck_pin, mosi_pin, cs_pin, miso_pin, origin=0):
    cfg = ShuttleConfig(in_base=mosi_pin, out_base=miso_pin, set_base=miso_pin, set_count=1,
                        autopull=True, autopush=True)
    return _done(shuttle, cfg, program("spi_target", origin, {"SCK": sck_pin, "CS": cs_pin}))


def i2c(shuttle, sda_pin, scl_hz, clk_hz, read=False, origin=0):
    """SCL is sda_pin + 1. Both must be bidirectional pins (8 to 15)."""
    if not 8 <= sda_pin <= 14:
        raise ValueError("I2C needs two bidirectional pins, 8 to 15")
    cfg = ShuttleConfig(out_base=sda_pin, set_base=sda_pin, set_count=1, in_base=sda_pin,
                        side_base=sda_pin + 1)
    cfg.set_rate(clk_hz, 4 * scl_hz)
    return _done(shuttle, cfg, program("i2c_read" if read else "i2c_write", origin))


def i2c_target(shuttle, sda_pin, address, origin=0):
    """SCL is sda_pin + 1. The CRC unit matches the address byte (see programs/i2c_target.weft)."""
    from .crc import crc_reference
    if not 8 <= sda_pin <= 14:
        raise ValueError("I2C needs two bidirectional pins, 8 to 15")
    both = (1 << sda_pin) | (1 << (sda_pin + 1))
    cfg = ShuttleConfig(in_base=sda_pin, set_base=sda_pin, set_count=1, jmp_pin=sda_pin + 1,
                        match_mask=both, match_val=both, crc_src=CRC_RX, crc_width=8, crc_poly=0x07,
                        crc_check=crc_reference(CATALOGUE["CRC-8/SMBUS"], bytes([address << 1])))
    return _done(shuttle, cfg, program("i2c_target", origin, {"SCL": sda_pin + 1}))


USB_CRC = weft_settings(CATALOGUE["CRC-16/USB"])


def usb_ls_tx(shuttle, dminus_pin, clk_hz, origin=0):
    """D+ is dminus_pin + 1."""
    cfg = ShuttleConfig(out_base=dminus_pin, set_base=dminus_pin, set_count=2, fst_diff=True,
                        out_right=True, autopull=True, pull_thresh=8)
    cfg.set_rate(clk_hz, 4 * 1.5e6)
    cfg.use_line_code(linecodes.usb(), "tx")
    cfg.use_crc(USB_CRC, CRC_TX)
    return _done(shuttle, cfg, program("usb_ls_tx", origin))


def usb_ls_rx(shuttle, dminus_pin, clk_hz, origin=0):
    """D+ is dminus_pin + 1. The sync phase puts the samples at 1/4 and 3/4 of a bit."""
    cpb = clk_hz / 1.5e6
    cfg = ShuttleConfig(in_base=dminus_pin, in_right=True, autopush=True, push_thresh=8,
                        sync_en=True, sync_pin=dminus_pin, sync_phase=max(round(cpb / 4) - 1, 0),
                        match_mask=0b11 << dminus_pin, match_val=0)
    cfg.set_rate(clk_hz, 2 * 1.5e6)
    cfg.use_line_code(linecodes.usb(), "rx")
    cfg.use_crc(USB_CRC, CRC_RX)
    return _done(shuttle, cfg, program("usb_ls_rx", origin))


def can_tx(shuttle, tx_pin, bitrate, clk_hz, origin=0):
    cfg = ShuttleConfig(out_base=tx_pin, set_base=tx_pin, set_count=1, autopull=True, pull_thresh=8)
    cfg.set_rate(clk_hz, 4 * bitrate)
    cfg.use_line_code(linecodes.can(), "tx")
    cfg.use_crc(weft_settings(CATALOGUE["CRC-15/CAN"]), CRC_TX)
    return _done(shuttle, cfg, program("can_tx", origin))


def dali_tx(shuttle, tx_pin, clk_hz, origin=0):
    """DALI forward frames at 1200 bit/s: push two bytes per frame in one burst."""
    cfg = ShuttleConfig(out_base=tx_pin, set_base=tx_pin, set_count=1, autopull=True, pull_thresh=8)
    cfg.set_rate(clk_hz, 4 * 1200)
    cfg.use_line_code(linecodes.manchester(), "tx")
    return _done(shuttle, cfg, program("dali_tx", origin))


def dshot(shuttle, data_pin, clk_hz, rate=600e3, origin=0):
    """DShot frames: push (value >> 4, (value & 15) << 4) per frame, where value = throttle << 1 | telemetry."""
    cfg = ShuttleConfig(side_base=data_pin, set_base=data_pin, set_count=1, autopull=True, pull_thresh=8,
                        crc_src=CRC_TX, crc_width=4, crc_poly=0x1)
    cfg.set_rate(clk_hz, 16 * rate)
    return _done(shuttle, cfg, program("dshot", origin))


def ws2812(shuttle, data_pin, clk_hz, origin=0):
    cfg = ShuttleConfig(side_base=data_pin, set_base=data_pin, set_count=1, autopull=True, pull_thresh=8)
    cfg.set_rate(clk_hz, 8e6)
    return _done(shuttle, cfg, program("ws2812", origin))


PRESETS = {f.__name__: f for f in (uart_tx, uart_tx_parity, uart_rx, spi_master, spi_target, i2c, i2c_target,
                                    usb_ls_tx, usb_ls_rx, can_tx, dali_tx, dshot, ws2812)}
