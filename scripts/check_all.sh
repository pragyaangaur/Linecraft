#!/bin/sh
# Run every check in the repository: the Python tests, the cocotb tests, the
# formal proofs and the line code analysis. The cocotb Makefile cannot handle
# spaces in paths, so the tests run through a temporary symlink.
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LINK="$(mktemp -d)/weft"
ln -s "$ROOT" "$LINK"
cd "$LINK"

echo "== Python tests"
python3 -m pytest weft -q

echo "== cocotb tests (RTL)"
(cd test && make -B > /dev/null 2>&1 || true)
python3 -c "
import sys, xml.etree.ElementTree as ET
r = ET.parse('test/results.xml').getroot()
cases = r.findall('.//testcase')
bad = [c.get('name') for c in cases if c.find('failure') is not None or c.find('error') is not None]
print(f'{len(cases) - len(bad)} of {len(cases)} passed')
sys.exit(1 if bad or not cases else 0)
"

echo "== formal proofs"
./formal/run.sh

echo "== line code analysis"
python3 -m weft.analysis
python3 -m weft.synth
rm "$LINK"
