<!---
This file is used to generate the project datasheet.
-->

## How it works

Linecraft is a reprogrammable protocol emulator. It has four identical engines called shuttles. Each shuttle runs a program from a shared 64 word instruction memory, ticks at a rate set by its own fractional clock divider, and moves bits between the pins and a pair of 4 byte FIFOs with exact cycle timing.

Each shuttle also has two programmable blocks in its data path. The line coder is a finite state transducer with 16 states stored in a 32 entry table, so NRZI, Manchester, bit stuffing and their combinations are data that the host loads. The CRC unit is a Galois LFSR with programmable width, polynomial, initial value and final XOR. A divider option resynchronises a shuttle on every edge of a chosen input pin, which works as a simple clock recovery loop for receivers.

The full instruction set and register map are in `docs/spec.md` in the repository.

## How to test

The RP2350 on the demo board talks to the chip over SPI mode 0 on `ui_in[7]` (CSn), `ui_in[6]` (SCK), `ui_in[5]` (MOSI) and `uo_out[7]` (MISO), with SCK at most one eighth of the chip clock. Each transaction is a two byte header `{rw, addr[14:8]}, addr[7:0]` followed by data bytes. Reading address 0 returns `0x4C`.

To run a protocol, write the program into the instruction memory at `0x400`, write the shuttle configuration at `0x100 + 0x40 * shuttle`, optionally load a line coder table at `0x200 + 0x20 * shuttle`, then set the shuttle's bit in the enable register at `0x001`. Data goes in and out through the FIFO registers at `0x008` to `0x00B`. The repository has an assembler and example programs for UART, SPI, I2C and USB low speed.

## External hardware

Whatever the chosen protocol needs. I2C needs pull-up resistors on the two pins used (pins 8 to 15 only, since only the bidirectional pins can be open drain). USB low speed needs the usual series resistors and a 1.5 kilohm pull-up on D-.
