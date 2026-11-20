"""Turn a high-level shuttle configuration plus a program into host writes.

    cfg = ShuttleConfig(out_base=0, set_count=1, out_right=True)
    cfg.set_rate(clk_hz=50e6, rate_hz=115200)
    writes = setup_writes(0, cfg, program)
    chip.write_all(writes)

Fields left as None are taken from the program (wrap, start, side-set).
Every value is checked against its register width.
"""

from dataclasses import dataclass, field

CRC_NONE = 0
CRC_TX = 1     # feed data bits leaving through OUT
CRC_RX = 2     # feed data bits entering through IN


def div_for(freq_clk, rate):
    """Divider for `rate` ticks per second from a `freq_clk` clock.

    Returns (div_int, div_frac, achieved_rate). The tick period is
    div_int + div_frac / 256 cycles on average.
    """
    ratio = freq_clk / rate
    if ratio < 1:
        raise ValueError("rate is above the clock frequency")
    steps = round(ratio * 256)
    div_int, div_frac = divmod(steps, 256)
    if div_int > 0xFFFF:
        raise ValueError("rate too low for a 16 bit divider")
    return div_int, div_frac, freq_clk / (div_int + div_frac / 256)


@dataclass
class ShuttleConfig:
    div_int: int = 1
    div_frac: int = 0
    in_base: int = 0
    out_base: int = 0
    set_base: int = 0
    set_count: int = 0
    side_base: int = 0
    side_count: int | None = None
    side_pindir: bool | None = None
    out_right: bool = False
    in_right: bool = False
    autopull: bool = False
    autopush: bool = False
    fst_diff: bool = False
    pull_thresh: int = 8
    push_thresh: int = 8
    wrap_bot: int | None = None
    wrap_top: int | None = None
    start_pc: int | None = None
    jmp_pin: int = 0
    sync_en: bool = False
    sync_pin: int = 0
    sync_phase: int = 0
    fst_init: int = 0
    fst_table: list | None = None
    crc_src: int = CRC_NONE
    crc_init_ones: bool = False
    crc_xorout_ones: bool = False
    crc_width: int = 32
    crc_poly: int = 0
    crc_check: int = 0
    match_mask: int = 0
    match_val: int = 0
    extra: dict = field(default_factory=dict)   # raw {k: byte} overrides, written last

    def set_rate(self, clk_hz, rate_hz):
        """Set the divider for `rate_hz` ticks per second. Returns the achieved rate."""
        self.div_int, self.div_frac, achieved = div_for(clk_hz, rate_hz)
        return achieved

    def set_ratio(self, cycles_per_tick):
        """Set the divider from a (possibly fractional) number of cycles per tick."""
        self.div_int, self.div_frac, _ = div_for(cycles_per_tick, 1)

    def use_crc(self, settings, src):
        """Copy a linecraft.crc.LinecraftCrc into the CRC fields."""
        self.crc_src = src
        self.crc_width = settings.width
        self.crc_poly = settings.poly
        self.crc_init_ones = settings.init_ones
        self.crc_xorout_ones = settings.xorout_ones
        self.crc_check = settings.check

    def use_line_code(self, code, direction):
        """Load a linecraft.linecodes.LineCode table: direction is 'tx' or 'rx'."""
        self.fst_table = list(getattr(code, direction))
        self.fst_init = getattr(code, direction + "_init")


def _check(name, value, lo, hi):
    if not lo <= int(value) <= hi:
        raise ValueError(f"{name} = {value} is outside {lo} to {hi}")
    return int(value)


def config_registers(cfg, program=None):
    """The configuration as an ordered list of (k, byte) register writes."""
    side_count = cfg.side_count if cfg.side_count is not None else (program.side_count if program else 0)
    side_pindir = cfg.side_pindir if cfg.side_pindir is not None else (program.side_pindir if program else False)
    if program is not None and side_count != program.side_count:
        raise ValueError("side_count disagrees with the program's .side_set")

    def pick(value, attr):
        if value is not None:
            return value
        return getattr(program, attr) if program is not None else 0

    regs = []

    def put(k, value, bits, name):
        value = _check(name, value, 0, (1 << bits) - 1)
        for i in range(0, bits, 8):
            regs.append((k + i // 8, (value >> i) & 0xFF))

    put(0x00, cfg.div_int, 16, "div_int")
    put(0x02, cfg.div_frac, 8, "div_frac")
    put(0x03, cfg.in_base, 4, "in_base")
    put(0x04, cfg.out_base, 4, "out_base")
    put(0x05, cfg.set_base, 4, "set_base")
    put(0x06, _check("set_count", cfg.set_count, 0, 8), 4, "set_count")
    put(0x07, cfg.side_base, 4, "side_base")
    put(0x08, _check("side_count", side_count, 0, 3) | int(side_pindir) << 2, 3, "side")
    put(0x09, int(cfg.out_right) | int(cfg.in_right) << 1 | int(cfg.autopull) << 2
        | int(cfg.autopush) << 3 | int(cfg.fst_diff) << 4, 5, "shift flags")
    # Thresholds and the CRC width use the documented encoding of 32 as 0.
    put(0x0A, _check("pull_thresh", cfg.pull_thresh, 1, 32) & 31, 6, "pull_thresh")
    put(0x0B, _check("push_thresh", cfg.push_thresh, 1, 32) & 31, 6, "push_thresh")
    put(0x0C, pick(cfg.wrap_bot, "wrap_bot"), 6, "wrap_bot")
    put(0x0D, pick(cfg.wrap_top, "wrap_top"), 6, "wrap_top")
    put(0x0E, pick(cfg.start_pc, "origin"), 6, "start_pc")
    put(0x0F, cfg.jmp_pin, 4, "jmp_pin")
    put(0x10, int(cfg.sync_en), 1, "sync_en")
    put(0x11, cfg.sync_pin, 4, "sync_pin")
    put(0x12, cfg.sync_phase, 16, "sync_phase")
    put(0x14, cfg.fst_init, 4, "fst_init")
    put(0x15, _check("crc_src", cfg.crc_src, 0, 2) | int(cfg.crc_init_ones) << 2
        | int(cfg.crc_xorout_ones) << 3, 4, "crc flags")
    put(0x16, _check("crc_width", cfg.crc_width, 1, 32) & 31, 6, "crc_width")
    put(0x18, cfg.crc_poly, 32, "crc_poly")
    put(0x1C, cfg.crc_check, 32, "crc_check")
    put(0x20, cfg.match_mask, 16, "match_mask")
    put(0x22, cfg.match_val, 16, "match_val")
    for k, v in cfg.extra.items():
        regs.append((k, v))
    return regs


def program_writes(program):
    """Instruction memory writes: bytes 0, 1, 2 of each word (byte 2 commits)."""
    writes = []
    for i, word in enumerate(program.words):
        base = 0x400 + 4 * (program.origin + i)
        writes += [(base, word & 0xFF), (base + 1, (word >> 8) & 0xFF), (base + 2, word >> 16)]
    return writes


def setup_writes(shuttle, cfg, program=None):
    """All host writes that set up `shuttle`: configuration, line coder table, program.

    The list ends with a RESTART of the shuttle, because pc, fst and crc only
    take START_PC, FST_INIT and the CRC init value on a restart or reset.
    """
    base = 0x100 + 0x40 * shuttle
    writes = [(base + k, v) for k, v in config_registers(cfg, program)]
    if cfg.fst_table is not None:
        if len(cfg.fst_table) != 32:
            raise ValueError("a line coder table has 32 entries")
        writes += [(0x200 + 0x20 * shuttle + j, _check("table entry", e, 0, 127))
                   for j, e in enumerate(cfg.fst_table)]
    if program is not None:
        writes += program_writes(program)
    writes.append((0x002, 1 << shuttle))
    return writes
