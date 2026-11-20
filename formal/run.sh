#!/bin/sh
# Run the formal proofs. Each one is unbounded: a base case from reset plus
# an induction step, so a pass means the property holds on every cycle of
# every run.
#
#   divider  zero cumulative drift of the fractional divider, for every
#            DIV_INT and DIV_FRAC (yosys-smtbmc with z3, k-induction)
#   shuttle  FIFO, counter and issue invariants of a whole shuttle, for every
#            program, configuration and input (yosys sat, temporal induction)
#   sanity   the shuttle setup with one deliberately false assertion added;
#            this must fail, which shows the setup is not vacuous
set -e
cd "$(dirname "$0")/.."
OUT=${OUT:-formal/build}
mkdir -p "$OUT"

echo "== divider: k-induction with z3"
yosys -q -p "read_verilog -formal -DFORMAL_DIVIDER src/linecraft_divider.v; prep -top linecraft_divider; async2sync; dffunmap; write_smt2 -wires $OUT/divider.smt2"
yosys-smtbmc -s z3 --presat -t 4 "$OUT/divider.smt2" > "$OUT/divider.bmc.log" || { tail -20 "$OUT/divider.bmc.log"; exit 1; }
yosys-smtbmc -s z3 -i -t 4 "$OUT/divider.smt2" > "$OUT/divider.ind.log" || { tail -20 "$OUT/divider.ind.log"; exit 1; }
echo "   base case and induction: PASS"

shuttle() {
    yosys -p "read_verilog -formal -DFORMAL_SHUTTLE $1 src/linecraft_shuttle.v src/linecraft_divider.v; prep -top linecraft_shuttle; flatten; memory_map; opt -fast; async2sync; dffunmap; opt_clean; sat -tempinduct -prove-asserts -set-assumes -set-init-zero -seq 1 -maxsteps 12" > "$2" 2>&1
    grep -q "Induction step proven: SUCCESS" "$2"
}

echo "== shuttle: temporal induction with yosys sat"
if shuttle "" "$OUT/shuttle.log"; then echo "   PASS"; else tail -20 "$OUT/shuttle.log"; exit 1; fi

echo "== sanity: a planted false assertion must be found"
if shuttle "-DFORMAL_SANITY" "$OUT/sanity.log"; then echo "   the false assertion was proved, so the setup is broken"; exit 1; fi
grep -q "model found for base case: FAIL" "$OUT/sanity.log" && echo "   counterexample found as expected: PASS"
