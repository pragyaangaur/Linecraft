"""Save a protocol setup as JSON for a host board.

    python -m weft.export uart_tx shuttle=0 tx_pin=1 baud=115200 clk_hz=50e6 > uart.json

The arguments after the preset name are the keyword arguments of the function in weft.protocols. The output is a JSON object with the preset name, its arguments and the list of [address, byte] writes, which host/weft_rp2350.py replays over SPI.
"""

import json
import sys

from .protocols import PRESETS


def parse(value):
    for kind in (int, float):
        try:
            return kind(value)
        except ValueError:
            pass
    if value in ("True", "False"):
        return value == "True"
    return value


def main(argv):
    if len(argv) < 1 or argv[0] not in PRESETS:
        print("usage: python -m weft.export <preset> name=value ...", file=sys.stderr)
        print("presets: " + ", ".join(sorted(PRESETS)), file=sys.stderr)
        return 2
    kwargs = dict((k, parse(v)) for k, v in (a.split("=", 1) for a in argv[1:]))
    writes = PRESETS[argv[0]](**kwargs)
    json.dump({"preset": argv[0], "args": kwargs, "writes": writes}, sys.stdout)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
