"""Constrained random lockstep testing of the RTL against the Python model.

Each trial builds a random program, random configurations for all four shuttles, random line coder tables, random CRC settings and random FIFO contents. It loads them into the RTL through the SPI port and into the model through its register interface, enables all shuttles, and then drives random pin inputs. On every clock cycle it compares every output pin and the architectural state of every shuttle. The RTL and the model were written separately from docs/spec.md, so any difference is a bug in one of them or a gap in the spec.
"""

import os
import random
import sys

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, ReadOnly, RisingEdge

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from weft.model import Chip                        # noqa: E402
from weft.config import ShuttleConfig, setup_writes  # noqa: E402
from weft.asm import disassemble                    # noqa: E402
from weft_host import Host, reset                  # noqa: E402

TRIALS = int(os.environ.get("LOCKSTEP_TRIALS", "12"))
OPS = ["JMP", "WAIT", "IN", "OUT", "PUSH", "PULL", "MOV", "SET", "IRQ", "CTL"]
CONDS = ["always", "!X", "X--", "!Y", "Y--", "MORE", "PIN", "!PIN", "!OSRE", "TXNE",
         "MATCH", "!MATCH", "CRCOK", "FERR", "FPEND", "RXNF"]


class Coverage:
    """Counts what the random programs actually exercised, sampled from the RTL each cycle."""

    def __init__(self):
        self.ran = {op: 0 for op in OPS}
        self.stalled = {op: 0 for op in OPS}
        self.jumps = {c: [0, 0] for c in CONDS}       # [not taken, taken]
        self.fst_steps = [0, 0]                         # transmit, receive line coder steps
        self.crc_feeds = 0
        self.side_sets = 0

    def sample(self, u):
        if not int(u.attempt.value):
            return
        op = int(u.op.value)
        if op >= len(OPS):
            return
        if int(u.stall.value):
            self.stalled[OPS[op]] += 1
            return
        self.ran[OPS[op]] += 1
        f_a = int(u.f_a.value)
        if op == 0:
            self.jumps[CONDS[f_a]][int(u.cond.value)] += 1
        if op == 3 and f_a == 7:
            self.fst_steps[0] += 1
        if op == 2 and f_a == 6:
            self.fst_steps[1] += 1
        if int(u.feed_n.value):
            self.crc_feeds += 1

    def report(self):
        lines = ["opcode   ran      stalled"]
        lines += [f"{op:<8} {self.ran[op]:<8} {self.stalled[op]}" for op in OPS]
        lines.append("jump condition   not taken   taken")
        lines += [f"{c:<16} {n:<11} {t}" for c, (n, t) in self.jumps.items()]
        lines.append(f"line coder steps: {self.fst_steps[0]} transmit, {self.fst_steps[1]} receive")
        lines.append(f"cycles with CRC bits fed: {self.crc_feeds}")
        return "\n".join(lines)

    def holes(self):
        missing = [f"{op} never ran" for op in OPS if not self.ran[op]]
        missing += [f"{op} never stalled" for op in ("WAIT", "IN", "OUT", "PUSH", "PULL") if not self.stalled[op]]
        missing += [f"JMP {c} never {'taken' if i else 'not taken'}"
                    for c, counts in self.jumps.items() for i in (0, 1)
                    if not counts[i] and not (c == "always" and i == 0)]
        return missing


COVERAGE = Coverage()
CYCLES = int(os.environ.get("LOCKSTEP_CYCLES", "600"))
SEED = int(os.environ.get("LOCKSTEP_SEED", "1"))
HOST_TRAFFIC = os.environ.get("LOCKSTEP_HOST_TRAFFIC", "1") == "1"
NS = int(os.environ.get("WEFT_NS", "4"))      # must match NS in src/project.v


def random_instruction(rng):
    op = rng.choices(range(16), weights=[10, 4, 10, 10, 5, 5, 8, 10, 3, 3, 1, 0, 0, 0, 0, 1])[0]
    delay = rng.choice([0, 0, 0, 1, 2, 3, rng.randrange(32)])
    side = rng.randrange(8)
    if op == 0:                                    # JMP
        args = (rng.randrange(16) << 8) | rng.randrange(64)
    elif op == 1:                                  # WAIT, mostly on pins so it finishes
        src = rng.choice([0, 0, 1, 1, 2, 3])
        args = (rng.randrange(2) << 7) | (src << 5) | rng.randrange(16)
    elif op in (2, 3):                             # IN, OUT
        src = rng.randrange(8)
        n = 1 if src in (6, 7) and rng.random() < 0.8 else rng.randrange(64)
        args = (src << 8) | n
    elif op in (4, 5):                             # PUSH, PULL
        args = rng.randrange(4)
    elif op == 6:                                  # MOV
        args = (rng.randrange(8) << 8) | (rng.randrange(4) << 6) | rng.randrange(8)
    elif op == 7:                                  # SET
        args = (rng.choice([0, 1, 2, 4, 4, 3]) << 8) | rng.randrange(256)
    elif op == 8:                                  # IRQ
        args = (rng.randrange(2) << 4) | rng.randrange(8)
    else:
        args = rng.randrange(4096)
    return (op << 20) | (delay << 15) | (side << 12) | args


def random_table(rng):
    """A random line coder table in which some states have two equal entries with no transfer, so FPEND is exercised."""
    table = [rng.randrange(128) for _ in range(32)]
    for state in range(16):
        if rng.random() < 0.3:
            table[2 * state] &= ~(1 << 5)
            table[2 * state + 1] = table[2 * state]
    return table


def random_config(rng):
    cfg = ShuttleConfig(
        div_int=rng.choice([1, 1, 1, 2, 3, 5]),
        div_frac=rng.choice([0, 0, rng.randrange(256)]),
        in_base=rng.randrange(16), out_base=rng.randrange(16), set_base=rng.randrange(16),
        set_count=rng.randrange(9), side_base=rng.randrange(16),
        side_count=rng.randrange(4), side_pindir=rng.random() < 0.3,
        out_right=rng.random() < 0.5, in_right=rng.random() < 0.5,
        autopull=rng.random() < 0.5, autopush=rng.random() < 0.5, fst_diff=rng.random() < 0.3,
        pull_thresh=rng.randrange(1, 33), push_thresh=rng.randrange(1, 33),
        wrap_bot=rng.randrange(64), wrap_top=rng.randrange(64), start_pc=rng.randrange(64),
        jmp_pin=rng.randrange(16),
        sync_en=rng.random() < 0.3, sync_pin=rng.randrange(16), sync_phase=rng.randrange(6),
        fst_init=rng.randrange(16),
        fst_table=random_table(rng),
        crc_src=rng.randrange(3), crc_init_ones=rng.random() < 0.5,
        crc_xorout_ones=rng.random() < 0.5, crc_width=rng.randrange(1, 33),
        crc_poly=rng.randrange(1 << 32),
        # A check of 0 or all ones matches a fresh CRC register, so CRCOK is seen taken.
        crc_check=rng.choice([0, 0xFFFFFFFF, rng.randrange(1 << 32)]),
        # Masks with few bits make MATCH true often enough to be exercised.
        match_mask=rng.choice([0, 1 << rng.randrange(16), (1 << rng.randrange(16)) | (1 << rng.randrange(16)),
                               rng.randrange(1 << 16)]),
        match_val=rng.choice([0, rng.randrange(1 << 16)]),
    )
    return cfg


def rtl_state(sh):
    return {
        "pc": int(sh.pc.value), "x": int(sh.x.value), "y": int(sh.y.value),
        "osr": int(sh.osr.value), "osr_left": int(sh.osr_left.value),
        "isr": int(sh.isr.value), "isr_cnt": int(sh.isr_cnt.value), "dly": int(sh.dly.value),
        "fst": int(sh.fst.value), "fst_err": int(sh.fst_err.value), "crc": int(sh.crc.value),
        "pv": int(sh.pv.value), "pe": int(sh.pe.value),
        "tx_n": int(sh.tx_cnt.value), "rx_n": int(sh.rx_cnt.value),
    }


def model_state(sh):
    s = sh.snapshot()
    return {k: s[k] for k in ("pc", "x", "y", "osr", "osr_left", "isr", "isr_cnt", "dly",
                              "fst", "fst_err", "crc", "pv", "pe")} | {
        "tx_n": len(s["tx_fifo"]), "rx_n": len(s["rx_fifo"])}


async def one_trial(dut, rng, trial):
    host = Host(dut)
    await reset(dut)
    chip = Chip(num_shuttles=NS)

    words = [random_instruction(rng) for _ in range(64)]
    writes = []
    for i, w in enumerate(words):
        a = 0x400 + 4 * i
        writes += [(a, w & 0xFF), (a + 1, (w >> 8) & 0xFF), (a + 2, w >> 16)]
    for e in range(NS):
        writes += setup_writes(e, random_config(rng))
    preload = {e: [rng.randrange(256) for _ in range(rng.randrange(5))] for e in range(NS)}

    await host.write_many(writes)
    chip.write_all(writes)
    for e, data in preload.items():
        if data:
            await host.write(0x008 + e, data)
            for b in data:
                chip.write(0x008 + e, b)

    # Enable all shuttles and line the model up with the clock edge where the
    # RTL's enable register changes. The synchronizer flops are copied from the
    # RTL, since the host pins were toggling until then.
    enable = rng.choice([0xF, 0xF, 0x7, 0x9]) & ((1 << NS) - 1)
    writer = cocotb.start_soon(host.write(0x001, enable))
    top = dut.user_project
    while True:
        await RisingEdge(dut.clk)
        await ReadOnly()
        if int(top.enable.value) == enable:
            break
    chip.write(0x001, enable)
    chip.sync1, chip.pins, chip.pins_prev = (int(top.sync1.value), int(top.pins.value),
                                             int(top.pins_prev.value))

    # From here on the model takes the same pin inputs as the RTL on every
    # cycle. Once the enable write is over, a background host keeps pushing
    # and popping FIFO bytes while the shuttles run, and random values go on
    # the pins the host does not use. Every host push or pop that the RTL's
    # host port makes is copied into the model at the same clock edge, after
    # the shuttles' own step, judged against the FIFO as it was at the start
    # of the cycle (the rule in docs/spec.md).
    traffic = None
    popped_rtl, popped_model = [], []

    async def host_traffic():
        # Change pins only just after rising edges, as the host driver always does.
        await RisingEdge(dut.clk)
        while True:
            e = rng.randrange(NS)
            if rng.random() < 0.5:
                await host.write(0x008 + e, [rng.randrange(256) for _ in range(rng.randrange(1, 4))])
            else:
                popped_rtl.extend((e, b) for b in await host.read(0x008 + e, rng.randrange(1, 3)))

    uio_rand = 0
    for cycle in range(CYCLES):
        await FallingEdge(dut.clk)
        for e in range(NS):
            COVERAGE.sample(top.sh[e].u)
        if writer.done():
            if traffic is None and HOST_TRAFFIC:
                traffic = cocotb.start_soon(host_traffic())
            if rng.random() < 0.3:
                host.set_pins(rng.randrange(32), 0x1F)
            if rng.random() < 0.3:
                uio_rand ^= 1 << rng.randrange(8)
            dut.uio_in.value = uio_rand
            # A value written this timestep cannot be read back yet, so use
            # the host driver's copy of ui_in and our own copy of uio_in.
            ui, uio = host.ui, uio_rand
        else:
            ui, uio = int(dut.ui_in.value), int(dut.uio_in.value)
        wr = int(top.h_wr.value) and 8 <= int(top.h_waddr.value) <= 11
        rd = int(top.h_rd_commit.value) and 8 <= int(top.h_rd_addr.value) <= 11
        if wr:
            we, wbyte = int(top.h_waddr.value) - 8, int(top.h_wdata.value)
            tx_full_before = len(chip.shuttles[we].tx_fifo) >= 4
        if rd:
            re = int(top.h_rd_addr.value) - 8
            rx_empty_before = len(chip.shuttles[re].rx_fifo) == 0
        before = [model_state(sh) for sh in chip.shuttles]
        want = chip.step(ui, uio)
        if wr and not tx_full_before:
            chip.shuttles[we].tx_fifo.append(wbyte)
        if rd:
            popped_model.append((re, chip.shuttles[re].rx_fifo.popleft() if not rx_empty_before else 0))
        await RisingEdge(dut.clk)
        await ReadOnly()
        got = (int(dut.uo_out.value) & 0x7F, int(dut.uio_out.value), int(dut.uio_oe.value))
        where = f"trial {trial} cycle {cycle}"
        for e in range(NS):
            r = rtl_state(top.sh[e].u)
            m = model_state(chip.shuttles[e])
            if r != m:
                diff = {k: (r[k], m[k]) for k in r if r[k] != m[k]}
                b = before[e]
                sh = chip.shuttles[e]
                print("before", {k: hex(v) for k, v in b.items()})
                print("instr", (disassemble(words[b["pc"]], sh.side_count) or hex(words[b["pc"]])) if hasattr(sh, "side_count") else hex(words[b["pc"]]),
                      "cfg", {k: getattr(sh, k) for k in vars(sh) if not k.startswith("_") and isinstance(getattr(sh, k), (int, bool))})
                assert False, f"{where} shuttle {e} state differs (rtl, model): {diff}"
        assert int(top.irq.value) == chip.irq, f"{where} irq rtl {int(top.irq.value):#x} model {chip.irq:#x}"
        assert got == tuple(want), f"{where} pins rtl {got} model {tuple(want)}"
    if traffic is not None:
        traffic.cancel()
    await FallingEdge(dut.clk)
    # Every byte the host read must be the byte the model popped at that edge.
    done = min(len(popped_rtl), len(popped_model))
    assert popped_rtl[:done] == popped_model[:done], f"trial {trial}: host reads differ from the model"
    return words


@cocotb.test()
async def test_lockstep_random(dut):
    cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
    rng = random.Random(SEED)
    for trial in range(TRIALS):
        await one_trial(dut, rng, trial)
    dut._log.info("coverage over %d trials of %d cycles\n%s", TRIALS, CYCLES, COVERAGE.report())
    holes = COVERAGE.holes()
    if holes:
        dut._log.warning("coverage holes: %s", "; ".join(holes))
