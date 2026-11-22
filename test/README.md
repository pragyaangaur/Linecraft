# Pin level tests

These [cocotb](https://docs.cocotb.org/en/stable/) tests drive Linecraft only through its pins and its SPI host port, in the same way the RP2350 on the Tiny Tapeout demo board will.

| File | Contents |
| --- | --- |
| `test.py` | Basic tests of the ID register, the enable register, a FIFO loopback and a hand-encoded UART program |
| `test_protocols.py` | One test for each protocol, checked against the peer models |
| `test_lockstep.py` | Random programs and pin inputs, compared cycle by cycle with the Python model in `linecraft/model.py` |
| `peers.py` | Protocol peers and decoders written from each protocol's own specification, sharing no code with the toolchain |
| `linecraft_host.py` | The SPI host driver used by every test |
| `tb.v` | The testbench wrapper around `tt_um_pragyaangaur_linecraft` |

You need Icarus Verilog and the packages in `requirements.txt`. Make cannot handle a path with spaces in it, so if the repository sits in such a path, run the tests through a symlink without spaces.

```sh
make -B
```

This runs the RTL simulation of all three modules. The gate level simulation runs `test` and `test_protocols` on the hardened netlist. Copy the netlist from the hardening run to `gate_level_netlist.v`, set `PDK_ROOT`, and run:

```sh
make -B GATES=yes
```

The Tiny Tapeout workflow does this in CI, and `../scripts/postsynth_sim.sh` does it locally with a Yosys netlist instead. Waveforms are written to `tb.fst`, which opens in GTKWave with `gtkwave tb.fst tb.gtkw` or in Surfer with `surfer tb.fst`.
