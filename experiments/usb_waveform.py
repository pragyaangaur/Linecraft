"""Draw one USB low speed packet as Weft sends it, from the cycle accurate model.

Shuttle 0 runs programs/usb_ls_tx.weft with the usb line code and the CRC16 unit. The script records D- and D+ on every clock, marks the stuffed bits that the line coder inserted, and writes an SVG.

Run: python3 experiments/usb_waveform.py docs/img/usb_waveform.svg
"""

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from weft import protocols                     # noqa: E402
from weft.model import Chip                    # noqa: E402

CLK = 50e6
CPB = CLK / 1.5e6


def record(payload):
    chip = Chip()
    chip.write_all(protocols.usb_ls_tx(0, 8, CLK))
    chip.write(0x001, 1)
    for _ in range(200):
        chip.step(0x80, 0)
    for b in [0x80] + payload:
        chip.write(0x008, b)
    samples, stuffed = [], []
    sh = chip.shuttles[0]
    for t in range(3000):
        before = (sh.pc, sh.osr_left, sh.fst)
        _, uio, oe = chip.step(0x80, 0)
        dm = uio & 1 if oe & 1 else 1
        dp = (uio >> 1) & 1 if oe & 2 else 0
        samples.append((dm, dp))
    return samples


def bits_from(samples):
    """Return per-bit (start cycle, d-, d+) at mid-bit, from the first K to the end of the J after EOP."""
    start = next(i for i, (dm, dp) in enumerate(samples) if dm == 0 and dp == 1)
    out, t = [], start
    while t + CPB < len(samples):
        out.append((t, *samples[int(t + CPB / 2)]))
        dm, dp = samples[int(t + CPB / 2)]
        t += CPB
        if len(out) > 3 and out[-2][1:] == (0, 0) and (dm, dp) == (1, 0):
            break
    return out


def label_bits(bits):
    """Undo NRZI and stuffing and name each bit: SYNC, PID, DATA, CRC, STUFF or EOP."""
    names, level, ones, n_data = [], 1, 0, 0
    for _, dm, dp in bits:
        if (dm, dp) == (0, 0):
            names.append("EOP")
            continue
        if (dm, dp) == (1, 0) and names and names[-1] == "EOP":
            names.append("J")
            continue
        b = int(dm == level)
        level = dm
        if ones == 6:
            names.append("STUFF")
            ones = 0
            continue
        ones = ones + 1 if b else 0
        field = "SYNC" if n_data < 8 else "PID" if n_data < 16 else "DATA"
        names.append(field)
        n_data += 1
    data_bits = [i for i, n in enumerate(names) if n == "DATA"]
    for i in data_bits[-16:]:
        names[i] = "CRC"
    return names


def svg(path, bits, names):
    colours = {"SYNC": "#dfe7f2", "PID": "#fbe3c8", "DATA": "#dcefdc", "CRC": "#efd9ef",
               "STUFF": "#f7c6c6", "EOP": "#e6e6e6", "J": "#e6e6e6"}
    w = 18 * len(bits) + 120
    h = 190
    x0 = 70
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" font-family="sans-serif" font-size="11">',
           '<rect width="100%" height="100%" fill="white"/>',
           f'<text x="{x0}" y="16" font-size="13">One USB low speed DATA0 packet from usb_ls_tx.weft, with the stuffed bits the line coder inserted</text>']
    for i, name in enumerate(names):
        x = x0 + 18 * i
        out.append(f'<rect x="{x}" y="26" width="18" height="120" fill="{colours[name]}"/>')
        if name == "STUFF":
            out.append(f'<text x="{x + 9}" y="160" text-anchor="middle" fill="#a33">S</text>')
    for row, (label, idx, y) in enumerate((("D-", 1, 40), ("D+", 2, 95))):
        out.append(f'<text x="{x0 - 10}" y="{y + 22}" text-anchor="end">{label}</text>')
        pts = []
        for i, bit in enumerate(bits):
            level = bit[idx]
            yy = y + (5 if level else 40)
            pts += [(x0 + 18 * i, yy), (x0 + 18 * (i + 1), yy)]
        out.append('<polyline fill="none" stroke="#222" stroke-width="1.5" points="'
                   + " ".join(f"{a},{b}" for a, b in pts) + '"/>')
    x = x0
    legend = ["SYNC", "PID", "DATA", "CRC", "STUFF", "EOP"]
    for k, name in enumerate(legend):
        out.append(f'<rect x="{x0 + 90 * k}" y="170" width="12" height="12" fill="{colours[name]}"/>')
        out.append(f'<text x="{x0 + 90 * k + 16}" y="180">{"stuffed bit" if name == "STUFF" else name}</text>')
    out.append("</svg>")
    with open(path, "w") as f:
        f.write("\n".join(out))


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "usb_waveform.svg"
    payload = [0xC3, 0xFF, 0x3F]          # DATA0 and bytes with runs of ones
    bits = bits_from(record(payload))
    names = label_bits(bits)
    svg(path, bits, names)
    print(f"{len(bits)} bit times, {names.count('STUFF')} stuffed bits, written to {path}")


if __name__ == "__main__":
    main()
