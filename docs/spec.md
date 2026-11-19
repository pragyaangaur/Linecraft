# Weft architecture and instruction set

This file is the reference for the chip. The Verilog in `src/` and the Python model in `weft/model.py` are both written from this file, and the tests check that the two agree cycle by cycle. When the two disagree, this file decides which one is wrong.

## Big picture

Weft has four identical engines called shuttles. Each shuttle is a small processor that runs a program from a shared instruction memory and moves bits between pins and FIFOs with exact cycle timing. A host microcontroller loads programs and talks to the shuttles over an SPI port.

Every shuttle also has two pieces of programmable hardware that sit between the pins and the shift registers.

- A **line coder**. It is a finite state transducer with 16 states, stored as a 32 entry table. It can do NRZI, Manchester, bit stuffing and bit destuffing, and combinations of them, because each of those codes is a small state machine.
- A **CRC unit**. It is a Galois LFSR with programmable width (1 to 32), polynomial, initial value and final XOR. It can compute the CRC of a USB packet, an Ethernet frame, a CAN frame or an SMBus transaction while the bits go past.

The idea behind the design is that most serial protocols are built from three layers. The bottom layer is timing, which the shuttle program and its clock divider handle. The middle layer is a line code, which is a finite state machine on bits. The top layer is an error check, which is a linear map over GF(2). Weft gives each layer its own programmable hardware.

## Clock, reset and pins

The chip runs from `clk`. All state goes to the values given below when `rst_n` is low.

There are 16 input pin bits and 16 output pin bits.

| Pin index | Input | Output |
| --- | --- | --- |
| 0 to 7 | `ui_in[0..7]` | `uo_out[0..7]` |
| 8 to 15 | `uio_in[0..7]` | `uio_out[0..7]` with `uio_oe[0..7]` |

Pins 8 to 15 are the only pins where the input and the output are the same physical wire. Open drain protocols such as I2C must use them.

The host SPI port uses four pins: `ui_in[7]` is CSn, `ui_in[6]` is SCK, `ui_in[5]` is MOSI and `uo_out[7]` is MISO. Shuttles can still read input pins 5 to 7, but writes to output pin 7 have no effect.

All 16 input bits pass through a two flop synchronizer. Shuttles only see the synchronized value `pins`. A third flop holds the previous synchronized value `pins_prev`, which is used for edge detection.

Each shuttle has its own 16 bit output value register `pv` and output enable register `pe`. For each output pin `i`, the pin is driven by the highest numbered shuttle whose `pe[i]` is 1. If no shuttle has `pe[i]` set, the output value is 0. For pins 8 to 15, `uio_oe[i-8]` is the OR of `pe[i]` over all shuttles. Pins 0 to 7 are always outputs, but a shuttle still has to set `pe[i]` to own them. All `pv` and `pe` bits reset to 0.

## Clock divider and edge resync

Each shuttle has a divider that produces a one cycle `tick` pulse. A shuttle only does anything on a tick. The divider has a 16 bit integer part `DIV_INT` and an 8 bit fractional part `DIV_FRAC`. Let `N = max(DIV_INT, 1)`. The divider holds a counter `cnt` (16 bits) and an accumulator `acc` (8 bits).

When the shuttle is disabled, `cnt = 0`, `acc = 0` and there are no ticks. When it is enabled, each clock cycle does the following.

1. If resync is enabled (`SYNC_EN = 1`) and `pins[SYNC_PIN] != pins_prev[SYNC_PIN]`, there is no tick this cycle. Set `cnt = SYNC_PHASE` and `acc = 0`.
2. Otherwise, if `cnt == 0`, there is a tick. Compute `{carry, acc} = acc + DIV_FRAC` (9 bits) and set `cnt = N - 1 + carry`.
3. Otherwise there is no tick and `cnt = cnt - 1`.

With `DIV_INT = 1` and `DIV_FRAC = 0` the shuttle ticks every cycle. The average tick period is `N + DIV_FRAC / 256` cycles. Resync is a digital phase locked loop with a hard reset on every edge. With `SYNC_PHASE` set to half a bit time, the shuttle samples the middle of each bit and lines itself up again at every transition of the incoming signal.

## Shuttle state

| Name | Width | Reset and restart value | Meaning |
| --- | --- | --- | --- |
| `pc` | 6 | `START_PC` | Program counter |
| `x`, `y` | 16 | 0 | Scratch registers |
| `osr` | 32 | 0 | Output shift register |
| `osr_left` | 6 | 0 | Bits left in `osr`. The OSR is empty when this is 0 |
| `isr` | 32 | 0 | Input shift register |
| `isr_cnt` | 6 | 0 | Bits shifted into `isr` since it was last cleared |
| `dly` | 5 | 0 | Delay ticks still to wait |
| `fst` | 4 | `FST_INIT` | Line coder state |
| `fst_err` | 1 | 0 | Sticky flag set by line coder entries with the flag bit |
| `crc` | 32 | init value | CRC register (only the low `CRC_WIDTH` bits are used) |
| `pv`, `pe` | 16 each | 0 (kept on restart) | Output values and enables |
| TX FIFO, RX FIFO | 4 entries of 8 bits each | empty | Host to shuttle and shuttle to host |

Configuration does not load `pc`, `fst` or `crc` by itself, so the host should write `RESTART` after configuring a shuttle. A restart (from the host `RESTART` register) puts every item above into its restart value, empties both FIFOs, and resets the divider (`cnt = 0`, `acc = 0`). It keeps `pv` and `pe` so that pins do not glitch. Reset (`rst_n`) does all of that and also clears `pv` and `pe`.

## Execution

On a tick, if `dly > 0`, the shuttle sets `dly = dly - 1` and does nothing else. If `dly == 0`, it tries to execute the instruction at `imem[pc]`.

- If the side-set count `SIDE_COUNT` is more than 0, the side-set bits are applied to the pins on every attempt, even if the instruction then stalls.
- If the instruction stalls, nothing else changes. It is tried again on the next tick.
- If the instruction completes, its effects are applied, `dly` is set to the instruction's delay field, and `pc` moves on. If the instruction jumped, `pc` is the jump target. Otherwise, if `pc == WRAP_TOP`, then `pc = WRAP_BOT`. Otherwise `pc = pc + 1` (mod 64).

All shuttles see shared state (pins, IRQ flags, instruction memory) as it was at the start of the cycle. When two writers change the same shared thing in the same cycle the rules are given where that thing is described.

## Instruction format

Instructions are 24 bits wide. The instruction memory holds 64 instructions shared by all four shuttles.

| Bits | Field |
| --- | --- |
| 23:20 | opcode |
| 19:15 | delay (0 to 31 ticks after the instruction completes) |
| 14:12 | side-set value. Bit `i` goes to pin `SIDE_BASE + i` for `i < SIDE_COUNT` |
| 11:0 | arguments |

Pin numbers always wrap modulo 16, so `OUT_BASE + i` means `(OUT_BASE + i) mod 16`. A count field of 0 means 32, and so does any value from 33 to 63. The same rule applies to `PULL_THRESH` and `PUSH_THRESH`.

Side-set writes `pv` bits, or `pe` bits if `SIDE_PINDIR = 1`. If an instruction writes the same pin through its main effect and through side-set, the side-set value wins.

### Opcodes

| Op | Name | Arguments |
| --- | --- | --- |
| 0 | `JMP` | `[11:8]` condition, `[5:0]` address |
| 1 | `WAIT` | `[7]` polarity, `[6:5]` source, `[3:0]` index |
| 2 | `IN` | `[11:8]` source, `[5:0]` count |
| 3 | `OUT` | `[11:8]` destination, `[5:0]` count |
| 4 | `PUSH` | `[1]` iffull, `[0]` block |
| 5 | `PULL` | `[1]` ifempty, `[0]` block |
| 6 | `MOV` | `[11:8]` destination, `[7:6]` operation, `[3:0]` source |
| 7 | `SET` | `[11:8]` destination, `[7:0]` immediate |
| 8 | `IRQ` | `[4]` clear, `[2:0]` index |
| 9 | `CTL` | `[0]` reset CRC, `[1]` reset line coder |
| 10 to 15 | `NOP` | none. Delay and side-set still apply |

### JMP

The jump is taken if the condition holds. `X--` and `Y--` decrement the register whether or not the jump is taken, using the value before the decrement for the test.

| Code | Condition |
| --- | --- |
| 0 | always |
| 1 | `!X`: `x == 0` |
| 2 | `X--`: `x != 0`, then `x = x - 1` |
| 3 | `!Y`: `y == 0` |
| 4 | `Y--`: `y != 0`, then `y = y - 1` |
| 5 | `MORE`: an `OUT` would not stall for lack of data. True when `osr_left != 0`, or `AUTOPULL = 1` and the TX FIFO is not empty, or `FPEND` is true. It is meant for line coder transmit loops, which must keep a fixed number of ticks per symbol up to the last stuffed bit |
| 6 | `PIN`: `pins[JMP_PIN] == 1` |
| 7 | `!PIN`: `pins[JMP_PIN] == 0` |
| 8 | `!OSRE`: `osr_left != 0` |
| 9 | `TXNE`: TX FIFO is not empty |
| 10 | `MATCH`: `(pins & MATCH_MASK) == MATCH_VAL` |
| 11 | `!MATCH` |
| 12 | `CRCOK`: low `CRC_WIDTH` bits of `crc` equal `CRC_CHECK` (also masked to the width) |
| 13 | `FERR`: `fst_err == 1` |
| 14 | `FPEND`: the line coder has output that does not need input. This is true when the two table entries for the current state are identical and have the transfer bit clear, so the next step does not depend on the data bit. Note that an all zero table makes this true in state 0 |
| 15 | `RXNF`: RX FIFO is not full |

### WAIT

`WAIT` stalls until a value equals the polarity bit.

| Source | Value tested |
| --- | --- |
| 0 | `pins[index]` |
| 1 | `pins[IN_BASE + index]` |
| 2 | IRQ flag `index[2:0]`. If polarity is 1, the flag is cleared when the wait completes |
| 3 | always 0 at polarity 0 (so `WAIT 0` on source 3 completes at once) |

### IN

`IN` shifts `n` data bits into `isr`. The data bits are `d[0] .. d[n-1]`.

| Source | Data |
| --- | --- |
| 0 `PINS` | `d[i] = pins[IN_BASE + i]` |
| 1 `X` | low `n` bits of `x` (zero above bit 15) |
| 2 `Y` | low `n` bits of `y` |
| 3 `NULL` | zeros |
| 4 `ISR` | low `n` bits of `isr` |
| 5 `OSR` | low `n` bits of `osr` |
| 6 `FST` | line coder receive step (see below). The count must be 1 |

If `IN_RIGHT = 1`, then `isr = (isr >> n) | (d << (32 - n))`. Otherwise `isr = (isr << n) | d`. Then `isr_cnt = min(isr_cnt + n, 32)`.

Autopush. If `AUTOPUSH = 1` and the new `isr_cnt >= PUSH_THRESH`, the shuttle pushes in the same cycle. If the RX FIFO is full, the whole instruction stalls and nothing changes. A `IN FST` step that emits no bit never pushes and never stalls.

### OUT

`OUT` shifts `n` bits out of `osr`. If `OUT_RIGHT = 1`, then `d = osr[n-1:0]` and `osr = osr >> n`. Otherwise `d = osr >> (32 - n)` and `osr = osr << n`. Then `osr_left = max(osr_left - n, 0)`.

Autopull. If `AUTOPULL = 1` and `osr_left == 0` when `OUT` starts, the shuttle first loads `osr` from the TX FIFO (as `PULL` would) and then shifts from the new value, all in the same cycle. If the TX FIFO is empty, the instruction stalls. The one exception is `OUT FST` when `FPEND` is true: then no input bit is needed, and it runs without pulling.

| Destination | Effect |
| --- | --- |
| 0 `PINS` | `pv[OUT_BASE + i] = d[i]` |
| 1 `X` | `x = d` (low 16 bits) |
| 2 `Y` | `y = d` |
| 3 `NULL` | nothing |
| 4 `PINDIRS` | `pe[OUT_BASE + i] = d[i]` |
| 5 `PC` | jump to `d[5:0]` |
| 6 `ISR` | `isr = d`, `isr_cnt = n` |
| 7 `FST` | line coder transmit step (see below). The count must be 1 |

### PUSH and PULL

`PUSH` sends one byte from `isr` to the RX FIFO. The byte is `isr[31:24]` if `IN_RIGHT = 1` and `isr[7:0]` otherwise. Then `isr = 0` and `isr_cnt = 0`. If `iffull` is set and `isr_cnt < PUSH_THRESH`, the instruction does nothing. If the FIFO is full, it stalls when `block` is set, and when `block` is clear it drops the byte (still clearing `isr`).

`PULL` loads one byte from the TX FIFO into `osr`. The byte `b` goes to `osr = b` if `OUT_RIGHT = 1` and to `osr = b << 24` otherwise. Then `osr_left = PULL_THRESH`. If `ifempty` is set and `osr_left != 0`, the instruction does nothing. If the FIFO is empty, it stalls when `block` is set, and when `block` is clear it copies `osr = x` and sets `osr_left = PULL_THRESH`.

### MOV

The source value `v` is 32 bits. The operation is applied to `v` and the result goes to the destination.

| Source | Value |
| --- | --- |
| 0 `PINS` | `pins` rotated right by `IN_BASE`, zero extended |
| 1 `X`, 2 `Y` | zero extended register |
| 3 `NULL` | 0 |
| 4 `ISR`, 5 `OSR` | the register |
| 6 `CRC` | `(crc XOR xorout)` masked to `CRC_WIDTH` bits |
| 7 `STATUS` | `{rx_full, tx_empty, fst_err, fst[3:0]}` in bits 6:0 |

| Operation | Result |
| --- | --- |
| 0 | `v` |
| 1 | `~v` |
| 2 | `v` with its 32 bits reversed |
| 3 | `v` |

| Destination | Effect |
| --- | --- |
| 0 `X`, 1 `Y` | low 16 bits |
| 2 `ISR` | `isr = v`, `isr_cnt = 0` |
| 3 `OSR` | `osr = v`, `osr_left = 32` |
| 4 `PC` | jump to `v[5:0]` |

`MOV OSR, CRC` is a special case that loads the CRC ready to send. Let `r` be `(crc XOR xorout)` masked to `W = CRC_WIDTH` bits. The bits must leave in the order `r[W-1]` first. So if `OUT_RIGHT = 1` then `osr` is `r` with its low `W` bits reversed, and otherwise `osr = r << (32 - W)`. `osr_left = W`. The operation field is ignored.

### SET

| Destination | Effect |
| --- | --- |
| 0 `PINS` | `pv[SET_BASE + i] = imm[i]` for `i < SET_COUNT` |
| 1 `X`, 2 `Y` | register = `imm` zero extended |
| 4 `PINDIRS` | `pe[SET_BASE + i] = imm[i]` for `i < SET_COUNT` |

### IRQ

There are 8 IRQ flags shared by all shuttles and the host. `IRQ` sets flag `index` (or clears it if the clear bit is 1). In one cycle, the new flags are `(flags AND NOT clears) OR sets`, where `clears` and `sets` collect every shuttle instruction, every completing `WAIT` on an IRQ, and every host write.

### CTL

Bit 0 sets `crc` to the CRC init value. Bit 1 sets `fst = FST_INIT` and `fst_err = 0`.

## Line coder

The line coder table has 32 entries of 7 bits. Entry `T[s][b]` sits at index `2*s + b`. Its fields are `next = e[3:0]`, `out = e[4]`, `xfer = e[5]` and `flag = e[6]`.

Each step reads one entry and always moves to `fst = next`. If `flag` is 1, it sets `fst_err = 1`.

On transmit (`OUT FST, 1`), the input `b` is the next data bit, which is `osr[0]` if `OUT_RIGHT = 1` and `osr[31]` otherwise. The entry's `out` bit goes to pin `OUT_BASE`. If `FST_DIFF = 1`, the inverted bit also goes to pin `OUT_BASE + 1`, which gives a differential pair such as USB D- and D+. If `xfer` is 1, the bit is consumed: the OSR shifts by one, `osr_left` drops by one (not below 0), and the bit `b` is fed to the CRC if `CRC_SRC` is transmit. If `xfer` is 0, the OSR does not change.

On receive (`IN FST, 1`), the input `b` is `pins[IN_BASE]`. If `xfer` is 1, the bit `out` is shifted into `isr` as a one bit `IN`, with autopush, and is fed to the CRC if `CRC_SRC` is receive. If `xfer` is 0, nothing goes into `isr`.

So each transmit step sends exactly one line symbol and uses zero or one data bit, and each receive step reads exactly one line symbol and produces zero or one data bit. This one rule covers NRZI, Manchester, bit stuffing and their combinations.

## CRC unit

The CRC register is a Galois LFSR in normal (non reflected) form. Let `W = CRC_WIDTH` (0 means 32) and `mask = 2^W - 1`. For each data bit `b`:

```
fb  = crc[W-1] XOR b
crc = ((crc << 1) AND mask) XOR (fb ? CRC_POLY : 0)
```

Bits are fed in wire order. A protocol that sends bytes least significant bit first and uses a "reflected" CRC is handled by this same rule, because feeding wire order bits into a normal CRC gives the reflected CRC with its bits reversed. `MOV OSR, CRC` already sends them in the right order.

The init value is all ones if `CRC_INIT_ONES = 1` and 0 otherwise. After `CRC_WIDTH` changes, the CRC register is undefined until the next CRC reset (`CTL` or a restart), because the hardware keeps the register aligned to the current width. `xorout` is all ones (masked) if `CRC_XOROUT_ONES = 1` and 0 otherwise.

Which bits are fed is set by `CRC_SRC`. 0 feeds nothing. 1 feeds data bits leaving through `OUT`. 2 feeds data bits entering through `IN`. For an `IN` or `OUT` of `n` bits, the first `min(n, 8)` bits are fed in shift order. For a right shift that order is `d[0], d[1], ...`, and for a left shift it is `d[n-1], d[n-2], ...`. A line coder step feeds its one data bit only when `xfer` is 1.

The CRC update lands one clock cycle after the instruction that fed it. So an instruction that reads the CRC (`MOV` from `CRC` or `JMP CRCOK`) sees the bits fed by instructions that completed at least two cycles earlier, and does not see bits fed in the cycle just before it. With a divider of 2 or more this never matters, because instructions are at least two cycles apart. A `CTL` CRC reset in the same cycle as a pending update wins over the update.

## Host SPI port

The port is an SPI target in mode 0 (data sampled on the rising edge of SCK, most significant bit first). SCK must be at most `clk / 8`. A transaction starts when CSn goes low and ends when it goes high.

The first byte is `{rw, a[14:8]}` and the second is `a[7:0]`. `rw = 1` means write. After that each byte is one data byte. The address goes up by one after every data byte, except at the FIFO addresses `0x008` to `0x00B`, where it stays the same.

For a read, the data byte is chosen when the previous byte ends, and MISO shows its bits during the next byte. A read with a side effect (popping the RX FIFO) only takes effect if the host clocks at least one bit of that byte. MISO is 0 during the two header bytes.

### Register map

| Address | Access | Meaning |
| --- | --- | --- |
| `0x000` | R | ID, reads `0x4C` |
| `0x001` | R/W | `ENABLE[3:0]`, one bit per shuttle |
| `0x002` | W | `RESTART[3:0]`, write 1 to restart a shuttle |
| `0x003` | R/W1C | IRQ flags. Writing 1 clears a flag |
| `0x004` | W1S | Writing 1 sets an IRQ flag |
| `0x005` | R | FIFO status: `[3:0]` TX not full, `[7:4]` RX not empty |
| `0x006` | R | RX FIFO levels (0 to 4): shuttle 0 in `[2:0]`, shuttle 1 in `[6:4]` |
| `0x007` | R | RX FIFO levels: shuttle 2 in `[2:0]`, shuttle 3 in `[6:4]` |
| `0x008 + e` | R/W | Write pushes a byte into the TX FIFO of shuttle `e`. Read pops its RX FIFO (0 if empty). Writes to a full FIFO are dropped |
| `0x100 + 0x40*e + k` | W | Shuttle `e` configuration register `k` (table below) |
| `0x200 + 0x20*e + j` | W | Shuttle `e` line coder entry `j` (7 bits) |
| `0x400 + 4*i + b` | W | Instruction `i`, byte `b`. Bytes 0 and 1 are held. Writing byte 2 stores `{byte2, byte1, byte0}` as instruction `i`. Byte 3 is ignored |

Shuttle configuration registers:

| `k` | Name | Bits |
| --- | --- | --- |
| `0x00`, `0x01` | `DIV_INT` | 16 bits, low byte first |
| `0x02` | `DIV_FRAC` | 8 |
| `0x03` | `IN_BASE` | 4 |
| `0x04` | `OUT_BASE` | 4 |
| `0x05` | `SET_BASE` | 4 |
| `0x06` | `SET_COUNT` | 4 (0 to 8) |
| `0x07` | `SIDE_BASE` | 4 |
| `0x08` | `SIDE_COUNT` in `[1:0]` (0 to 3), `SIDE_PINDIR` in `[2]` | |
| `0x09` | `OUT_RIGHT` `[0]`, `IN_RIGHT` `[1]`, `AUTOPULL` `[2]`, `AUTOPUSH` `[3]`, `FST_DIFF` `[4]` | |
| `0x0A` | `PULL_THRESH` | 6 (1 to 32) |
| `0x0B` | `PUSH_THRESH` | 6 (1 to 32) |
| `0x0C` | `WRAP_BOT` | 6 |
| `0x0D` | `WRAP_TOP` | 6 |
| `0x0E` | `START_PC` | 6 |
| `0x0F` | `JMP_PIN` | 4 |
| `0x10` | `SYNC_EN` | 1 |
| `0x11` | `SYNC_PIN` | 4 |
| `0x12`, `0x13` | `SYNC_PHASE` | 16 |
| `0x14` | `FST_INIT` | 4 |
| `0x15` | `CRC_SRC` `[1:0]`, `CRC_INIT_ONES` `[2]`, `CRC_XOROUT_ONES` `[3]` | |
| `0x16` | `CRC_WIDTH` | 6 (0 means 32) |
| `0x18` to `0x1B` | `CRC_POLY` | 32, low byte first |
| `0x1C` to `0x1F` | `CRC_CHECK` | 32, low byte first |
| `0x20`, `0x21` | `MATCH_MASK` | 16 |
| `0x22`, `0x23` | `MATCH_VAL` | 16 |

All configuration registers reset to 0, except `DIV_INT` which resets to 1, and `PULL_THRESH` and `PUSH_THRESH` which reset to 8. A threshold of 0 means 32. Changing a register while its shuttle runs is allowed and takes effect on the next cycle.
