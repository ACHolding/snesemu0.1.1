# SNESemu 0.1.1

Single-file Super Nintendo (65C816) emulator in Python 3.10+.

**No ROMs are bundled.** You must supply ROM files you legally own (homebrew, test ROMs you created, or dumps of cartridges you possess).

## Features

- Full **256/256 official 65C816 opcode** jump-table dispatch (native + emulation modes)
- LoROM / HiROM / ExHiROM cartridge mapping with SMC header stripping
- Reset / NMI / IRQ vectors, WRAM mirrors, open-bus behavior
- Scanline PPU (Modes 0–7), HDMA, color math stubs
- SPC700 port bridge for APU boot handshake
- DSP / Cx4 / SA-1 / Super FX co-processor stubs
- Portable save states (`.s9state`) and battery SRAM (`.srm`)

## Quick start

```bash
# Built-in smoke tests (no external ROM required)
python3 snesemu0.1.1.py --self-test

# Run with a ROM you legally own
python3 snesemu0.1.1.py path/to/your_rom.sfc

# Headless-friendly: disable audio
python3 snesemu0.1.1.py --no-sound path/to/test.sfc
```

Alternate entry point: `>snesemu.py` (kept in sync with `snesemu0.1.1.py`).

## Validation with legal test ROMs

Recommended suites ( obtain from their authors; do not use pirated commercial dumps ):

| Suite | Purpose |
| :--- | :--- |
| **Blargg 65C816 CPU tests** | Opcode, flag, and decimal-mode conformance |
| **Anomie SNES tests** | CPU / DMA / PPU register behavior |
| **240p Test Suite** | Video timing and lag (homebrew) |
| **`--self-test`** | Built-in mapper, boot, MVN, decimal ADC, and PPU smoke checks |

Example workflow:

```bash
python3 snesemu0.1.1.py --self-test          # 31 internal checks
python3 snesemu0.1.1.py --no-sound cpu_test.sfc   # your legal test ROM
```

## Legal notice

This project is an educational emulator. It does not include copyrighted game data. Loading `.sfc` / `.smc` files is provided so owners can run software they are entitled to use. Do not use this emulator to play ROMs you did not legally obtain.

## Remaining gaps (honest)

- SPC700 / S-DSP audio is stubbed (boot handshake only; no cycle-accurate BRR)
- SA-1, Super FX, and SDD-1 are bus stubs — titles requiring them will not run correctly
- PPU lacks offset-per-tile, pseudo-hires, and full window/color-math priority
- CPU timing is scanline-budgeted, not cycle-exact per access
- No netplay, rewind, or shader pipeline

## Files

| File | Role |
| :--- | :--- |
| `snesemu0.1.1.py` | Canonical single-file emulator |
| `>snesemu.py` | Synced alias / alternate entry point |
| `snesemu_0.1_guide.md` | Extended architecture notes |

---
*AC Holding Retro Emulation Division*
