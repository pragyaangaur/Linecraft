"""Run a protocol preset in the cycle accurate model and save the pins as a VCD waveform.

    python -m linecraft.sim uart_tx shuttle=0 tx_pin=1 baud=115200 clk_hz=25e6 --send 48,69 --cycles 4000 --vcd uart.vcd

The arguments after the preset name are the keyword arguments of the function in linecraft.protocols, as for linecraft.export. `--send` pushes bytes into the TX FIFO of that shuttle, a few at a time as space frees up. The VCD file holds all 16 output pins and the IRQ flags, and opens in GTKWave or Surfer. No Verilog simulator is needed.
"""

import sys

from .export import parse
from .model import Chip
from .protocols import PRESETS

CSN_HIGH = 0x80


def run(writes, shuttle, send, cycles):
    """Return a list of (uo_out, uio_out, uio_oe, irq) after each cycle."""
    chip = Chip()
    chip.write_all(writes)
    chip.write(0x001, 1 << shuttle)
    queue = list(send)
    samples = []
    for _ in range(cycles):
        while queue and len(chip.shuttles[shuttle].tx_fifo) < 4:
            chip.write(0x008 + shuttle, queue.pop(0))
        uo, uio, oe = chip.step(CSN_HIGH, 0)
        samples.append((uo, uio, oe, chip.irq))
    return samples


def write_vcd(path, samples, clk_hz):
    """Write the samples as a VCD file, one timestep per clock cycle."""
    ps = round(1e12 / clk_hz)
    signals = [(f"pin{p}", p) for p in range(16)] + [("irq", None)]
    ids = {name: chr(33 + k) for k, (name, _) in enumerate(signals)}
    with open(path, "w") as f:
        f.write("$timescale 1ps $end\n$scope module linecraft $end\n")
        for name, pin in signals:
            width = 8 if pin is None else 1
            f.write(f"$var wire {width} {ids[name]} {name} $end\n")
        f.write("$upscope $end\n$enddefinitions $end\n")
        last = {}
        for t, (uo, uio, oe, irq) in enumerate(samples):
            values = {}
            for name, pin in signals:
                if pin is None:
                    values[name] = "b" + format(irq, "08b") + " "
                elif pin < 8:
                    values[name] = str((uo >> pin) & 1)
                else:
                    b = pin - 8
                    values[name] = str((uio >> b) & 1) if (oe >> b) & 1 else "z"
            changed = [n for n in values if last.get(n) != values[n]]
            if changed:
                f.write(f"#{t * ps}\n")
                for n in changed:
                    f.write(f"{values[n]}{ids[n]}\n")
            last = values


def main(argv):
    if len(argv) < 1 or argv[0] not in PRESETS:
        print("usage: python -m linecraft.sim <preset> name=value ... [--send b0,b1,...] [--cycles N] [--vcd out.vcd]",
              file=sys.stderr)
        print("presets: " + ", ".join(sorted(PRESETS)), file=sys.stderr)
        return 2
    send, cycles, vcd, kwargs = [], 5000, "linecraft.vcd", {}
    args = argv[1:]
    while args:
        a = args.pop(0)
        if a == "--send":
            send = [int(x, 0) for x in args.pop(0).split(",")]
        elif a == "--cycles":
            cycles = int(args.pop(0))
        elif a == "--vcd":
            vcd = args.pop(0)
        else:
            k, v = a.split("=", 1)
            kwargs[k] = parse(v)
    writes = PRESETS[argv[0]](**kwargs)
    samples = run(writes, kwargs.get("shuttle", 0), send, cycles)
    write_vcd(vcd, samples, kwargs.get("clk_hz", 25e6))
    print(f"{cycles} cycles of {argv[0]} written to {vcd}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
