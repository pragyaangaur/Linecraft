"""Draw the README figures from the cycle accurate model and the toolchain.

Every waveform here comes from `linecraft.model.Chip`, which the lockstep tests hold equal to the RTL on every pin and every cycle. Nothing is drawn by hand.

Run: python3 docs/make_figures.py
"""

import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt                          # noqa: E402
from matplotlib.patches import FancyBboxPatch            # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from linecraft import linecodes, protocols                   # noqa: E402
from linecraft.model import Chip                              # noqa: E402

OUT = os.path.join(ROOT, "docs", "img")
CLK = 40e6
CSN_HIGH = 0x80

INK = "#1f2933"
MUTED = "#6b7785"
COLOURS = ["#2f6db5", "#c2571a", "#2e8b57", "#8e44ad", "#b8860b", "#c0392b", "#16809a", "#555555"]


def pin_level(uo, uio, oe, pin, pull_up=False):
    if pin < 8:
        return (uo >> pin) & 1
    b = pin - 8
    if (oe >> b) & 1:
        return (uio >> b) & 1
    return 1 if pull_up else 0


def capture(writes, shuttle, data, pins, cycles, pull_up=False, start_at=300):
    """Load one protocol, push data into its TX FIFO, and record the listed pins every cycle."""
    chip = Chip()
    chip.write_all(writes)
    chip.write(0x001, 1 << shuttle)
    for _ in range(start_at):
        chip.step(CSN_HIGH, 0xFF if pull_up else 0)
    queue = list(data)
    trace = {p: [] for p in pins}
    for _ in range(cycles):
        while queue and len(chip.shuttles[shuttle].tx_fifo) < 4:
            chip.write(0x008 + shuttle, queue.pop(0))
        uo, uio, oe = chip.step(CSN_HIGH, 0xFF if pull_up else 0)
        for p in pins:
            trace[p].append(pin_level(uo, uio, oe, p, pull_up))
    return trace


def first_activity(trace, idle):
    for i, v in enumerate(trace):
        if v != idle:
            return i
    return 0


def window(samples, start, before, after):
    """Cut a window round the first activity, padding with the idle level if it starts early."""
    lo = start - before
    pad = [samples[0]] * max(0, -lo)
    return pad + samples[max(0, lo):start + after]


def protocol_traces():
    """Return (label, [(signal name, samples)]) for the hero figure."""
    out = []

    t = capture(protocols.uart_tx(0, 1, 2e6, CLK), 0, [0x57, 0x65], [1], 3000)
    s = first_activity(t[1], 1)
    out.append(("UART", [("TX", window(t[1], s, 20, 420))]))

    t = capture(protocols.spi_master(0, 0, 4, 2e6, CLK, mode=0), 0, [0xA5, 0x3C], [0, 1, 2], 3000)
    s = first_activity(t[2], 1)
    out.append(("SPI", [("CS", window(t[2], s, 20, 420)), ("SCK", window(t[0], s, 20, 420)), ("MOSI", window(t[1], s, 20, 420))]))

    t = capture(protocols.i2c(0, 8, 400e3, CLK), 0, [0xA0, 0x42], [8, 9], 6000, pull_up=True)
    s = first_activity(t[8], 1)
    out.append(("I2C", [("SDA", window(t[8], s, 40, 1400)), ("SCL", window(t[9], s, 40, 1400))]))

    t = capture(protocols.usb_ls_tx(0, 8, CLK), 0, [0x80, 0xC3, 0xFF, 0x3F], [8, 9], 3000)
    s = first_activity(t[9], 0)
    out.append(("USB", [("D-", window(t[8], s, 30, 1500)), ("D+", window(t[9], s, 30, 1500))]))

    t = capture(protocols.can_tx(0, 1, 500e3, CLK), 0, [0x24, 0x68, 0x11, 0x22], [1], 8000)
    s = first_activity(t[1], 1)
    out.append(("CAN", [("TX", window(t[1], s, 40, 3000))]))

    t = capture(protocols.ws2812(0, 1, CLK), 0, [0xF0, 0x0F, 0xAA], [1], 2000)
    s = first_activity(t[1], 0)
    out.append(("WS2812", [("DIN", window(t[1], s, 10, 360))]))
    return out


def draw_wave(ax, samples, y, height, colour, lw=1.4):
    xs, ys = [], []
    n = len(samples)
    for i, v in enumerate(samples):
        x0, x1 = i / n, (i + 1) / n
        yy = y + (height if v else 0)
        xs += [x0, x1]
        ys += [yy, yy]
    ax.plot(xs, ys, color=colour, lw=lw, solid_joinstyle="miter")


def hero():
    rows = protocol_traces()
    n_signals = sum(len(r[1]) for r in rows)
    fig = plt.figure(figsize=(12, 6.2), dpi=150)
    fig.patch.set_facecolor("white")
    ax = fig.add_axes([0.0, 0.0, 1.0, 1.0])
    ax.set_xlim(-0.42, 1.03)
    ax.set_ylim(-0.6, n_signals + 2.6)
    ax.axis("off")

    chip = FancyBboxPatch((-0.39, n_signals / 2 - 1.6), 0.17, 3.2, boxstyle="round,pad=0.02,rounding_size=0.03",
                          fc="#20262e", ec="#20262e")
    ax.add_patch(chip)
    for k in range(9):
        yy = n_signals / 2 - 1.45 + k * 0.36
        ax.plot([-0.42, -0.39], [yy, yy], color="#9aa5b1", lw=2)
        ax.plot([-0.22, -0.19], [yy, yy], color="#9aa5b1", lw=2)
    ax.text(-0.305, n_signals / 2 + 0.55, "Linecraft", color="white", ha="center", va="center", fontsize=20, weight="bold")
    ax.text(-0.305, n_signals / 2 - 0.25, "4 shuttles\nline coder\nCRC unit", color="#c9d2dc", ha="center",
            va="center", fontsize=8.5, linespacing=1.5)

    y = n_signals + 0.2
    for k, (label, signals) in enumerate(rows):
        colour = COLOURS[k % len(COLOURS)]
        top = y
        for name, samples in signals:
            y -= 1
            draw_wave(ax, samples, y + 0.15, 0.6, colour)
            ax.text(-0.005, y + 0.45, name, ha="right", va="center", fontsize=8, color=MUTED, family="monospace")
        ax.text(-0.085, (top + y) / 2 + 0.1, label, ha="right", va="center", fontsize=11, color=colour, weight="bold")
        ax.plot([-0.07, -0.07], [y + 0.12, top - 0.12], color=colour, lw=2.5, solid_capstyle="round")
    ax.text(-0.42, n_signals + 2.05, "One chip, many wire protocols", fontsize=17, color=INK, weight="bold")
    ax.text(-0.42, n_signals + 1.3, "Each trace is a real pin waveform from the cycle accurate model of Linecraft, "
            "loaded with a different program, line code table and CRC polynomial.", fontsize=9.5, color=MUTED)
    fig.savefig(os.path.join(OUT, "hero.png"), facecolor="white")
    plt.close(fig)


def linecode_figure():
    """Data bits into symbols for three tables, with the bits that the table inserted marked."""
    bits = [1, 1, 1, 1, 1, 1, 1, 0, 1, 0, 0, 1]
    codes = [("NRZI", linecodes.nrzi(), 2), ("Manchester", linecodes.manchester(), 3), ("USB stuffing\nand NRZI", linecodes.usb(), 14)]
    fig, axes = plt.subplots(len(codes), 1, figsize=(11, 3.8), dpi=150, sharex=True)
    width = 26
    for ax, (name, code, states) in zip(axes, codes):
        table, init = code.tx, code.tx_init
        symbols, consumed = trace_tx(table, init, bits)
        ax.set_xlim(0, width)
        ax.set_ylim(-0.3, 1.3)
        ax.axis("off")
        k = 0
        for i, (s, c) in enumerate(zip(symbols, consumed)):
            if not c and "Manchester" not in name:
                ax.add_patch(plt.Rectangle((i, -0.2), 1, 1.4, fc="#f7d4d4", ec="none"))
            if c:
                x = 2 * k + 1 if "Manchester" in name else i + 0.5
                ax.text(x, -0.28, str(bits[k]), ha="center", va="top", fontsize=7.5, color=MUTED, family="monospace")
                k += 1
        xs, ys = [], []
        for i, s in enumerate(symbols):
            xs += [i, i + 1]
            ys += [s, s]
        ax.plot(xs, ys, color=COLOURS[1] if "Manchester" in name else COLOURS[0], lw=1.8)
        ax.text(-0.3, 0.5, f"{name}\n({states} states)", ha="right", va="center", fontsize=9.5, color=INK)
    axes[0].set_title("The same data bits, 111111101001, through three line code tables", fontsize=10.5, color=INK, loc="left")
    axes[-1].text(width, -0.25, "shaded: a stuffed bit, which the table sent without using a data bit", ha="right", va="top",
                  fontsize=8.5, color="#a33")
    fig.subplots_adjust(left=0.17, right=0.98, top=0.90, bottom=0.08, hspace=0.35)
    fig.savefig(os.path.join(OUT, "linecodes.png"), facecolor="white")
    plt.close(fig)


def trace_tx(table, init, bits):
    """Run a transmit table step by step and return the symbols and whether each step used a data bit."""
    state, symbols, used, i = init, [], [], 0
    guard = 0
    while i < len(bits) and guard < 200:
        guard += 1
        entry = table[2 * state + bits[i]]
        nxt, out, xfer, _ = linecodes.unpack_entry(entry)
        symbols.append(out)
        used.append(bool(xfer))
        state = nxt
        if xfer:
            i += 1
    return symbols, used


def tolerance_figure():
    rows = []
    with open(os.path.join(OUT, "cdr_sweep.txt")) as f:
        for line in f:
            parts = line.replace("%", "").split()
            if len(parts) == 7 and parts[0].isdigit():
                rows.append((int(parts[0]), float(parts[1]), float(parts[3]), float(parts[4]), float(parts[6])))
    fig, ax = plt.subplots(figsize=(8.5, 4.2), dpi=150)
    for k, (p, plo, phi, mlo, mhi) in enumerate(rows):
        ax.plot([mlo, mhi], [p, p], color="#c9d2dc", lw=9, solid_capstyle="butt", zorder=1,
                label="measured: packets still pass" if k == 0 else None)
        ax.plot([plo, phi], [p, p], color=COLOURS[0], lw=3.5, solid_capstyle="butt", zorder=2,
                label="predicted from the table" if k == 0 else None)
    ax.axvspan(-1.5, 1.5, color=COLOURS[2], alpha=0.08, zorder=0)
    ax.text(0, -0.2, "USB allows a low speed device ±1.5%", ha="center", va="center", fontsize=8.5, color=COLOURS[2])
    ax.axvline(0, color=MUTED, lw=0.6)
    ax.set_yticks([r[0] for r in rows])
    ax.set_ylabel("SYNC_PHASE register P")
    ax.set_xlabel("transmitter clock error (%)")
    ax.set_ylim(-1, 14)
    ax.invert_yaxis()
    ax.set_title("How much clock error a USB receiver on Linecraft survives", fontsize=11, color=INK, loc="left")
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.legend(frameon=False, fontsize=8.5, loc="lower left")
    fig.tight_layout()
    fig.savefig(os.path.join(OUT, "tolerance.png"), facecolor="white")
    plt.close(fig)


def box(ax, x, y, w, h, text, fc, ec=None, size=9.5, colour=INK, weight="normal"):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.01,rounding_size=0.02", fc=fc, ec=ec or fc))
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", fontsize=size, color=colour, weight=weight,
            linespacing=1.35)


def arrow(ax, x0, y0, x1, y1):
    ax.annotate("", xy=(x1, y1), xytext=(x0, y0), arrowprops=dict(arrowstyle="-|>", color=MUTED, lw=1.2))


def verification_figure():
    fig = plt.figure(figsize=(11, 4.4), dpi=150)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    box(ax, 0.02, 0.40, 0.15, 0.2, "docs/spec.md\none written spec", "#20262e", colour="white", weight="bold")
    box(ax, 0.24, 0.70, 0.19, 0.2, "Verilog RTL\nsrc/", "#dbe7f5")
    box(ax, 0.24, 0.10, 0.19, 0.2, "Python model\nlinecraft/model.py\nwritten separately", "#fbe6d4")
    arrow(ax, 0.17, 0.55, 0.24, 0.78)
    arrow(ax, 0.17, 0.45, 0.24, 0.22)
    box(ax, 0.48, 0.38, 0.22, 0.24, "Lockstep\nrandom programs, host traffic;\nevery pin and register\nequal on every cycle", "#e3f1e3", size=9)
    arrow(ax, 0.335, 0.70, 0.52, 0.62)
    arrow(ax, 0.335, 0.30, 0.52, 0.38)
    checks = [("Protocol peers\nseparate code for each protocol", 0.75), ("Formal proofs\ndivider, shuttle invariants", 0.53),
              ("Gate netlist\n32 pin tests again", 0.31), ("Python tests\n177 for the toolchain", 0.07)]
    for text, y in checks:
        box(ax, 0.79, y, 0.19, 0.17, text, "#eef0f3", size=8.8)
    ax.plot([0.43, 0.75], [0.84, 0.84], color=MUTED, lw=1.2)
    ax.plot([0.75, 0.75], [0.84, 0.395], color=MUTED, lw=1.2)
    for y in (0.835, 0.615, 0.395):
        arrow(ax, 0.75, y, 0.79, y)
    arrow(ax, 0.43, 0.155, 0.79, 0.155)
    fig.savefig(os.path.join(OUT, "verification.png"), facecolor="white")
    plt.close(fig)


def main():
    os.makedirs(OUT, exist_ok=True)
    hero()
    linecode_figure()
    tolerance_figure()
    verification_figure()
    print("figures written to", OUT)


if __name__ == "__main__":
    main()
