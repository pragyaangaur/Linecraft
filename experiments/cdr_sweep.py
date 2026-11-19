"""Measure how much clock mismatch the USB low speed receiver tolerates, and compare it with the bound from the line code.

Shuttle 0 runs usb_ls_tx and shuttle 1 runs usb_ls_rx on the same pins, inside the cycle accurate model (which the lockstep tests hold equal to the RTL). The transmitter's divider is scaled by (1 + delta) so its bit rate is off by delta. For each delta the script sends packets that force the longest possible runs and records whether the receiver gets every byte and a good CRC.

The predicted bound is |delta| < (1/(2k) - q/M) / R, with k = 2 ticks per bit, M = cycles per bit, R = the longest run of equal symbols that the usb line code can produce (found from its table by weft.analysis), and q = the synchronizer and divider uncertainty in cycles.

Run: python3 experiments/cdr_sweep.py [--plot results.svg]
"""

import os
import random
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from weft.asm import assemble                              # noqa: E402
from weft.config import ShuttleConfig, setup_writes, CRC_TX, CRC_RX   # noqa: E402
from weft.crc import CATALOGUE, weft_settings, crc_reference  # noqa: E402
from weft.model import Chip                                # noqa: E402
from weft import linecodes, analysis                       # noqa: E402

CLK_HZ = 50e6
CPB = CLK_HZ / 1.5e6
USB_CRC = weft_settings(CATALOGUE["CRC-16/USB"])


def program(name, origin=0):
    with open(os.path.join(ROOT, "programs", name + ".weft")) as f:
        return assemble(f".origin {origin}\n" + f.read())


def setup(delta, sync_phase):
    tx = ShuttleConfig(out_base=8, set_base=8, set_count=2, fst_diff=True,
                       out_right=True, autopull=True, pull_thresh=8)
    tx.set_ratio(CPB * (1 + delta) / 4)
    tx.use_line_code(linecodes.usb(), "tx")
    tx.use_crc(USB_CRC, CRC_TX)
    rx = ShuttleConfig(in_base=8, in_right=True, autopush=True, push_thresh=8,
                       sync_en=True, sync_pin=8, sync_phase=sync_phase,
                       match_mask=0x0300, match_val=0)
    rx.set_ratio(CPB / 2)
    rx.use_line_code(linecodes.usb(), "rx")
    rx.use_crc(USB_CRC, CRC_RX)
    return (setup_writes(0, tx, program("usb_ls_tx"))
            + setup_writes(1, rx, program("usb_ls_rx", origin=32)))


def send(chip, payload, max_cycles=20000):
    """Send one packet through the loopback and return (received bytes, irq)."""
    queue = [0x80] + payload
    got = []
    out = (0, 0, 0)
    quiet = 0
    for cycle in range(max_cycles):
        if queue and chip.read(0x005) & 1:
            chip.write(0x008, queue.pop(0))
        while (chip.read(0x006) >> 4) & 7:
            got.append(chip.read(0x009))
        _, uio_out, uio_oe = out
        dm = (uio_out & 1) if uio_oe & 1 else 1
        dp = ((uio_out >> 1) & 1) if uio_oe & 2 else 0
        out = chip.step(0x80, dm | (dp << 1))
        if chip.irq & 3:
            quiet += 1
            if quiet > 200:
                break
    return got, chip.irq


def trial(delta, payloads, sync_phase=5):
    chip = Chip()
    chip.write_all(setup(delta, sync_phase))
    chip.write(0x001, 0x3)
    for payload in payloads:
        chip.write(0x003, 0xFF)                       # clear IRQ flags
        got, irq = send(chip, payload)
        crc = crc_reference(CATALOGUE["CRC-16/USB"], bytes(payload[1:]))
        want = [0x80] + payload + [crc & 0xFF, crc >> 8]
        if got != want or irq & 3 != 1:
            return False
    return True


def payloads(seed=1):
    rng = random.Random(seed)
    worst = [0xC3, 0xFF, 0xFF, 0xFF, 0xFF]            # long runs of ones, stuffed every 6 bits
    zeros = [0x4B, 0x00, 0x00, 0x00, 0x00]            # a transition on every bit
    mixed = [0xC3] + [rng.randrange(256) for _ in range(6)]
    return [worst, zeros, mixed]


def predicted(R, M, P, q=1.0):
    """Predicted working range of delta for sync phase P.

    After an edge the receiver ticks first at P + 3 cycles (two synchronizer
    flops and the cycle that sees the edge), and an IN reads pins that are two
    cycles old, so the samples sit at s = (P + 1) / M and s + 1/2 of a bit after
    the edge. With bits longer by a factor (1 + delta), the j-th sample after an
    edge moves by j * delta bits, and a run gives at most R samples before the
    next edge pulls the phase back. Sampling at s fails when R * delta reaches
    s (slow transmitter), and sampling at s + 1/2 fails when -R * delta reaches
    1/2 - s (fast transmitter). One cycle of edge and divider uncertainty is
    taken off each side.
    """
    s = (P + 1) / M
    return -(0.5 - s - q / M) / R, (s - q / M) / R


def edge(P, pkts, sign, step=0.001, limit=0.08):
    """Walk out from delta = 0 in `sign` direction until the first failure."""
    d = 0.0
    while abs(d) < limit:
        nd = d + sign * step
        if not trial(nd, pkts, sync_phase=P):
            return d
        d = nd
    return d


def main():
    code = linecodes.usb()
    R = analysis.max_run_length(code.tx, code.tx_init)
    print(f"usb line code: longest run R = {R} symbols, M = {CPB:.2f} cycles per bit, 2 ticks per bit")
    pkts = payloads()
    rows = []
    print(" P   predicted range        measured range")
    for P in (1, 3, 5, 7, 9, 11):
        lo_p, hi_p = predicted(R, CPB, P)
        lo_m, hi_m = edge(P, pkts, -1), edge(P, pkts, +1)
        rows.append((P, lo_p, hi_p, lo_m, hi_m))
        print(f"{P:2d}   {100 * lo_p:+6.2f}% to {100 * hi_p:+6.2f}%    {100 * lo_m:+6.2f}% to {100 * hi_m:+6.2f}%", flush=True)
    if "--plot" in sys.argv:
        write_svg(sys.argv[sys.argv.index("--plot") + 1], rows)


def write_svg(path, rows):
    """Predicted (bars) and measured (dots) working ranges for each sync phase, with no plotting library."""
    w, h = 640, 60 + 46 * len(rows)
    left, right = 70, w - 30
    dmin, dmax = -0.06, 0.06
    x = lambda d: left + (d - dmin) / (dmax - dmin) * (right - left)
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" font-family="sans-serif" font-size="12">',
           '<rect width="100%" height="100%" fill="white"/>',
           f'<text x="{w / 2}" y="18" text-anchor="middle">USB low speed receiver: clock offset it survives, by sync phase P</text>']
    for t in range(-6, 7, 2):
        xx = x(t / 100)
        out.append(f'<line x1="{xx:.1f}" y1="28" x2="{xx:.1f}" y2="{h - 30}" stroke="#ddd"/>')
        out.append(f'<text x="{xx:.1f}" y="{h - 14}" text-anchor="middle">{t:+d}%</text>')
    for i, (P, lo_p, hi_p, lo_m, hi_m) in enumerate(rows):
        y = 44 + 46 * i
        out.append(f'<text x="{left - 10}" y="{y + 14}" text-anchor="end">P = {P}</text>')
        out.append(f'<rect x="{x(lo_p):.1f}" y="{y}" width="{x(hi_p) - x(lo_p):.1f}" height="20" fill="#9ec5ea"/>')
        for d in (lo_m, hi_m):
            out.append(f'<circle cx="{x(d):.1f}" cy="{y + 10}" r="5" fill="#1f4e79"/>')
    out.append(f'<text x="{w / 2}" y="{h - 1}" text-anchor="middle" font-size="11">bars: predicted from the line code graph; dots: measured edges in the cycle accurate model</text>')
    out.append("</svg>")
    with open(path, "w") as f:
        f.write("\n".join(out))


if __name__ == "__main__":
    main()
