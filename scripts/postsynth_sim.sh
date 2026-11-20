#!/bin/sh
# Synthesise the RTL to generic Yosys gates and run the pin level cocotb
# tests on that netlist. It catches differences between simulation and
# synthesis (X propagation, latches, missing resets) before the Tiny Tapeout
# gate level job, which needs a finished GDS run.
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
ln -s "$ROOT" "$WORK/linecraft"
mkdir "$WORK/gates"
cd "$ROOT/src"
yosys -q -p "read_verilog project.v linecraft_host.v linecraft_shuttle.v linecraft_divider.v; synth -flatten -top tt_um_pragyaangaur_linecraft; opt_clean; write_verilog -noattr $WORK/gates/netlist.v"
cat "$WORK/gates/netlist.v" "$(yosys-config --datdir)/simcells.v" > "$WORK/gates/project.v"
for f in linecraft_host.v linecraft_shuttle.v linecraft_divider.v; do echo "// the gates are in project.v" > "$WORK/gates/$f"; done
cd "$WORK/linecraft/test"
make -B SRC_DIR="$WORK/gates" COCOTB_TEST_MODULES=test,test_protocols SIM_BUILD=sim_build/postsynth > "$WORK/run.log" 2>&1 || true
grep -E "TESTS=" "$WORK/run.log"
grep -q "FAIL=0" "$WORK/run.log"
