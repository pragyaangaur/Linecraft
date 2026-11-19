"""Small helpers shared by the model tests."""

from weft.asm import assemble
from weft.config import ShuttleConfig, setup_writes
from weft.model import Chip


def load(chip, shuttle, source, **fields):
    """Assemble `source`, configure `shuttle` with `fields`, return the Program."""
    program = assemble(source)
    chip.write_all(setup_writes(shuttle, ShuttleConfig(**fields), program))
    return program


def enable(chip, *shuttles):
    chip.write(0x001, sum(1 << e for e in shuttles))


def run(chip, cycles, ui_in=0, uio_in=0):
    """Step `cycles` times with fixed inputs; return the list of uo_out values."""
    return [chip.step(ui_in, uio_in)[0] for _ in range(cycles)]


def new_chip():
    return Chip()
