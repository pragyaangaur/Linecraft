#!/bin/sh
# Run the formal proofs with Yosys and yosys-smtbmc (z3 by default).
# Each proof is a bounded check from reset followed by k-induction, so a pass
# means the property holds on every cycle of every run.
set -e
cd "$(dirname "$0")/.."
SOLVER=${SOLVER:-z3}
DEPTH=${DEPTH:-6}
OUT=${OUT:-formal/build}
mkdir -p "$OUT"

prove() {
    name=$1; top=$2; define=$3; shift 3
    yosys -q -p "read_verilog -formal -D$define $*; prep -top $top; async2sync; dffunmap; write_smt2 -wires $OUT/$name.smt2"
    echo "== $name: base case, $DEPTH cycles from reset"
    yosys-smtbmc -s "$SOLVER" --presat -t "$DEPTH" "$OUT/$name.smt2" > "$OUT/$name.bmc.log" || { tail -20 "$OUT/$name.bmc.log"; exit 1; }
    tail -1 "$OUT/$name.bmc.log"
    echo "== $name: induction, depth $DEPTH"
    yosys-smtbmc -s "$SOLVER" -i -t "$DEPTH" "$OUT/$name.smt2" > "$OUT/$name.ind.log" || { tail -20 "$OUT/$name.ind.log"; exit 1; }
    tail -1 "$OUT/$name.ind.log"
}

prove divider weft_divider FORMAL_DIVIDER src/weft_divider.v
prove shuttle weft_shuttle FORMAL_SHUTTLE src/weft_shuttle.v src/weft_divider.v
