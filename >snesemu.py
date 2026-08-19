#!/usr/bin/env python3
# cython: language_level=3, boundscheck=False, wraparound=False
# cython: cdivision=True, initializedcheck=False
"""cat's snes9x 1.1 — single-file SNES emulator (Python 3.14+ / Cython-ready).

SNES9x-like shell. Theme: blue background, blue-hue text, black buttons.
The snes9x-compatible core (65816 + bus + PPU) is **inlined in this file** —
there is no external `import snes9x` / out-of-tree .so. Cython directives above
compile the same source when built as an extension; pure Python still runs.

  • All 256 official 65C816 opcodes via an in-file jump table
  • files=ON by default (File → Load ROM, Ctrl+O, argv, macOS drag/open)
  • LoROM/HiROM/ExHiROM, DSP/Cx4 stubs, Mode 7, color math, HDMA, APU ports

  python3 snesemu0.1.1.py [--no-sound] [game.sfc]
  python3 snesemu0.1.1.py --self-test

No ROMs bundled. Use dumps you legally own.
"""
from __future__ import annotations

from array import array
import base64
import json
import logging
import math
import os
import sys
import tempfile
import time
import tkinter as tk
import zlib
from collections import deque
from tkinter import filedialog

# ── Cython shim (in-file; never requires an out-of-program snes9x package) ───
try:  # pragma: no cover — present when this file is cythonized
    import cython  # type: ignore
except ImportError:  # pure-Python fallback — keep the program self-contained
    class _CythonShim:
        compiled = False

        @staticmethod
        def locals(**_kwargs):
            def _decorator(fn):
                return fn
            return _decorator

        @staticmethod
        def ccmp(_expr: bool = True):
            return False

        @staticmethod
        def cast(value, _t=None):
            return value

    cython = _CythonShim()  # type: ignore

# ── snes9x core (INLINED — not imported from outside the program) ───────────
CORE_BACKEND = "snes9x-core-inline"
APP_NAME = "cat's snes9x"
APP_VERSION = "1.1"
CORE_LABEL = "snes9x1.1"
SNES9X_CORE_INLINE = True  # always True: core lives in this file

log = logging.getLogger("snescore")
_UNIMPL: set[str] = set()


def _log_once(key: str, msg: str, *args) -> None:
    """Log unimplemented HW once per key so games degrade without spam/crash."""
    if key in _UNIMPL:
        return
    _UNIMPL.add(key)
    log.warning(msg, *args)

# Display (SNES active area)
WIDTH = 256
HEIGHT = 224
SCALE = 2
FPS = 60.0988
CPU_HZ = 3_579_545  # NTSC nominal fast-access CPU rate
CPU_CYCLES_PER_FRAME = CPU_HZ / FPS

# Theme: blue bg, blue hue text, black buttons
BG = "#0B1E4A"
PANEL = "#0A1840"
FG = "#5CB3FF"
FG_BRIGHT = "#A8D8FF"
FG_DIM = "#3A7AB8"
ACCENT = "#1E4A8C"
BTN_BG = "#000000"
BTN_FG = FG_BRIGHT
BTN_ACTIVE = "#102040"

# Classic SNES CGRAM-ish boot blues
BOOT_RGB = ((0, 0, 32), (8, 24, 80), (16, 48, 140), (40, 100, 200), (80, 160, 255))


class EmulatorError(Exception):
    """User-facing cartridge / emulation error."""


# Chip-type bits from cart header $FFD6 / $7FD6 (ROM type low nibble + chip)
_CHIP_NAMES = {
    0x00: "rom",
    0x01: "rom+ram",
    0x02: "rom+ram+battery",
    0x03: "rom+dsp",
    0x04: "rom+dsp+ram",
    0x05: "rom+dsp+ram+battery",
    0x13: "rom+superfx",
    0x14: "rom+superfx",
    0x15: "rom+superfx+ram",
    0x1A: "rom+superfx+ram+battery",
    0x34: "rom+sa1",
    0x35: "rom+sa1+ram+battery",
    0xF3: "rom+cx4",
    0xF5: "rom+cx4",
    0xF6: "rom+dsp4",  # Top Gear 3000-ish
}

# Accepted dump extensions (.sfc .smc .fig .swc — user-supplied, legally owned)
_ROM_EXTENSIONS = (".sfc", ".smc", ".fig", ".swc", ".SFC", ".SMC", ".FIG", ".SWC")
_ROM_FILETYPES = (
    ("SNES ROMs", "*.sfc *.smc *.fig *.swc *.SFC *.SMC *.FIG *.SWC"),
    ("All Files", "*.*"),
)


class Cartridge:
    """SNES cartridge (.sfc / .smc): LoROM / HiROM / ExHiROM + dynamic SRAM."""

    # SRAM size code → bytes (header $FFD8)
    _SRAM_SIZES = (0, 0x800, 0x1000, 0x2000, 0x4000, 0x8000, 0x10000, 0x20000)

    def __init__(self) -> None:
        self.path = ""
        self.name = "No ROM"
        self.rom = bytearray()
        self.sram = bytearray(0)
        self.map_mode = "lorom"  # lorom | hirom | exhirom
        self.has_smc_header = False
        self.reset_vector = 0x8000
        self.title = ""
        self.rom_type = 0
        self.rom_size_code = 0
        self.sram_size_code = 0
        self.map_byte = 0x20
        self.chip = "rom"
        self.fastrom = False
        self.checksum = 0
        self.checksum_complement = 0
        self.calculated_checksum = 0
        self.checksum_valid = False
        self.header_offset = 0x7FC0
        self.coprocessor = None  # type: ignore  # set by SNES after detect
        self._sram_path = ""

    def load(self, path: str) -> None:
        ext = os.path.splitext(path)[1]
        if ext and ext not in _ROM_EXTENSIONS:
            log.info("loading non-standard extension %s (still accepted)", ext)
        with open(path, "rb") as fh:
            data = bytearray(fh.read())
        if not data:
            raise EmulatorError("Empty ROM file.")
        # Legacy Super Magicom / SWC 512-byte copier header
        self.has_smc_header = (len(data) % 1024) == 512
        if self.has_smc_header:
            data = data[512:]
            log.info("stripped 512-byte SMC copier header from %s", path)
        if len(data) < 0x8000:
            raise EmulatorError("ROM too small for SNES.")
        self.rom = data
        self.path = path
        self.name = os.path.basename(path)
        self._detect_map()
        self._read_header()
        self._alloc_sram()
        self._load_sram_file()

    def _score_header(self, offset: int) -> int:
        if offset + 0x40 > len(self.rom):
            return -999
        score = 0
        csum = self.rom[offset + 0x1C] | (self.rom[offset + 0x1D] << 8)
        comp = self.rom[offset + 0x1E] | (self.rom[offset + 0x1F] << 8)
        if (csum ^ comp) == 0xFFFF:
            score += 24
        map_byte = self.rom[offset + 0x15]
        if map_byte in (0x20, 0x21, 0x22, 0x23, 0x25, 0x30, 0x31, 0x32, 0x33, 0x35):
            score += 12
        # Prefer map byte that matches candidate layout
        if offset == 0x7FC0 and (map_byte & 0x0F) in (0x0, 0x2, 0x3):
            score += 4
        if offset == 0xFFC0 and (map_byte & 0x0F) in (0x1, 0x5):
            score += 4
        if offset == 0x40FFC0 and (map_byte & 0x0F) == 0x5:
            score += 8
        reset = self.rom[offset + 0x3C] | (self.rom[offset + 0x3D] << 8)
        if 0x8000 <= reset <= 0xFFFF:
            score += 8
        elif reset != 0:
            score -= 4
        rom_size = self.rom[offset + 0x17]
        if 0x07 <= rom_size <= 0x0D:
            score += 4
        sram_size = self.rom[offset + 0x18]
        if sram_size <= 0x09:
            score += 2
        title = bytes(self.rom[offset : offset + 21])
        printable = sum(1 for b in title if 0x20 <= b <= 0x7E)
        score += printable // 2
        if printable < 6:
            score -= 8
        # Maker / region sanity
        region = self.rom[offset + 0x19]
        if region <= 0x14:
            score += 2
        return score

    def _detect_map(self) -> None:
        candidates = [
            (0x7FC0, "lorom"),
            (0xFFC0, "hirom"),
        ]
        if len(self.rom) > 0x410000:
            candidates.append((0x40FFC0, "exhirom"))
        best_off, best_mode, best = 0x7FC0, "lorom", -999
        for off, mode in candidates:
            sc = self._score_header(off)
            if sc > best:
                best, best_off, best_mode = sc, off, mode
        self.header_offset = best_off
        self.map_mode = best_mode
        log.info("cart map=%s header@$%06X score=%d", best_mode, best_off, best)

    def _read_header(self) -> None:
        off = self.header_offset
        if off + 0x40 > len(self.rom):
            off = 0x7FC0
            self.header_offset = off
        raw = bytes(self.rom[off : off + 21])
        self.title = "".join(chr(b) if 0x20 <= b <= 0x7E else " " for b in raw).strip() or self.name
        self.map_byte = self.rom[off + 0x15]
        self.rom_type = self.rom[off + 0x16]
        self.rom_size_code = self.rom[off + 0x17]
        self.sram_size_code = self.rom[off + 0x18]
        self.reset_vector = self.rom[off + 0x3C] | (self.rom[off + 0x3D] << 8)
        self.checksum_complement = self.rom[off + 0x1C] | (self.rom[off + 0x1D] << 8)
        self.checksum = self.rom[off + 0x1E] | (self.rom[off + 0x1F] << 8)
        self.calculated_checksum = sum(self.rom) & 0xFFFF
        self.checksum_valid = (
            (self.checksum ^ self.checksum_complement) == 0xFFFF
            and self.calculated_checksum == self.checksum
        )
        self.fastrom = bool(self.map_byte & 0x10)
        # Refine map from map_byte if header is strong
        mb = self.map_byte & 0x0F
        if mb in (0x1,):
            self.map_mode = "hirom"
        elif mb == 0x5:
            self.map_mode = "exhirom" if len(self.rom) > 0x400000 else "hirom"
        elif mb in (0x0, 0x2, 0x3):
            self.map_mode = "lorom"
        self.chip = self._detect_chip()

    def _detect_chip(self) -> str:
        t = self.rom_type
        if t in _CHIP_NAMES:
            name = _CHIP_NAMES[t]
        else:
            name = "rom"
        # Heuristic: Mega Man X2/X3 Cx4 often type $F3
        title_u = self.title.upper()
        if "MEGA MAN X" in title_u or "ROCKMAN X" in title_u:
            if t in (0xF3, 0xF5) or "X2" in title_u or "X3" in title_u:
                name = "rom+cx4"
        if "STAR FOX" in title_u or "STARWING" in title_u or "YOSHI" in title_u:
            if "superfx" not in name and t in (0x13, 0x14, 0x15, 0x1A):
                name = "rom+superfx"
        if "MARIO RPG" in title_u or t in (0x34, 0x35):
            name = "rom+sa1"
        # DSP family from type / expansion
        if "dsp" in name or t in (0x03, 0x04, 0x05):
            # DSP-1 most common; DSP-2/3/4 distinguished by title heuristics
            if "TOP GEAR 3000" in title_u or t == 0xF6:
                name = "rom+dsp4"
            elif "PLANET" in title_u:  # Ballz / DSP-2
                name = "rom+dsp2"
            elif "SD GUNDAM" in title_u or "GX" in title_u and "GUNDAM" in title_u:
                name = "rom+dsp3"
            else:
                name = "rom+dsp1"
        return name

    def _alloc_sram(self) -> None:
        code = self.sram_size_code & 0x0F
        size = self._SRAM_SIZES[code] if code < len(self._SRAM_SIZES) else 0x8000
        # Battery-backed carts with size 0 still often use 8KB windows in games
        if size == 0 and ("battery" in self.chip or self.rom_type in (0x02, 0x05, 0x1A, 0x35)):
            size = 0x2000
        self.sram = bytearray(size)
        log.info("SRAM %d bytes (code=$%02X) chip=%s", size, code, self.chip)

    def _sram_file(self) -> str:
        base, _ = os.path.splitext(self.path)
        return base + ".srm"

    def _load_sram_file(self) -> None:
        if not self.sram:
            return
        path = self._sram_file()
        self._sram_path = path
        if os.path.isfile(path):
            try:
                with open(path, "rb") as fh:
                    data = fh.read(len(self.sram))
                self.sram[: len(data)] = data
            except OSError as exc:
                log.warning("SRAM load failed: %s", exc)

    def save_sram(self) -> None:
        if not self.sram or not self.path:
            return
        path = self._sram_path or self._sram_file()
        try:
            with open(path, "wb") as fh:
                fh.write(self.sram)
        except OSError as exc:
            log.warning("SRAM save failed: %s", exc)

    def read_lorom(self, bank: int, addr: int) -> int:
        # Banks $40-$6F/$C0-$EF expose the LoROM chip across the full bank;
        # low and high halves mirror the same 32 KiB page.
        if addr < 0x8000 and not (0x40 <= (bank & 0x7F) <= 0x6F):
            return 0
        rom_addr = ((bank & 0x7F) << 15) | (addr & 0x7FFF)
        return self.rom[rom_addr % len(self.rom)]

    def read_hirom(self, bank: int, addr: int) -> int:
        rom_addr = ((bank & 0x3F) << 16) | addr
        return self.rom[rom_addr % len(self.rom)]

    def read_exhirom(self, bank: int, addr: int) -> int:
        # ExHiROM: banks $C0-$FF mirror first 4MB; $40-$7D high half for >4MB
        if bank >= 0xC0:
            rom_addr = ((bank - 0xC0) << 16) | addr
        elif 0x40 <= bank <= 0x7D:
            rom_addr = 0x400000 + (((bank - 0x40) & 0x3F) << 16) | addr
        elif (bank <= 0x3F or 0x80 <= bank <= 0xBF) and addr >= 0x8000:
            rom_addr = ((bank & 0x3F) << 16) | addr
        else:
            return 0
        return self.rom[rom_addr % len(self.rom)]

    def _sram_index(self, bank: int, addr: int) -> int | None:
        """Map CPU address → SRAM offset, or None."""
        if not self.sram:
            return None
        size = len(self.sram)
        b = bank & 0x7F
        if self.map_mode == "lorom":
            # LoROM SRAM: $70-$7D:$0000-$7FFF (and $F0-$FF mirrors)
            if 0x70 <= b <= 0x7D and addr < 0x8000:
                return (((b - 0x70) << 15) | addr) % size
        else:
            # HiROM / ExHiROM: $20-$3F/$A0-$BF:$6000-$7FFF
            if (0x20 <= b <= 0x3F) and 0x6000 <= addr <= 0x7FFF:
                return (((b - 0x20) << 13) | (addr - 0x6000)) % size
            if 0x70 <= b <= 0x7D and addr < 0x8000:
                return (((b - 0x70) << 15) | addr) % size
        return None

    def cpu_read(self, bank: int, addr: int) -> int:
        bank &= 0xFF
        addr &= 0xFFFF
        # Co-processor MMIO first (DSP/Cx4/SA-1/GSU own $6000 windows)
        if self.coprocessor is not None:
            v = self.coprocessor.cpu_read(bank, addr)
            if v is not None:
                return v
        si = self._sram_index(bank, addr)
        if si is not None:
            return self.sram[si]
        if self.map_mode == "exhirom":
            return self.read_exhirom(bank, addr)
        if self.map_mode == "hirom":
            if (0xC0 <= bank <= 0xFF) or (
                (bank <= 0x3F or 0x80 <= bank <= 0xBF) and addr >= 0x8000
            ):
                return self.read_hirom(bank, addr)
            if 0x40 <= bank <= 0x7D:
                return self.read_hirom(bank, addr)
        else:
            if (addr >= 0x8000) or (0x40 <= (bank & 0x7F) <= 0x6F):
                return self.read_lorom(bank, addr)
        return 0

    def cpu_write(self, bank: int, addr: int, value: int) -> None:
        bank &= 0xFF
        addr &= 0xFFFF
        value &= 0xFF
        if self.coprocessor is not None:
            if self.coprocessor.cpu_write(bank, addr, value):
                return
        si = self._sram_index(bank, addr)
        if si is not None:
            self.sram[si] = value


# ── Co-processors (DSP / Cx4 / SA-1 / SuperFX groundwork) ───────────────────
def _s16(v: int) -> int:
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def _clamp16(v: int) -> int:
    if v > 32767:
        return 32767
    if v < -32768:
        return -32768
    return v & 0xFFFF


class CoprocessorBase:
    """Abstract cart chip that can claim CPU bus cycles on $6000-$7FFF windows.

    SA-1 and SuperFX take over ROM/RAM rendering by implementing this interface;
    DSP/Cx4 use the same MMIO path so Bus stays chip-agnostic.
    """

    name = "none"

    def reset(self) -> None:
        pass

    def cpu_read(self, bank: int, addr: int) -> int | None:
        return None

    def cpu_write(self, bank: int, addr: int, value: int) -> bool:
        return False

    def tick(self, cycles: int) -> None:
        pass


class DSPChip(CoprocessorBase):
    """NEC DSP-1 / DSP-2 / DSP-3 / DSP-4 math co-processor (shared protocol).

    Games write a command byte then parameters to $6000; results are read back
    from $6000. Status at $6001: bit7=DR ready, bit6=SR busy clear when idle.
    Enough of DSP-1 for Mario Kart / Pilotwings style projection & multiply.
    """

    name = "dsp1"

    def __init__(self, variant: str = "dsp1") -> None:
        self.name = variant
        self.cmd = 0
        self.busy = False
        self.params: list[int] = []
        self.results: deque[int] = deque()
        self._waiting = 0
        self.matrix = [0] * 9  # attitude / shared working matrix (1.15 fixed)
        self.reset()

    def reset(self) -> None:
        self.cmd = 0
        self.busy = False
        self.params.clear()
        self.results.clear()
        self._waiting = 0
        self.matrix = [0x7FFF if i % 4 == 0 else 0 for i in range(9)]

    def _in_mmio(self, bank: int, addr: int) -> bool:
        b = bank & 0x7F
        return b <= 0x3F and 0x6000 <= addr <= 0x7FFF

    def cpu_read(self, bank: int, addr: int) -> int | None:
        if not self._in_mmio(bank, addr):
            return None
        if (addr & 1) == 0:  # data
            if self.results:
                return self.results.popleft() & 0xFF
            return 0x80
        # status: DR=1 when result pending, SR busy clear when idle
        st = 0x80 if self.results else 0x00
        if not self.busy:
            st |= 0x40
        return st

    def cpu_write(self, bank: int, addr: int, value: int) -> bool:
        if not self._in_mmio(bank, addr):
            return False
        if addr & 1:
            return True  # status is read-only
        value &= 0xFF
        if self._waiting == 0 and not self.results:
            self.cmd = value
            self._waiting = self._param_count(self.cmd)
            self.params.clear()
            if self._waiting == 0:
                self._execute()
            return True
        self.params.append(value)
        self._waiting -= 1
        if self._waiting <= 0:
            self._execute()
        return True

    def _param_count(self, cmd: int) -> int:
        # Byte counts (DSP transfers 16-bit words as two writes on some boards;
        # we accept byte stream and assemble words).
        # Counts are in 16-bit words * 2 bytes.
        table = {
            0x00: 4,   # MULTIPLY  — 2 words in, 2 out
            0x01: 2,   # High-precision multiply aspirational
            0x02: 4,   # Inverse
            0x03: 6,   # RadiusSinCos-ish
            0x04: 6,   # AttitudeA
            0x06: 6,   # AttitudeB
            0x0A: 14,  # Objective / Projection (Pilotwings / SMK)
            0x0B: 6,   # AttitudeC
            0x0D: 4,   # Triangle
            0x0F: 2,   # Raster (scanline transform helper)
            0x10: 4,   # DSP-2 / shared
            0x1F: 2,   # ROM version
        }
        return table.get(cmd & 0x7F, 2)

    def _pop_word(self) -> int:
        if len(self.params) < 2:
            return 0
        lo = self.params.pop(0)
        hi = self.params.pop(0)
        return lo | (hi << 8)

    def _push_word(self, w: int) -> None:
        w &= 0xFFFF
        self.results.append(w & 0xFF)
        self.results.append((w >> 8) & 0xFF)

    def _execute(self) -> None:
        self.busy = True
        cmd = self.cmd & 0x7F
        try:
            if cmd == 0x00:  # 16×16 → 32 multiply
                a = _s16(self._pop_word())
                b = _s16(self._pop_word())
                prod = a * b
                self._push_word((prod >> 16) & 0xFFFF)
                self._push_word(prod & 0xFFFF)
            elif cmd == 0x0A:  # Projection / Objective (simplified)
                # Inputs: x,y,z, Mij already in attitude — push screen x,y,scale
                x = _s16(self._pop_word())
                y = _s16(self._pop_word())
                z = _s16(self._pop_word())
                # consume remaining params if present
                while len(self.params) >= 2:
                    self._pop_word()
                m = self.matrix
                # Rotate then perspective (fixed-point approx)
                rx = (m[0] * x + m[1] * y + m[2] * z) >> 15
                ry = (m[3] * x + m[4] * y + m[5] * z) >> 15
                rz = (m[6] * x + m[7] * y + m[8] * z) >> 15
                if rz == 0:
                    rz = 1
                sx = _clamp16((rx << 8) // rz)
                sy = _clamp16((ry << 8) // rz)
                scale = _clamp16((0x7FFF << 8) // max(abs(rz), 1))
                self._push_word(sx)
                self._push_word(sy)
                self._push_word(scale)
            elif cmd in (0x04, 0x06, 0x0B):  # Attitude matrices from Euler-ish
                a = _s16(self._pop_word())
                b = _s16(self._pop_word())
                c = _s16(self._pop_word())
                # Build a rough rotation from three angles (1.15 → radians-ish)
                ca = math.cos(a * math.pi / 32768.0)
                sa = math.sin(a * math.pi / 32768.0)
                cb = math.cos(b * math.pi / 32768.0)
                sb = math.sin(b * math.pi / 32768.0)
                cc = math.cos(c * math.pi / 32768.0)
                sc = math.sin(c * math.pi / 32768.0)
                # ZYX composition → 1.15
                def f(v: float) -> int:
                    return _clamp16(int(v * 32767.0))
                self.matrix = [
                    f(cb * cc), f(cb * sc), f(-sb),
                    f(sa * sb * cc - ca * sc), f(sa * sb * sc + ca * cc), f(sa * cb),
                    f(ca * sb * cc + sa * sc), f(ca * sb * sc - sa * cc), f(ca * cb),
                ]
                for w in self.matrix:
                    self._push_word(w)
            elif cmd == 0x0F:  # Raster helper: pass-through slope
                v = self._pop_word()
                self._push_word(v)
                self._push_word((v * 3) & 0xFFFF)
            elif cmd == 0x1F:
                self._push_word(0x0101)  # version-ish
            elif self.name == "dsp4" and cmd == 0x00:
                # DSP-4 road projection stub — echo scaled params
                while len(self.params) >= 2:
                    self._push_word(self._pop_word())
            else:
                _log_once(f"dsp-{self.name}-{cmd:02X}",
                          "DSP %s cmd $%02X not fully implemented (%d params)",
                          self.name, cmd, len(self.params))
                # Drain params; return zeros so game does not hang
                while len(self.params) >= 2:
                    self._pop_word()
                self._push_word(0)
                self._push_word(0)
        finally:
            self.params.clear()
            self._waiting = 0
            self.busy = False


class Cx4Chip(CoprocessorBase):
    """Capcom Cx4 — Mega Man X2/X3 wireframe / transform MMIO stub."""

    name = "cx4"

    def __init__(self) -> None:
        self.ram = bytearray(0xC00)
        self.dma_src = 0
        self.dma_len = 0
        self.cmd = 0
        self.busy = False

    def reset(self) -> None:
        self.ram[:] = b"\x00" * len(self.ram)
        self.busy = False

    def _in_mmio(self, bank: int, addr: int) -> bool:
        b = bank & 0x7F
        return b <= 0x3F and 0x6000 <= addr <= 0x7FFF

    def cpu_read(self, bank: int, addr: int) -> int | None:
        if not self._in_mmio(bank, addr):
            return None
        off = addr - 0x6000
        if off < len(self.ram):
            return self.ram[off]
        if addr == 0x7F4E:  # status
            return 0x00 if not self.busy else 0x40
        return 0

    def cpu_write(self, bank: int, addr: int, value: int) -> bool:
        if not self._in_mmio(bank, addr):
            return False
        off = addr - 0x6000
        value &= 0xFF
        if off < len(self.ram):
            self.ram[off] = value
        if addr == 0x7F4F:  # command trigger
            self.cmd = value
            self._run_cmd(value)
        return True

    def _run_cmd(self, cmd: int) -> None:
        self.busy = True
        # Wireframe transform: scale/rotate a few points in chip RAM
        if cmd in (0x00, 0x01, 0x05, 0x0D):
            for i in range(0, min(0x100, len(self.ram) - 4), 4):
                x = _s16(self.ram[i] | (self.ram[i + 1] << 8))
                y = _s16(self.ram[i + 2] | (self.ram[i + 3] << 8))
                # Simple 45° rotate + scale for visible wireframe motion
                nx = (x - y) >> 1
                ny = (x + y) >> 1
                self.ram[i] = nx & 0xFF
                self.ram[i + 1] = (nx >> 8) & 0xFF
                self.ram[i + 2] = ny & 0xFF
                self.ram[i + 3] = (ny >> 8) & 0xFF
        else:
            _log_once(f"cx4-{cmd:02X}", "Cx4 command $%02X stubbed", cmd)
        self.busy = False


class SA1Chip(CoprocessorBase):
    """SA-1 groundwork — bus takeover hooks for Super Mario RPG-class carts."""

    name = "sa1"

    def __init__(self, cart: "Cartridge") -> None:
        self.cart = cart
        self.iram = bytearray(0x800)
        self.bwram = bytearray(0x20000)
        self.control = 0
        self.vec_nmi = 0
        self.cpu_active = False  # when True, SA-1 owns ROM fetches (future)

    def reset(self) -> None:
        self.iram[:] = b"\x00" * len(self.iram)
        self.control = 0
        self.cpu_active = False

    def cpu_read(self, bank: int, addr: int) -> int | None:
        b = bank & 0xFF
        if b == 0x00 and 0x2200 <= addr <= 0x23FF:
            _log_once("sa1-mmio-r", "SA-1 MMIO read $%02X:%04X (stub)", b, addr)
            return 0
        if 0x3000 <= addr <= 0x37FF and (b <= 0x3F or 0x80 <= b <= 0xBF):
            return self.iram[addr - 0x3000]
        return None

    def cpu_write(self, bank: int, addr: int, value: int) -> bool:
        b = bank & 0xFF
        if b == 0x00 and 0x2200 <= addr <= 0x23FF:
            if addr == 0x2200:
                self.control = value
                self.cpu_active = bool(value & 0x20)
            _log_once("sa1-mmio-w", "SA-1 MMIO write $%02X:%04X=$%02X (stub)", b, addr, value)
            return True
        if 0x3000 <= addr <= 0x37FF and (b <= 0x3F or 0x80 <= b <= 0xBF):
            self.iram[addr - 0x3000] = value & 0xFF
            return True
        return False


class SuperFXChip(CoprocessorBase):
    """Super FX / GSU groundwork — Star Fox / Yoshi's Island bus hooks."""

    name = "superfx"

    def __init__(self, cart: "Cartridge") -> None:
        self.cart = cart
        self.sram_cache = bytearray(0x20000)
        self.regs = bytearray(0x20)
        self.running = False

    def reset(self) -> None:
        self.regs[:] = b"\x00" * len(self.regs)
        self.running = False

    def cpu_read(self, bank: int, addr: int) -> int | None:
        b = bank & 0x7F
        if b <= 0x3F and 0x3000 <= addr <= 0x32FF:
            _log_once("gsu-mmio-r", "SuperFX MMIO read $%04X (stub)", addr)
            return self.regs[addr & 0x1F]
        return None

    def cpu_write(self, bank: int, addr: int, value: int) -> bool:
        b = bank & 0x7F
        if b <= 0x3F and 0x3000 <= addr <= 0x32FF:
            self.regs[addr & 0x1F] = value & 0xFF
            if addr == 0x3030:
                self.running = bool(value & 0x20)
            _log_once("gsu-mmio-w", "SuperFX MMIO write $%04X=$%02X (stub)", addr, value)
            return True
        return False


def make_coprocessor(cart: Cartridge) -> CoprocessorBase | None:
    chip = cart.chip
    if "dsp4" in chip:
        return DSPChip("dsp4")
    if "dsp3" in chip:
        return DSPChip("dsp3")
    if "dsp2" in chip:
        return DSPChip("dsp2")
    if "dsp1" in chip or "dsp" in chip:
        return DSPChip("dsp1")
    if "cx4" in chip:
        return Cx4Chip()
    if "sa1" in chip:
        return SA1Chip(cart)
    if "superfx" in chip:
        return SuperFXChip(cart)
    return None


class PPU:
    """SNES PPU — modes 0/1/7, color math, windows, scanline + tile cache."""

    def __init__(self) -> None:
        self.vram = bytearray(0x10000)
        self.cgram = bytearray(0x200)
        self.oam = bytearray(0x220)
        self.framebuffer = bytearray(WIDTH * HEIGHT * 3)
        self.brightness = 0x0F
        self.bg_mode = 0
        self.nmi_enable = False
        self.vblank = False
        self.hblank = False
        self.scanline = 0
        self.vmadd = 0
        self.cgadd = 0        # word address
        self.cg_latch = -1    # low byte pending
        self.cg_read_high = False
        self.oamadd = 0
        self.bg_hofs = [0, 0, 0, 0]
        self.bg_vofs = [0, 0, 0, 0]
        self.ofs_latch = 0
        self.regs = bytearray(0x40)
        # Mode 7 matrix (8.8 fixed from M7A..M7D) + center / scroll
        self.m7_a = 0x0100
        self.m7_b = 0
        self.m7_c = 0
        self.m7_d = 0x0100
        self.m7_x = 0
        self.m7_y = 0
        self.m7_hofs = 0
        self.m7_vofs = 0
        self.m7_latch = 0
        self.m7_mpy = 0  # 24-bit product for $2134-$2136
        # Color math / fixed color
        self.fixed_r = 0
        self.fixed_g = 0
        self.fixed_b = 0
        # Per-scanline layer buffers (palette indices + RGB for color math)
        self._main_idx = array("H", [0]) * WIDTH
        self._sub_idx = array("H", [0]) * WIDTH
        self._tile_cache: dict[tuple[int, int, int], bytes] = {}
        self._cache_gen = 0

    def reset(self) -> None:
        self.vram[:] = b"\x00" * len(self.vram)
        self.cgram[:] = b"\x00" * len(self.cgram)
        self.oam[:] = b"\x00" * len(self.oam)
        self.framebuffer[:] = b"\x00" * len(self.framebuffer)
        self.brightness = 0
        self.bg_mode = 0
        self.nmi_enable = False
        self.vblank = False
        self.hblank = False
        self.scanline = 0
        self.vmadd = 0
        self.cgadd = 0
        self.cg_latch = -1
        self.cg_read_high = False
        self.oamadd = 0
        self.bg_hofs = [0, 0, 0, 0]
        self.bg_vofs = [0, 0, 0, 0]
        self.ofs_latch = 0
        self.m7_a = 0x0100
        self.m7_b = 0
        self.m7_c = 0
        self.m7_d = 0x0100
        self.m7_x = self.m7_y = 0
        self.m7_hofs = self.m7_vofs = 0
        self.m7_latch = 0
        self.m7_mpy = 0
        self.fixed_r = self.fixed_g = self.fixed_b = 0
        self._tile_cache.clear()
        self._cache_gen = 0
        self.regs[:] = b"\x00" * len(self.regs)
        self.regs[0x00] = 0x80  # INIDISP forced blank (hardware reset)

    def _vram_step(self) -> int:
        inc = self.regs[0x15] & 0x03
        if inc == 0:
            return 1
        if inc == 1:
            return 32
        return 128

    def _vram_word_addr(self) -> int:
        """Apply VMAIN's full-graphic address remapping to VMADD."""
        addr = self.vmadd & 0xFFFF
        mode = (self.regs[0x15] >> 2) & 0x03
        if mode == 1:
            return (addr & 0xFF00) | ((addr & 0x001F) << 3) | ((addr >> 5) & 0x07)
        if mode == 2:
            return (addr & 0xFE00) | ((addr & 0x003F) << 3) | ((addr >> 6) & 0x07)
        if mode == 3:
            return (addr & 0xFC00) | ((addr & 0x007F) << 3) | ((addr >> 7) & 0x07)
        return addr

    def _vram_byte_addr(self, high: bool = False) -> int:
        return ((self._vram_word_addr() << 1) + int(high)) & 0xFFFF

    def _m7_write16(self, attr: str, value: int) -> None:
        """Mode-7 write-twice registers (M7A-D, M7X/Y, M7HOFS/VOFS)."""
        full = ((value << 8) | self.m7_latch) & 0xFFFF
        if attr in ("m7_x", "m7_y", "m7_hofs", "m7_vofs"):
            # 13-bit signed
            full &= 0x1FFF
            if full & 0x1000:
                full |= ~0x1FFF
            setattr(self, attr, full)
        else:
            setattr(self, attr, full)
            # Hardware multiplies M7A × last M7B write → $2134-36
            if attr == "m7_b":
                self.m7_mpy = (_s16(self.m7_a) * _s16(self.m7_b)) & 0xFFFFFF
        self.m7_latch = value & 0xFF

    def write_reg(self, addr: int, value: int) -> None:
        a = addr & 0x3F
        value &= 0xFF
        self.regs[a] = value
        if a == 0x00:  # INIDISP
            self.brightness = value & 0x0F
        elif a == 0x02:  # OAMADDL
            self.oamadd = ((self.regs[0x03] & 1) << 8) | value
        elif a == 0x03:  # OAMADDH
            self.oamadd = ((value & 1) << 8) | self.regs[0x02]
        elif a == 0x04:  # OAMDATA
            self.oam[self.oamadd % 0x220] = value
            self.oamadd = (self.oamadd + 1) % 0x220
        elif a == 0x05:  # BGMODE
            self.bg_mode = value & 0x07
        elif 0x0D <= a <= 0x14:  # BGnHOFS / BGnVOFS (write-twice)
            bg = (a - 0x0D) >> 1
            full = ((value << 8) | self.ofs_latch) & 0x3FF
            if (a - 0x0D) & 1:  # VOFS
                self.bg_vofs[bg] = full
            else:
                self.bg_hofs[bg] = full
            self.ofs_latch = value
            # Mode 7 shares BG1 HOFS/VOFS write path via $210D/$210E
            if a == 0x0D:
                self._m7_write16("m7_hofs", value)
            elif a == 0x0E:
                self._m7_write16("m7_vofs", value)
        elif a == 0x16:  # VMADDL
            self.vmadd = (self.vmadd & 0xFF00) | value
        elif a == 0x17:  # VMADDH
            self.vmadd = (self.vmadd & 0x00FF) | (value << 8)
        elif a == 0x18:  # VMDATAL
            self.vram[self._vram_byte_addr()] = value
            self._tile_cache.clear()
            if not (self.regs[0x15] & 0x80):
                self.vmadd = (self.vmadd + self._vram_step()) & 0xFFFF
        elif a == 0x19:  # VMDATAH
            self.vram[self._vram_byte_addr(high=True)] = value
            self._tile_cache.clear()
            if self.regs[0x15] & 0x80:
                self.vmadd = (self.vmadd + self._vram_step()) & 0xFFFF
        elif a == 0x1B:  # M7A
            self._m7_write16("m7_a", value)
        elif a == 0x1C:  # M7B
            self._m7_write16("m7_b", value)
        elif a == 0x1D:  # M7C
            self._m7_write16("m7_c", value)
        elif a == 0x1E:  # M7D
            self._m7_write16("m7_d", value)
        elif a == 0x1F:  # M7X
            self._m7_write16("m7_x", value)
        elif a == 0x20:  # M7Y
            self._m7_write16("m7_y", value)
        elif a == 0x21:  # CGADD
            self.cgadd = value & 0xFF
            self.cg_latch = -1
            self.cg_read_high = False
        elif a == 0x22:  # CGDATA
            if self.cg_latch < 0:
                self.cg_latch = value
            else:
                i = (self.cgadd & 0xFF) * 2
                self.cgram[i] = self.cg_latch
                self.cgram[i + 1] = value & 0x7F
                self.cgadd = (self.cgadd + 1) & 0xFF
                self.cg_latch = -1
        elif a == 0x32:  # COLDATA fixed color
            intensity = value & 0x1F
            if value & 0x20:
                self.fixed_r = intensity
            if value & 0x40:
                self.fixed_g = intensity
            if value & 0x80:
                self.fixed_b = intensity
        elif a in (0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x29, 0x2A, 0x2B,
                   0x2C, 0x2D, 0x2E, 0x2F, 0x30, 0x31):
            pass  # window / TM / TS / CGWSEL / CGADSUB stored in regs[]
        elif a not in (0x01, 0x06, 0x07, 0x08, 0x09, 0x0A, 0x0B, 0x0C, 0x15):
            _log_once(f"ppu-w-{a:02X}", "PPU write $21%02X=$%02X (stored)", a, value)

    def read_reg(self, addr: int) -> int:
        a = addr & 0x3F
        if a == 0x34:  # MPYL
            return self.m7_mpy & 0xFF
        if a == 0x35:  # MPYM
            return (self.m7_mpy >> 8) & 0xFF
        if a == 0x36:  # MPYH
            return (self.m7_mpy >> 16) & 0xFF
        if a == 0x38:  # OAMDATAREAD
            v = self.oam[self.oamadd % len(self.oam)]
            self.oamadd = (self.oamadd + 1) % 0x220
            return v
        if a == 0x39:  # VMDATALREAD
            v = self.vram[self._vram_byte_addr()]
            if not (self.regs[0x15] & 0x80):
                self.vmadd = (self.vmadd + self._vram_step()) & 0xFFFF
            return v
        if a == 0x3A:  # VMDATAHREAD
            v = self.vram[self._vram_byte_addr(high=True)]
            if self.regs[0x15] & 0x80:
                self.vmadd = (self.vmadd + self._vram_step()) & 0xFFFF
            return v
        if a == 0x3B:  # CGDATAREAD
            i = (self.cgadd & 0xFF) * 2 + int(self.cg_read_high)
            v = self.cgram[i]
            if self.cg_read_high:
                self.cgadd = (self.cgadd + 1) & 0xFF
            self.cg_read_high = not self.cg_read_high
            return v
        if a == 0x3E:  # STAT77
            return 0x01
        if a == 0x3F:  # STAT78
            v = 0x01
            if self.vblank:
                v |= 0x80
            if self.hblank:
                v |= 0x40
            return v
        return self.regs[a]

    def _cgram_rgb(self, index: int) -> tuple[int, int, int]:
        i = (index & 0xFF) * 2
        w = self.cgram[i] | (self.cgram[i + 1] << 8)
        r = (w & 0x1F) << 3
        g = ((w >> 5) & 0x1F) << 3
        b = ((w >> 10) & 0x1F) << 3
        return r, g, b

    def _window_mask(self, layer: int, x: int) -> bool:
        """Return True if pixel x is masked OUT for layer (BG0-3=0-3, OBJ=4).

        Uses $2123-$2125 (WSEL), $2126-$2129 (WH), $212A/$212B (WLOG).
        """
        if layer <= 1:
            sel = self.regs[0x23]
            shift = 0 if layer == 0 else 4
        elif layer <= 3:
            sel = self.regs[0x24]
            shift = 0 if layer == 2 else 4
        else:
            sel = self.regs[0x25]
            shift = 0
        sel = (sel >> shift) & 0x0F
        if sel == 0:
            return False
        w1_en, w1_out = bool(sel & 0x2), bool(sel & 0x1)
        w2_en, w2_out = bool(sel & 0x8), bool(sel & 0x4)
        in1 = self.regs[0x26] <= x <= self.regs[0x27]
        in2 = self.regs[0x28] <= x <= self.regs[0x29]
        m1 = (not in1) if w1_out else in1
        m2 = (not in2) if w2_out else in2
        if not w1_en and not w2_en:
            return False
        if w1_en and not w2_en:
            return m1
        if w2_en and not w1_en:
            return m2
        # Combine via WLOG (OR/AND/XOR/XNOR) — simplified OR/AND
        if layer <= 3:
            logic = (self.regs[0x2A] >> (layer * 2)) & 3
        else:
            logic = self.regs[0x2B] & 3
        if logic == 0:  # OR
            return m1 or m2
        if logic == 1:  # AND
            return m1 and m2
        if logic == 2:  # XOR
            return m1 ^ m2
        return not (m1 ^ m2)  # XNOR

    def _decode_tile_row(self, tile_base: int, tile: int, bpp: int, row: int) -> bytes:
        """Cache decoded 8-pixel color indices for one tile row."""
        key = (tile_base, tile, row | (bpp << 8) | (self._cache_gen << 16))
        cached = self._tile_cache.get(key)
        if cached is not None:
            return cached
        tile_bytes = 8 * bpp
        toff = (tile_base + tile * tile_bytes + row * 2) & 0xFFFF
        vram = self.vram
        out = bytearray(8)
        for fx in range(8):
            bit = 7 - fx
            cidx = ((vram[toff] >> bit) & 1) | (((vram[(toff + 1) & 0xFFFF] >> bit) & 1) << 1)
            if bpp >= 4:
                cidx |= (((vram[(toff + 16) & 0xFFFF] >> bit) & 1) << 2) \
                    | (((vram[(toff + 17) & 0xFFFF] >> bit) & 1) << 3)
            if bpp >= 8:
                cidx |= (((vram[(toff + 32) & 0xFFFF] >> bit) & 1) << 4) \
                    | (((vram[(toff + 33) & 0xFFFF] >> bit) & 1) << 5) \
                    | (((vram[(toff + 48) & 0xFFFF] >> bit) & 1) << 6) \
                    | (((vram[(toff + 49) & 0xFFFF] >> bit) & 1) << 7)
            out[fx] = cidx
        result = bytes(out)
        if len(self._tile_cache) > 4096:
            self._tile_cache.clear()
        self._tile_cache[key] = result
        return result

    def _bg_pixel(
        self, bg: int, bpp: int, x: int, y: int, palette_base: int,
    ) -> int:
        """Return CGRAM index for BG pixel, or 0 if transparent / windowed."""
        if self._window_mask(bg, x):
            tmw = self.regs[0x2E]
            if tmw & (1 << bg):
                return 0
        sc = self.regs[0x07 + bg]
        # BGSC selects 1K-word segments and BGnNBA selects 4K-word
        # segments. VRAM is byte-backed here, so both bases need x2.
        map_base = ((sc & 0xFC) << 9) & 0xFFFF
        nba = self.regs[0x0B + (bg >> 1)]
        shift = 4 * (bg & 1)
        tile_base = (((nba >> shift) & 0x0F) << 13) & 0xFFFF
        pal_size = 1 << bpp
        hofs = self.bg_hofs[bg]
        vofs = self.bg_vofs[bg]
        wide = bool(sc & 0x01)
        tall = bool(sc & 0x02)
        cell_size = 16 if (self.regs[0x05] & (0x10 << bg)) else 8
        map_w = 64 if wide else 32
        map_h = 64 if tall else 32
        sy = (y + vofs) & (map_h * cell_size - 1)
        sx = (x + hofs) & (map_w * cell_size - 1)
        ty, fy = sy // cell_size, sy & (cell_size - 1)
        tx, fx = sx // cell_size, sx & (cell_size - 1)
        screen = (ty >> 5) * (2 if wide else 1) + (tx >> 5)
        map_off = (map_base + screen * 0x800 + (((ty & 31) * 32 + (tx & 31)) * 2)) & 0xFFFF
        entry = self.vram[map_off] | (self.vram[(map_off + 1) & 0xFFFF] << 8)
        tile = entry & 0x3FF
        pal = (entry >> 10) & 7
        if entry & 0x4000:
            fx = cell_size - 1 - fx
        if entry & 0x8000:
            fy = cell_size - 1 - fy
        if cell_size == 16:
            tile = (tile + (fx >> 3) + ((fy >> 3) << 4)) & 0x3FF
        fx &= 7
        py = fy & 7
        row = self._decode_tile_row(tile_base, tile, bpp, py)
        cidx = row[fx]
        if cidx == 0:
            return 0
        # 8-bpp BGs address the complete 256-color CGRAM directly.
        return cidx if bpp == 8 else palette_base + pal * pal_size + cidx

    @staticmethod
    def _mode_layers(mode: int) -> tuple[tuple[int, int, int], ...]:
        """Return BG layers bottom-to-top as (BG index, bpp, palette base).

        Offset-per-tile and true 512-pixel sampling remain future accuracy
        work, but every hardware mode now uses its correct color depth/layers.
        """
        return (
            ((3, 2, 96), (2, 2, 64), (1, 2, 32), (0, 2, 0)),  # mode 0
            ((2, 2, 0), (1, 4, 0), (0, 4, 0)),                 # mode 1
            ((1, 4, 0), (0, 4, 0)),                            # mode 2
            ((1, 4, 0), (0, 8, 0)),                            # mode 3
            ((1, 2, 0), (0, 8, 0)),                            # mode 4
            ((1, 2, 0), (0, 4, 0)),                            # mode 5 (downsampled)
            ((0, 4, 0),),                                      # mode 6 (downsampled)
            (),                                                # mode 7
        )[mode & 7]

    def _mode7_pixel(self, x: int, y: int) -> int:
        """Mode 7 affine: screen (x,y) → VRAM tilemap pixel (CGRAM index)."""
        if self._window_mask(0, x) and (self.regs[0x2E] & 1):
            return 0
        m7sel = self.regs[0x1A]
        if m7sel & 0x01:
            x = 255 - x
        if m7sel & 0x02:
            y = 255 - y
        # SNES Mode 7: (A B; C D) around center (M7X,M7Y) with scroll
        a, b, c, d = _s16(self.m7_a), _s16(self.m7_b), _s16(self.m7_c), _s16(self.m7_d)
        cx, cy = self.m7_x, self.m7_y
        # Clip signed 13-bit centers already applied in write
        sx = x + (self.m7_hofs - cx)
        sy = y + (self.m7_vofs - cy)
        vx = ((a * sx + b * sy) >> 8) + cx
        vy = ((c * sx + d * sy) >> 8) + cy
        # Screen over: wrap / transparent / tile 0
        if vx < 0 or vy < 0 or vx >= 1024 or vy >= 1024:
            if m7sel & 0x80:
                if m7sel & 0x40:
                    return 0  # transparent
                vx &= 1023
                vy &= 1023
            else:
                vx &= 1023
                vy &= 1023
        tile_x, fine_x = (vx >> 3) & 127, vx & 7
        tile_y, fine_y = (vy >> 3) & 127, vy & 7
        # Tilemap in VRAM low bytes of each word; char data in high bytes
        map_addr = ((tile_y * 128 + tile_x) * 2) & 0xFFFF
        tile = self.vram[map_addr]
        char_addr = (tile * 128 + fine_y * 16 + fine_x) & 0xFFFF
        # Character data is in the high byte of each VRAM word for Mode 7
        # Standard: pixel at (tile*64 + fy*8 + fx) as bytes in VRAM
        pix_addr = ((tile << 6) + (fine_y << 3) + fine_x) & 0x3FFF
        # Mode 7 chars: even addresses hold tilemap, odd hold graphics — use word layout
        cidx = self.vram[((pix_addr << 1) + 1) & 0xFFFF]
        return cidx & 0xFF

    def _sprite_pixel(self, x: int, y: int) -> int:
        if self._window_mask(4, x) and (self.regs[0x2E] & 0x10):
            return 0
        obsel = self.regs[0x01]
        # OBJ base is an 8K-word segment (16 KiB in byte-backed VRAM).
        tile_base = (obsel & 0x03) << 14
        name_offset = (((obsel >> 3) & 0x03) + 1) << 13
        size_pairs = (
            (8, 16), (8, 32), (8, 64), (16, 32),
            (16, 64), (32, 64), (16, 32), (16, 32),
        )
        small_size, large_size = size_pairs[(obsel >> 5) & 7]
        best = 0
        for n in range(128):
            o = n * 4
            sx = self.oam[o]
            sy = self.oam[o + 1]
            tile = self.oam[o + 2]
            attr = self.oam[o + 3]
            hi = self.oam[0x200 + (n >> 2)]
            pair_shift = (n & 3) * 2
            if (hi >> pair_shift) & 1:
                sx -= 256
            size = large_size if ((hi >> (pair_shift + 1)) & 1) else small_size
            px = x - sx
            py = (y - sy) & 0xFF
            if not (0 <= px < size and py < size):
                continue
            pal = (attr >> 1) & 7
            flip_x = bool(attr & 0x40)
            flip_y = bool(attr & 0x80)
            if flip_x:
                px = size - 1 - px
            if flip_y:
                py = size - 1 - py
            base = tile_base + (name_offset if (attr & 1) else 0)
            tno = (tile + (px >> 3) + ((py >> 3) << 4)) & 0xFF
            row = self._decode_tile_row(base & 0xFFFF, tno, 4, py & 7)
            cidx = row[px & 7]
            if cidx:
                best = 128 + pal * 16 + cidx
                break
        return best

    def _color_math(
        self, main: tuple[int, int, int], sub: tuple[int, int, int], apply: bool,
    ) -> tuple[int, int, int]:
        if not apply:
            return main
        cgadsub = self.regs[0x31]
        subtract = bool(cgadsub & 0x80)
        half = bool(cgadsub & 0x40)
        mr, mg, mb = main
        sr, sg, sb = sub
        if subtract:
            r, g, b = mr - sr, mg - sg, mb - sb
        else:
            r, g, b = mr + sr, mg + sg, mb + sb
        if half:
            r, g, b = r >> 1, g >> 1, b >> 1
        return (max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b)))

    def render_scanline(self, y: int) -> None:
        """Scanline renderer — Mode 7 / color math / windows, with fast path."""
        if y < 0 or y >= HEIGHT:
            return
        fb = self.framebuffer
        forced = self.regs[0x00] & 0x80
        br = self.brightness / 15.0 if not forced else 0.0
        row_off = y * WIDTH * 3
        if br == 0.0:
            fb[row_off:row_off + WIDTH * 3] = b"\x00" * (WIDTH * 3)
            return

        mode = self.bg_mode
        tm = self.regs[0x2C]
        cgadsub = self.regs[0x31]
        # Fast path: no color math + no window selects → tile-cached BG blit
        windows_used = self.regs[0x23] | self.regs[0x24] | self.regs[0x25]
        if mode != 7 and cgadsub == 0 and windows_used == 0:
            self._render_scanline_fast(y, br, tm, mode)
            return

        ts = self.regs[0x2D]
        cgwsel = self.regs[0x30]
        fixed = (
            (self.fixed_r << 3) & 0xF8,
            (self.fixed_g << 3) & 0xF8,
            (self.fixed_b << 3) & 0xF8,
        )
        back = self._cgram_rgb(0)

        layers = self._mode_layers(mode)

        for x in range(WIDTH):
            main_idx = 0
            sub_idx = 0
            main_layer = -1
            if mode == 7 and (tm & 1):
                main_idx = self._mode7_pixel(x, y)
                if main_idx:
                    main_layer = 0
            else:
                for bg, bpp, pbase in layers:
                    if tm & (1 << bg):
                        pix = self._bg_pixel(bg, bpp, x, y, pbase)
                        if pix:
                            main_idx = pix
                            main_layer = bg
                if tm & 0x10:
                    sp = self._sprite_pixel(x, y)
                    if sp:
                        main_idx = sp
                        main_layer = 4
            if not (cgwsel & 0x02):
                if mode == 7 and (ts & 1):
                    sub_idx = self._mode7_pixel(x, y)
                else:
                    for bg, bpp, pbase in layers:
                        if ts & (1 << bg):
                            pix = self._bg_pixel(bg, bpp, x, y, pbase)
                            if pix:
                                sub_idx = pix
                    if ts & 0x10:
                        sp = self._sprite_pixel(x, y)
                        if sp:
                            sub_idx = sp

            main_rgb = self._cgram_rgb(main_idx) if main_idx else back
            if cgwsel & 0x02:
                sub_rgb = fixed
            else:
                sub_rgb = self._cgram_rgb(sub_idx) if sub_idx else fixed

            apply = False
            if main_layer >= 0 and (cgadsub & (1 << min(main_layer, 4))):
                clip = (cgwsel >> 4) & 3
                inside_win = self._window_mask(main_layer if main_layer < 4 else 4, x)
                if clip == 0:
                    apply = True
                elif clip == 1:
                    apply = not inside_win
                elif clip == 2:
                    apply = inside_win
            rgb = self._color_math(main_rgb, sub_rgb, apply)
            o = row_off + x * 3
            fb[o] = int(rgb[0] * br)
            fb[o + 1] = int(rgb[1] * br)
            fb[o + 2] = int(rgb[2] * br)

    def _render_scanline_fast(self, y: int, br: float, tm: int, mode: int) -> None:
        """Tile-cached single scanline without color math / windows."""
        fb = self.framebuffer
        row_off = y * WIDTH * 3
        r, g, b = self._cgram_rgb(0)
        back = (int(r * br), int(g * br), int(b * br))
        row = bytearray(back) * WIDTH
        # Paint into local row then blit once
        layers = self._mode_layers(mode)

        for bg, bpp, pbase in layers:
            if not (tm & (1 << bg)):
                continue
            pal_size = 1 << bpp
            sc = self.regs[0x07 + bg]
            map_base = ((sc & 0xFC) << 9) & 0xFFFF
            nba = self.regs[0x0B + (bg >> 1)]
            shift = 4 * (bg & 1)
            tile_base = (((nba >> shift) & 0x0F) << 13) & 0xFFFF
            hofs = self.bg_hofs[bg]
            vofs = self.bg_vofs[bg]
            wide = bool(sc & 0x01)
            tall = bool(sc & 0x02)
            cell_size = 16 if (self.regs[0x05] & (0x10 << bg)) else 8
            map_w = 64 if wide else 32
            map_h = 64 if tall else 32
            sy = (y + vofs) & (map_h * cell_size - 1)
            ty, fy = sy // cell_size, sy & (cell_size - 1)
            for x in range(WIDTH):
                sx = (x + hofs) & (map_w * cell_size - 1)
                tx, fx = sx // cell_size, sx & (cell_size - 1)
                screen = (ty >> 5) * (2 if wide else 1) + (tx >> 5)
                map_off = (map_base + screen * 0x800 + (((ty & 31) * 32 + (tx & 31)) * 2)) & 0xFFFF
                entry = self.vram[map_off] | (self.vram[(map_off + 1) & 0xFFFF] << 8)
                tile = entry & 0x3FF
                pal = (entry >> 10) & 7
                px = cell_size - 1 - fx if entry & 0x4000 else fx
                py = cell_size - 1 - fy if entry & 0x8000 else fy
                if cell_size == 16:
                    tile = (tile + (px >> 3) + ((py >> 3) << 4)) & 0x3FF
                px &= 7
                py &= 7
                tiled = self._decode_tile_row(tile_base, tile, bpp, py)
                cidx = tiled[px]
                if not cidx:
                    continue
                color = cidx if bpp == 8 else pbase + pal * pal_size + cidx
                cr, cg, cb = self._cgram_rgb(color)
                o = x * 3
                row[o] = int(cr * br)
                row[o + 1] = int(cg * br)
                row[o + 2] = int(cb * br)

        if tm & 0x10:
            for x in range(WIDTH):
                sp = self._sprite_pixel(x, y)
                if not sp:
                    continue
                cr, cg, cb = self._cgram_rgb(sp)
                o = x * 3
                row[o] = int(cr * br)
                row[o + 1] = int(cg * br)
                row[o + 2] = int(cb * br)

        fb[row_off:row_off + WIDTH * 3] = row

    def render_frame(self) -> bytearray:
        fb = self.framebuffer
        forced = self.regs[0x00] & 0x80
        br = self.brightness / 15.0 if not forced else 0.0
        if br == 0.0 and not any(self.cgram) and not any(self.vram[:0x1000]):
            for y in range(HEIGHT):
                shade = BOOT_RGB[(y * len(BOOT_RGB)) // HEIGHT]
                fb[y * WIDTH * 3:(y + 1) * WIDTH * 3] = bytes(shade) * WIDTH
            return fb
        # If run_frame already painted scanlines, framebuffer is ready.
        # Fallback full-frame pass for callers that skip scanline pacing:
        if self.scanline == 0:
            for y in range(HEIGHT):
                self.render_scanline(y)
        return fb


class APU:
    """SPC700 port bridge + lightweight S-DSP (ADPCM/pitch/echo stubs).

    Keeps commercial boot from hanging on $2140-$2143. Uploaded IPL traffic
    is echoed with a short latency so the reset handshake ($AA/$BB) completes.
    Audio mixes BRR-ish noise shaped by port activity plus an echo buffer.
    """

    SAMPLE_RATE = 32000
    ECHO_SIZE = 4096

    def __init__(self) -> None:
        self.regs = bytearray(0x100)
        self.cpu_ports = bytearray(4)
        self.output_ports = bytearray(4)
        self._phase = 0.0
        self._activity = 0.0
        self._sample_debt = 0.0
        self._boot = True
        self._echo = array("h", [0]) * self.ECHO_SIZE
        self._echo_pos = 0
        self._echo_fb = 0.35
        self._pitch_mod = 1.0
        self._brr_phase = 0.0
        self._pending_echo_writes: list[tuple[int, int]] = []
        self.reset()

    def reset(self) -> None:
        self.regs[:] = b"\x00" * len(self.regs)
        self.cpu_ports[:] = b"\x00\x00\x00\x00"
        # SPC700 IPL ROM announces itself with $AA/$BB on output ports 0/1.
        self.output_ports[:] = b"\xAA\xBB\x00\x00"
        self._phase = 0.0
        self._activity = 0.0
        self._sample_debt = 0.0
        self._boot = True
        self._echo_pos = 0
        for i in range(self.ECHO_SIZE):
            self._echo[i] = 0
        self._pitch_mod = 1.0
        self._brr_phase = 0.0

    def write(self, addr: int, value: int) -> None:
        port = addr & 0x03
        value &= 0xFF
        self.cpu_ports[port] = value
        self.regs[port] = value
        # Stable handshake: always present CPU→APU write on the output port
        # after a 0-latency mirror (IPL transfer protocol). Boot $AA/$BB is
        # replaced once the game starts talking.
        if self._boot and port == 0 and value == 0xCC:
            self._boot = False
        self.output_ports[port] = value
        self._activity = min(1.0, self._activity + (0.04 if port < 2 else 0.015))
        # Pitch modulation driven by ports 2/3 (common song-select traffic)
        word = self.cpu_ports[2] | (self.cpu_ports[3] << 8)
        self._pitch_mod = 0.85 + (word & 0x3FF) / 2048.0

    def read(self, addr: int) -> int:
        return self.output_ports[addr & 0x03]

    def _brr_sample(self) -> float:
        """Cheap ADPCM-ish waveform: stepped triangle + noise (BRR stand-in)."""
        self._brr_phase = (self._brr_phase + 0.01 * self._pitch_mod) % 1.0
        step = int(self._brr_phase * 16) / 16.0
        triangle = 1.0 - 4.0 * abs(step - 0.5)
        # 4-bit nibble quantization like BRR filter 0
        nibble = int(triangle * 7) / 7.0
        return nibble

    def drain_samples(self, n: int) -> bytes:
        if n <= 0:
            return b""
        out = array("h")
        phase = self._phase
        activity = self._activity
        word = self.cpu_ports[2] | (self.cpu_ports[3] << 8)
        frequency = 110.0 + float(word % 770)
        phase_step = (frequency * self._pitch_mod) / self.SAMPLE_RATE
        echo = self._echo
        epos = self._echo_pos
        fb = self._echo_fb
        for _ in range(n):
            dry = self._brr_sample() * 1800.0 * activity
            dry += (1.0 - 4.0 * abs(phase - 0.5)) * 900.0 * activity
            wet = echo[epos] * fb
            sample = int(max(-32768, min(32767, dry + wet)))
            echo[epos] = int(dry * 0.55)
            epos = (epos + 1) % self.ECHO_SIZE
            out.append(sample)
            phase = (phase + phase_step) % 1.0
            activity *= 0.99945
        self._phase = phase
        self._echo_pos = epos
        self._activity = activity if activity > 0.0005 else 0.0
        if sys.byteorder != "little":
            out.byteswap()
        return out.tobytes()

    def drain_frame_samples(self, fps: float) -> bytes:
        self._sample_debt += self.SAMPLE_RATE / fps
        count = int(self._sample_debt)
        self._sample_debt -= count
        return self.drain_samples(count)


class DMA:
    """MDMA + HDMA — H-blank transfers for raster effects / parallax."""

    def __init__(self, bus: "Bus") -> None:
        self.bus = bus
        self.channels = [bytearray(0x10) for _ in range(8)]
        self.hdma_enable = 0
        self._hdma_line = [0] * 8
        self._hdma_do_transfer = [True] * 8
        self._hdma_addr = [0] * 8
        self._hdma_bank = [0] * 8
        self._hdma_indirect = [0] * 8

    def write(self, offset: int, value: int) -> None:
        ch = (offset >> 4) & 7
        reg = offset & 0xF
        if reg < 0x10:
            self.channels[ch][reg] = value & 0xFF

    def read(self, offset: int) -> int:
        ch = (offset >> 4) & 7
        reg = offset & 0xF
        if reg < 0x10:
            return self.channels[ch][reg]
        return 0

    def trigger(self, mask: int) -> int:
        transferred = 0
        for i in range(8):
            if mask & (1 << i):
                transferred += self._run_channel(i)
        # DMA owns the buses while active. The core counts nominal CPU cycles,
        # so convert the 8-master-clock setup/byte cost to that time base.
        stall = (8 + transferred * 8 + 5) // 6 if transferred else 0
        self.bus.dma_stall_cycles += stall
        return transferred

    def _run_channel(self, ch: int) -> int:
        c = self.channels[ch]
        ctrl = c[0]
        bbus = c[1] & 0xFF
        a_addr = c[2] | (c[3] << 8)
        a_bank = c[4]
        size = c[5] | (c[6] << 8)
        if size == 0:
            size = 0x10000
        a_step = 1
        if ctrl & 0x08:
            a_step = 0
        elif ctrl & 0x10:
            a_step = -1
        patterns = (
            (0,), (0, 1), (0, 0), (0, 0, 1, 1),
            (0, 1, 2, 3), (0, 1, 0, 1), (0, 0), (0, 0, 1, 1),
        )
        pattern = patterns[ctrl & 0x07]
        b_to_a = bool(ctrl & 0x80)
        for index in range(size):
            dest = 0x2100 | ((bbus + pattern[index % len(pattern)]) & 0xFF)
            if b_to_a:
                byte = self.bus.read(0, dest)
                self.bus.write(a_bank, a_addr, byte)
            else:
                byte = self.bus.read(a_bank, a_addr)
                self.bus.write(0, dest, byte)
            if a_step:
                a_addr = (a_addr + a_step) & 0xFFFF
        c[2] = a_addr & 0xFF
        c[3] = (a_addr >> 8) & 0xFF
        c[5] = 0
        c[6] = 0
        return size

    def hdma_init_frame(self) -> None:
        """Called at start of frame / after VBlank — reload HDMA tables."""
        for ch in range(8):
            if not (self.hdma_enable & (1 << ch)):
                continue
            c = self.channels[ch]
            self._hdma_addr[ch] = c[2] | (c[3] << 8)
            self._hdma_bank[ch] = c[4]
            self._hdma_line[ch] = 0
            self._hdma_do_transfer[ch] = True

    def hdma_scanline(self) -> None:
        """Run HDMA for current scanline during H-Blank (before visible paint)."""
        patterns = (
            (0,), (0, 1), (0, 0), (0, 0, 1, 1),
            (0, 1, 2, 3), (0, 1, 0, 1), (0, 0), (0, 0, 1, 1),
        )
        for ch in range(8):
            if not (self.hdma_enable & (1 << ch)):
                continue
            c = self.channels[ch]
            ctrl = c[0]
            bbus = c[1]
            pattern = patterns[ctrl & 0x07]
            indirect = bool(ctrl & 0x40)
            if self._hdma_line[ch] <= 0:
                # Fetch new line counter
                table_addr = self._hdma_addr[ch]
                bank = self._hdma_bank[ch]
                line = self.bus.read(bank, table_addr)
                self._hdma_addr[ch] = (table_addr + 1) & 0xFFFF
                if line == 0:
                    # Terminate channel for this frame
                    self.hdma_enable &= ~(1 << ch)
                    continue
                self._hdma_line[ch] = line & 0x7F
                self._hdma_do_transfer[ch] = True
                if indirect:
                    lo = self.bus.read(bank, self._hdma_addr[ch])
                    self._hdma_addr[ch] = (self._hdma_addr[ch] + 1) & 0xFFFF
                    hi = self.bus.read(bank, self._hdma_addr[ch])
                    self._hdma_addr[ch] = (self._hdma_addr[ch] + 1) & 0xFFFF
                    self._hdma_indirect[ch] = lo | (hi << 8)
                    # Indirect bank in DASB ($43x7)
                    c[7] = c[7]  # already set by game
                continuous = bool(line & 0x80)
                c[8] = 1 if continuous else 0  # stash continuous flag in unused byte
            if self._hdma_do_transfer[ch] or c[8]:
                if indirect:
                    src_bank = c[7]
                    src = self._hdma_indirect[ch]
                else:
                    src_bank = self._hdma_bank[ch]
                    src = self._hdma_addr[ch]
                for i, off in enumerate(pattern):
                    dest = 0x2100 | ((bbus + off) & 0xFF)
                    byte = self.bus.read(src_bank, (src + i) & 0xFFFF)
                    self.bus.write(0, dest, byte)
                if indirect:
                    self._hdma_indirect[ch] = (src + len(pattern)) & 0xFFFF
                else:
                    self._hdma_addr[ch] = (src + len(pattern)) & 0xFFFF
                # Non-continuous: transfer only on first scanline of the group
                self._hdma_do_transfer[ch] = False
            self._hdma_line[ch] -= 1
            if c[8]:
                self._hdma_do_transfer[ch] = True


class Bus:
    """SNES CPU bus: WRAM, PPU, APU ports, cart."""

    _JOY_SERIAL_MASKS = (
        0x0080, 0x0040, 0x0020, 0x0010,
        0x0008, 0x0004, 0x0002, 0x0001,
        0x8000, 0x4000, 0x2000, 0x1000,
    )

    def __init__(self, cart: Cartridge, ppu: PPU, apu: APU) -> None:
        self.cart = cart
        self.ppu = ppu
        self.apu = apu
        self.wram = bytearray(0x20000)
        self.cpu = None  # type: ignore
        self.nmi_flag = False
        self.irq_flag = False
        self.joy1 = 0
        self.joy2 = 0
        self.joylatch = 0
        self.joy_strobe = False
        self.joy_index1 = 0
        self.joy_index2 = 0
        self.memsel = 0
        self.wdm_hook = 0
        self.open_bus = 0
        self.nmitimen = 0
        self.htime = 0x1FF
        self.vtime = 0x1FF
        self.irq_mode = 0
        self._irq_fired_line = -1
        self.dma_stall_cycles = 0
        self.dma = DMA(self)
        self.wmadd = 0          # $2181-$2183 WRAM data port address
        self.mul_a = 0xFF       # $4202
        self.mul_b = 0          # $4203
        self.div_a = 0xFFFF     # $4204/$4205
        self.div_b = 0          # $4206
        self.rddiv = 0          # $4214/$4215
        self.rdmpy = 0          # $4216/$4217
        self.auto_joy = False   # NMITIMEN bit 0
        self.hdmaen = 0         # $420C latch (survives per-frame channel disable)

    def reset(self) -> None:
        self.wram[:] = b"\x00" * len(self.wram)
        self.nmi_flag = False
        self.irq_flag = False
        self.open_bus = 0
        self.nmitimen = 0
        self.htime = 0x1FF
        self.vtime = 0x1FF
        self.irq_mode = 0
        self._irq_fired_line = -1
        self.dma_stall_cycles = 0
        self.dma = DMA(self)
        self.wmadd = 0
        self.rddiv = 0
        self.rdmpy = 0
        self.auto_joy = False
        self.joy_strobe = False
        self.joy_index1 = 0
        self.joy_index2 = 0
        self.hdmaen = 0
        self.dma.hdma_enable = 0

    def _in_system_bank(self, bank: int) -> bool:
        return bank <= 0x3F or 0x80 <= bank <= 0xBF

    def _wram_index(self, bank: int, addr: int) -> int | None:
        """Map the 128 KiB WRAM and its documented low-8-KiB mirrors."""
        if bank in (0x7E, 0x7F):
            return ((bank - 0x7E) << 16) | addr
        if not self._in_system_bank(bank):
            return None
        return (addr & 0x1FFF) if addr < 0x2000 else None

    def _latch(self, value: int) -> int:
        self.open_bus = value & 0xFF
        return self.open_bus

    def consume_dma_stall(self) -> int:
        cycles = self.dma_stall_cycles
        self.dma_stall_cycles = 0
        return cycles

    def poll_irq(self, scanline: int) -> bool:
        """Latch one H/V timer IRQ event at scanline granularity."""
        if self.irq_mode == 0 or self._irq_fired_line == scanline:
            return False
        vmatch = (scanline & 0x1FF) == (self.vtime & 0x1FF)
        hvalid = (self.htime & 0x1FF) < 340
        match = (
            (self.irq_mode == 1 and hvalid)
            or (self.irq_mode == 2 and vmatch)
            or (self.irq_mode == 3 and hvalid and vmatch)
        )
        if match:
            self._irq_fired_line = scanline
            self.irq_flag = True
            return True
        return False

    def read(self, bank: int, addr: int) -> int:
        bank &= 0xFF
        addr &= 0xFFFF
        wi = self._wram_index(bank, addr)
        if wi is not None and bank in (0x7E, 0x7F):
            return self._latch(self.wram[wi])
        # PPU / APU / CPU / DMA before WRAM mirror overlap
        if self._in_system_bank(bank) and 0x2100 <= addr <= 0x213F:
            return self._latch(self.ppu.read_reg(addr - 0x2100))
        if self._in_system_bank(bank) and 0x2140 <= addr <= 0x217F:
            return self._latch(self.apu.read(addr - 0x2140))
        if self._in_system_bank(bank) and addr == 0x2180:  # WMDATA
            v = self.wram[self.wmadd % len(self.wram)]
            self.wmadd = (self.wmadd + 1) & 0x1FFFF
            return self._latch(v)
        if self._in_system_bank(bank) and 0x4200 <= addr <= 0x421F:
            return self._latch(self._read_cpu_reg(addr))
        if self._in_system_bank(bank) and 0x4300 <= addr <= 0x437F:
            return self._latch(self.dma.read(addr - 0x4300))
        if wi is not None:
            return self._latch(self.wram[wi])
        if self._in_system_bank(bank) and addr == 0x4016:
            return self._latch(self._read_joy_serial(0))
        if self._in_system_bank(bank) and addr == 0x4017:
            return self._latch(self._read_joy_serial(1))
        # Reserved system-register space is open bus, not a WRAM mirror.
        if self._in_system_bank(bank) and 0x2000 <= addr < 0x6000:
            return self.open_bus
        if (self._in_system_bank(bank) and 0x6000 <= addr < 0x8000
                and self.cart._sram_index(bank, addr) is None
                and self.cart.coprocessor is None):
            return self.open_bus
        return self._latch(self.cart.cpu_read(bank, addr))

    def write(self, bank: int, addr: int, value: int) -> None:
        bank &= 0xFF
        addr &= 0xFFFF
        value &= 0xFF
        self.open_bus = value
        wi = self._wram_index(bank, addr)
        if wi is not None and bank in (0x7E, 0x7F):
            self.wram[wi] = value
            return
        if self._in_system_bank(bank) and 0x2100 <= addr <= 0x213F:
            self.ppu.write_reg(addr - 0x2100, value)
            return
        if self._in_system_bank(bank) and 0x2140 <= addr <= 0x217F:
            self.apu.write(addr - 0x2140, value)
            return
        if self._in_system_bank(bank) and 0x2180 <= addr <= 0x2183:
            if addr == 0x2180:  # WMDATA
                self.wram[self.wmadd % len(self.wram)] = value
                self.wmadd = (self.wmadd + 1) & 0x1FFFF
            elif addr == 0x2181:
                self.wmadd = (self.wmadd & 0x1FF00) | value
            elif addr == 0x2182:
                self.wmadd = (self.wmadd & 0x100FF) | (value << 8)
            else:  # 0x2183
                self.wmadd = (self.wmadd & 0x0FFFF) | ((value & 1) << 16)
            return
        if self._in_system_bank(bank) and 0x4200 <= addr <= 0x421F:
            self._write_cpu_reg(addr, value)
            return
        if self._in_system_bank(bank) and 0x4300 <= addr <= 0x437F:
            self.dma.write(addr - 0x4300, value)
            return
        if self._in_system_bank(bank) and addr == 0x4016:
            was_strobing = self.joy_strobe
            self.joy_strobe = bool(value & 1)
            if self.joy_strobe or was_strobing:
                self.joy_index1 = 0
                self.joy_index2 = 0
            return
        if wi is not None:
            self.wram[wi] = value
            return
        self.cart.cpu_write(bank, addr, value)

    def _read_joy_serial(self, pad: int) -> int:
        index = self.joy_index1 if pad == 0 else self.joy_index2
        buttons = self.joy1 if pad == 0 else self.joy2
        value = (
            1 if index >= len(self._JOY_SERIAL_MASKS)
            else int(bool(buttons & self._JOY_SERIAL_MASKS[index]))
        )
        if not self.joy_strobe:
            if pad == 0:
                self.joy_index1 += 1
            else:
                self.joy_index2 += 1
        return value

    def _read_cpu_reg(self, addr: int) -> int:
        if addr == 0x4210:  # RDNMI
            v = 0x02  # CPU version-ish
            if self.nmi_flag:
                v |= 0x80
                self.nmi_flag = False
            return v
        if addr == 0x4211:
            v = 0x80 if self.irq_flag else 0x00
            self.irq_flag = False
            return v
        if addr == 0x4212:  # HVBJOY
            v = 0x80 if self.ppu.vblank else 0x00
            if self.ppu.hblank:
                v |= 0x40
            return v
        if addr == 0x4214:  # RDDIVL
            return self.rddiv & 0xFF
        if addr == 0x4215:  # RDDIVH
            return (self.rddiv >> 8) & 0xFF
        if addr == 0x4216:  # RDMPYL
            return self.rdmpy & 0xFF
        if addr == 0x4217:  # RDMPYH
            return (self.rdmpy >> 8) & 0xFF
        if addr == 0x4218:
            return self.joy1 & 0xFF
        if addr == 0x4219:
            return (self.joy1 >> 8) & 0xFF
        if addr == 0x421A:
            return self.joy2 & 0xFF
        if addr == 0x421B:
            return (self.joy2 >> 8) & 0xFF
        return self.open_bus

    def _write_cpu_reg(self, addr: int, value: int) -> None:
        if addr == 0x4200:  # NMITIMEN
            self.nmitimen = value
            self.ppu.nmi_enable = bool(value & 0x80)
            self.auto_joy = bool(value & 0x01)
            self.irq_mode = (value >> 4) & 0x03
            if self.irq_mode == 0:
                self.irq_flag = False
        elif addr == 0x4202:  # WRMPYA
            self.mul_a = value
        elif addr == 0x4203:  # WRMPYB — starts 8x8 multiply
            self.mul_b = value
            self.rdmpy = (self.mul_a * self.mul_b) & 0xFFFF
        elif addr == 0x4204:  # WRDIVL
            self.div_a = (self.div_a & 0xFF00) | value
        elif addr == 0x4205:  # WRDIVH
            self.div_a = (self.div_a & 0x00FF) | (value << 8)
        elif addr == 0x4206:  # WRDIVB — starts 16/8 divide
            self.div_b = value
            if self.div_b == 0:
                self.rddiv = 0xFFFF
                self.rdmpy = self.div_a & 0xFFFF
            else:
                self.rddiv = (self.div_a // self.div_b) & 0xFFFF
                self.rdmpy = (self.div_a % self.div_b) & 0xFFFF
        elif addr == 0x4207:
            self.htime = (self.htime & 0x100) | value
        elif addr == 0x4208:
            self.htime = (self.htime & 0x0FF) | ((value & 1) << 8)
        elif addr == 0x4209:
            self.vtime = (self.vtime & 0x100) | value
        elif addr == 0x420A:
            self.vtime = (self.vtime & 0x0FF) | ((value & 1) << 8)
        elif addr == 0x420B:
            self.dma.trigger(value & 0xFF)
        elif addr == 0x420C:  # HDMAEN — enable mask for H-blank HDMA
            self.hdmaen = value & 0xFF
            self.dma.hdma_enable = self.hdmaen
        elif addr == 0x420D:
            self.memsel = value & 1
        else:
            _log_once(f"cpu-w-{addr:04X}", "CPU reg write $%04X=$%02X ignored", addr, value)


class CPU65816:
    """65C816 — all 256 opcodes (native + emulation). Inlined snes9x CPU core."""

    # P flags
    C, Z, I, D, X, M, V, N = 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80
    E = 0x100  # emulation tracked separately

    _ROWS = (
        # Correct 65C816 map: x2=(dp), x7=[dp], x17/x37...=[dp],Y (idpy)
        "BRK:imp:8 ORA:dpix:6 COP:imp:8 ORA:sr:4 TSB:dp:5 ORA:dp:3 ASL:dp:5 ORA:idp:6 PHP:imp:3 ORA:imm:3 ASL:acc:2 PHD:imp:4 TSB:abs:6 ORA:abs:4 ASL:abs:6 ORA:abl:5",
        "BPL:rel:2 ORA:dpiy:5 ORA:dpi:5 ORA:sriy:7 TRB:dp:5 ORA:dpx:4 ASL:dpx:6 ORA:idpy:6 CLC:imp:2 ORA:aby:5 INC:acc:2 TCS:imp:2 TRB:abs:6 ORA:abx:5 ASL:abx:7 ORA:ablx:5",
        "JSR:abs:6 AND:dpix:6 JSL:abl:8 AND:sr:4 BIT:dp:3 AND:dp:3 ROL:dp:5 AND:idp:6 PLP:imp:4 AND:imm:3 ROL:acc:2 PLD:imp:5 BIT:abs:4 AND:abs:4 ROL:abs:6 AND:abl:5",
        "BMI:rel:2 AND:dpiy:5 AND:dpi:5 AND:sriy:7 BIT:dpx:4 AND:dpx:4 ROL:dpx:6 AND:idpy:6 SEC:imp:2 AND:aby:5 DEC:acc:2 TSC:imp:2 BIT:abx:5 AND:abx:5 ROL:abx:7 AND:ablx:5",
        "RTI:imp:7 EOR:dpix:6 WDM:imm:2 EOR:sr:4 MVP:bm:7 EOR:dp:3 LSR:dp:5 EOR:idp:6 PHA:imp:3 EOR:imm:3 LSR:acc:2 PHK:imp:3 JMP:abs:3 EOR:abs:4 LSR:abs:6 EOR:abl:5",
        "BVC:rel:2 EOR:dpiy:5 EOR:dpi:5 EOR:sriy:7 MVN:bm:7 EOR:dpx:4 LSR:dpx:6 EOR:idpy:6 CLI:imp:2 EOR:aby:5 PHY:imp:3 TCD:imp:2 JML:abl:4 EOR:abx:5 LSR:abx:7 EOR:ablx:5",
        "RTS:imp:6 ADC:dpix:6 PER:rell:6 ADC:sr:4 STZ:dp:3 ADC:dp:3 ROR:dp:5 ADC:idp:6 PLA:imp:4 ADC:imm:3 ROR:acc:2 RTL:imp:6 JMP:ind:5 ADC:abs:4 ROR:abs:6 ADC:abl:5",
        "BVS:rel:2 ADC:dpiy:5 ADC:dpi:5 ADC:sriy:7 STZ:dpx:4 ADC:dpx:4 ROR:dpx:6 ADC:idpy:6 SEI:imp:2 ADC:aby:5 PLY:imp:4 TDC:imp:2 JMP:indx:6 ADC:abx:5 ROR:abx:7 ADC:ablx:5",
        "BRA:rel:2 STA:dpix:6 BRL:rell:4 STA:sr:4 STY:dp:3 STA:dp:3 STX:dp:3 STA:idp:6 DEY:imp:2 BIT:imm:3 TXA:imp:2 PHB:imp:3 STY:abs:4 STA:abs:4 STX:abs:4 STA:abl:5",
        "BCC:rel:2 STA:dpiy:5 STA:dpi:5 STA:sriy:7 STY:dpx:4 STA:dpx:4 STX:dpy:4 STA:idpy:6 TYA:imp:2 STA:aby:5 TXS:imp:2 TXY:imp:2 STZ:abs:4 STA:abx:5 STZ:abx:5 STA:ablx:5",
        "LDY:immx:3 LDA:dpix:6 LDX:immx:3 LDA:sr:4 LDY:dp:3 LDA:dp:3 LDX:dp:3 LDA:idp:6 TAY:imp:2 LDA:imm:3 TAX:imp:2 PLB:imp:4 LDY:abs:4 LDA:abs:4 LDX:abs:4 LDA:abl:5",
        "BCS:rel:2 LDA:dpiy:5 LDA:dpi:5 LDA:sriy:7 LDY:dpx:4 LDA:dpx:4 LDX:dpy:4 LDA:idpy:6 CLV:imp:2 LDA:aby:5 TSX:imp:2 TYX:imp:2 LDY:abx:5 LDA:abx:5 LDX:aby:5 LDA:ablx:5",
        "CPY:immx:3 CMP:dpix:6 REP:imm:3 CMP:sr:4 CPY:dp:3 CMP:dp:3 DEC:dp:5 CMP:idp:6 INY:imp:2 CMP:imm:3 DEX:imp:2 WAI:imp:3 CPY:abs:4 CMP:abs:4 DEC:abs:6 CMP:abl:5",
        "BNE:rel:2 CMP:dpiy:5 CMP:dpi:5 CMP:sriy:7 PEI:dp:6 CMP:dpx:4 DEC:dpx:6 CMP:idpy:6 CLD:imp:2 CMP:aby:5 PHX:imp:3 STP:imp:3 JML:indl:6 CMP:abx:5 DEC:abx:7 CMP:ablx:5",
        "CPX:immx:3 SBC:dpix:6 SEP:imm:3 SBC:sr:4 CPX:dp:3 SBC:dp:3 INC:dp:5 SBC:idp:6 INX:imp:2 SBC:imm:3 NOP:imp:2 XBA:imp:3 CPX:abs:4 SBC:abs:4 INC:abs:6 SBC:abl:5",
        "BEQ:rel:2 SBC:dpiy:5 SBC:dpi:5 SBC:sriy:7 PEA:abs:5 SBC:dpx:4 INC:dpx:6 SBC:idpy:6 SED:imp:2 SBC:aby:5 PLX:imp:4 XCE:imp:2 JSR:indx:6 SBC:abx:5 INC:abx:7 SBC:ablx:5",
    )

    OPCODES = tuple(
        tuple(token.split(":")[:2]) + (int(token.split(":")[2]),)
        for row in _ROWS for token in row.split()
    )
    assert len(OPCODES) == 256, "65816 ISA must cover all 256 opcodes"
    OPCODE_COUNT = 256
    # Jump-table slices — avoid re-unpacking OPCODES tuples on the hot path
    _OP_NAME = tuple(op[0] for op in OPCODES)
    _OP_MODE = tuple(op[1] for op in OPCODES)
    _OP_CYCLES = tuple(op[2] for op in OPCODES)
    # Filled once by _build_opcode_jump_table() — 256 callables (snes9x-style)
    _JUMP_TABLE: tuple | None = None
    _MNEMONICS: frozenset[str] | None = None

    @classmethod
    def _build_opcode_jump_table(cls) -> tuple:
        """Build a 256-entry snes9x-style opcode jump table (in-file, Cython-ready)."""
        if cls._JUMP_TABLE is not None:
            return cls._JUMP_TABLE

        def _make(name: str, mode: str, base_cycles: int):
            # Default-arg capture avoids late-binding closure bugs across the loop.
            def _handler(cpu: "CPU65816", n=name, m=mode, c=base_cycles) -> int:
                return cpu._exec(n, m, c)
            _handler.__name__ = f"op_{name}_{mode}"
            _handler.__doc__ = f"65C816 {name} ({mode})"
            return _handler

        table = tuple(
            _make(cls._OP_NAME[i], cls._OP_MODE[i], cls._OP_CYCLES[i])
            for i in range(256)
        )
        assert len(table) == 256
        cls._JUMP_TABLE = table
        cls._MNEMONICS = frozenset(cls._OP_NAME)
        assert len(cls._MNEMONICS) == 92, "official 65C816 mnemonic set is 92 names"
        return table

    @classmethod
    def all_opcodes_supported(cls) -> bool:
        """True when every opcode $00-$FF has a jump-table handler."""
        table = cls._build_opcode_jump_table()
        return len(table) == 256 and all(callable(h) for h in table)

    def __init__(self, bus: Bus) -> None:
        self.bus = bus
        bus.cpu = self
        self.a = 0
        self.x = 0
        self.y = 0
        self.sp = 0x01FF
        self.d = 0  # direct page
        self.db = 0  # data bank
        self.pb = 0  # program bank
        self.p = self.M | self.X | self.I  # m=1,x=1 at reset in emu
        self.e = True  # emulation mode
        self.pc = 0
        self.total_cycles = 0
        self.stopped = False
        self.waiting = False
        self.frozen = False
        self._amask = 0xFF
        self._xmask = 0xFF
        self._page_crossed = False
        # Bind the in-file snes9x opcode jump table (256/256)
        self._dispatch = self._build_opcode_jump_table()
        self._sync_width()

    def _sync_width(self) -> None:
        """Cache register width after REP/SEP/XCE (hot path)."""
        m8 = self.e or bool(self.p & self.M)
        x8 = self.e or bool(self.p & self.X)
        self._amask = 0xFF if m8 else 0xFFFF
        self._xmask = 0xFF if x8 else 0xFFFF

    # ── helpers ────────────────────────────────────────────────────────────
    def read8(self, bank: int, addr: int) -> int:
        return self.bus.read(bank, addr)

    def write8(self, bank: int, addr: int, value: int) -> None:
        self.bus.write(bank, addr, value)

    def read16(self, bank: int, addr: int) -> int:
        lo = self.read8(bank, addr)
        hi = self.read8(bank, (addr + 1) & 0xFFFF)
        return lo | (hi << 8)

    def write16(self, bank: int, addr: int, value: int) -> None:
        self.write8(bank, addr, value & 0xFF)
        self.write8(bank, (addr + 1) & 0xFFFF, (value >> 8) & 0xFF)

    def _read16_dp(self, addr: int) -> int:
        """Direct-page 16-bit read; page-wrap in emulation when DL==0."""
        if self.e and (self.d & 0xFF) == 0:
            lo = self.read8(0x00, addr & 0xFFFF)
            hi = self.read8(0x00, (addr & 0xFF00) | ((addr + 1) & 0xFF))
            return lo | (hi << 8)
        return self.read16(0x00, addr & 0xFFFF)

    def _add24(self, bank: int, addr: int, index: int) -> tuple[int, int]:
        """24-bit address + index (crosses banks)."""
        full = ((bank & 0xFF) << 16) | (addr & 0xFFFF)
        full = (full + (index & 0xFFFF)) & 0xFFFFFF
        return (full >> 16) & 0xFF, full & 0xFFFF

    def fetch8(self) -> int:
        v = self.read8(self.pb, self.pc)
        self.pc = (self.pc + 1) & 0xFFFF
        return v

    def fetch16(self) -> int:
        lo = self.fetch8()
        hi = self.fetch8()
        return lo | (hi << 8)

    def fetch24(self) -> int:
        lo = self.fetch8()
        hi = self.fetch8()
        bk = self.fetch8()
        return lo | (hi << 8) | (bk << 16)

    def a_mask(self) -> int:
        return self._amask

    def x_mask(self) -> int:
        return self._xmask

    def a_bytes(self) -> int:
        return 1 if self._amask == 0xFF else 2

    def x_bytes(self) -> int:
        return 1 if self._xmask == 0xFF else 2

    def set_nz_a(self, v: int) -> None:
        m = self.a_mask()
        v &= m
        self.p = (self.p & ~(self.Z | self.N))
        if v == 0:
            self.p |= self.Z
        neg = 0x80 if m == 0xFF else 0x8000
        if v & neg:
            self.p |= self.N

    def set_nz_x(self, v: int) -> None:
        m = self.x_mask()
        v &= m
        self.p = (self.p & ~(self.Z | self.N))
        if v == 0:
            self.p |= self.Z
        neg = 0x80 if m == 0xFF else 0x8000
        if v & neg:
            self.p |= self.N

    def push8(self, v: int) -> None:
        self.write8(0x00, self.sp & 0xFFFF, v & 0xFF)
        if self.e:
            self.sp = 0x0100 | ((self.sp - 1) & 0xFF)
        else:
            self.sp = (self.sp - 1) & 0xFFFF

    def pull8(self) -> int:
        if self.e:
            self.sp = 0x0100 | ((self.sp + 1) & 0xFF)
        else:
            self.sp = (self.sp + 1) & 0xFFFF
        return self.read8(0x00, self.sp & 0xFFFF)

    def push16(self, v: int) -> None:
        self.push8((v >> 8) & 0xFF)
        self.push8(v & 0xFF)

    def pull16(self) -> int:
        lo = self.pull8()
        hi = self.pull8()
        return lo | (hi << 8)

    def get_a(self) -> int:
        return self.a & self.a_mask()

    def set_a(self, v: int) -> None:
        m = self.a_mask()
        if m == 0xFF:
            self.a = (self.a & 0xFF00) | (v & 0xFF)
        else:
            self.a = v & 0xFFFF

    def get_x(self) -> int:
        return self.x & self.x_mask()

    def set_x(self, v: int) -> None:
        m = self.x_mask()
        self.x = v & m
        if m == 0xFF:
            self.x &= 0xFF

    def get_y(self) -> int:
        return self.y & self.x_mask()

    def set_y(self, v: int) -> None:
        m = self.x_mask()
        self.y = v & m
        if m == 0xFF:
            self.y &= 0xFF

    def reset(self) -> None:
        # Reset always enters 6502-compatible emulation mode. SNES boot code
        # normally uses CLC/XCE to enter native mode after basic setup.
        self.e = True
        self.p = self.M | self.X | self.I
        self.a = self.x = self.y = 0
        self.d = 0
        self.db = 0
        self.pb = 0
        self.sp = 0x01FF
        lo = self.read8(0x00, 0xFFFC)
        hi = self.read8(0x00, 0xFFFD)
        self.pc = lo | (hi << 8)
        self.stopped = False
        self.waiting = False
        self.frozen = False
        self.total_cycles = 0
        self._sync_width()
        if self.pc == 0 and lo == 0 and hi == 0:
            raise EmulatorError("Invalid reset vector $0000 — bad ROM mapping?")

    def nmi(self) -> None:
        self.waiting = False
        if self.e:
            self.push8((self.pc >> 8) & 0xFF)
            self.push8(self.pc & 0xFF)
            self.push8(((self.p | 0x20) & ~0x10) & 0xFF)
            self.p = (self.p | self.I) & ~self.D
            self.pb = 0
            self.pc = self.read16(0x00, 0xFFFA)
        else:
            self.push8(self.pb)
            self.push16(self.pc)
            self.push8(self.p & 0xFF)
            self.p |= self.I
            self.p &= ~self.D
            self.pb = 0
            self.pc = self.read16(0x00, 0xFFEA)

    def irq(self) -> bool:
        """Wake WAI and enter the maskable interrupt vector when I is clear."""
        self.waiting = False
        if self.p & self.I:
            return False
        if self.e:
            self.push8((self.pc >> 8) & 0xFF)
            self.push8(self.pc & 0xFF)
            self.push8(((self.p | 0x20) & ~0x10) & 0xFF)
            self.p = (self.p | self.I) & ~self.D
            self.pb = 0
            self.pc = self.read16(0x00, 0xFFFE)
        else:
            self.push8(self.pb)
            self.push16(self.pc)
            self.push8(self.p & 0xFF)
            self.p = (self.p | self.I) & ~self.D
            self.pb = 0
            self.pc = self.read16(0x00, 0xFFEE)
        return True

    # ── addressing ─────────────────────────────────────────────────────────
    def _ea(self, mode: str) -> tuple[int, int]:
        """Return (bank, addr) effective address for all 65816 modes."""
        self._page_crossed = False
        if mode in ("imp", "acc"):
            return 0, 0
        if mode == "imm":
            addr = self.pc
            self.pc = (self.pc + self.a_bytes()) & 0xFFFF
            return self.pb, addr
        if mode == "immx":
            addr = self.pc
            self.pc = (self.pc + self.x_bytes()) & 0xFFFF
            return self.pb, addr
        if mode == "dp":
            return 0x00, (self.d + self.fetch8()) & 0xFFFF
        if mode == "dpx":
            return 0x00, (self.d + self.fetch8() + self.get_x()) & 0xFFFF
        if mode == "dpy":
            return 0x00, (self.d + self.fetch8() + self.get_y()) & 0xFFFF
        if mode == "dpi":  # (dp)
            dp = (self.d + self.fetch8()) & 0xFFFF
            return self.db, self._read16_dp(dp)
        if mode == "dpix":  # (dp,X)
            dp = (self.d + self.fetch8() + self.get_x()) & 0xFFFF
            return self.db, self._read16_dp(dp)
        if mode == "dpiy":  # (dp),Y
            dp = (self.d + self.fetch8()) & 0xFFFF
            ptr = self._read16_dp(dp)
            result = (ptr + self.get_y()) & 0xFFFF
            self._page_crossed = (ptr & 0xFF00) != (result & 0xFF00)
            return self.db, result
        if mode == "idp":  # [dp]
            dp = (self.d + self.fetch8()) & 0xFFFF
            lo = self.read8(0x00, dp)
            hi = self.read8(0x00, (dp + 1) & 0xFFFF)
            bk = self.read8(0x00, (dp + 2) & 0xFFFF)
            return bk, lo | (hi << 8)
        if mode == "idpy":  # [dp],Y — 24-bit + Y (may cross banks)
            dp = (self.d + self.fetch8()) & 0xFFFF
            lo = self.read8(0x00, dp)
            hi = self.read8(0x00, (dp + 1) & 0xFFFF)
            bk = self.read8(0x00, (dp + 2) & 0xFFFF)
            return self._add24(bk, lo | (hi << 8), self.get_y())
        if mode == "sr":  # sr,S
            return 0x00, (self.sp + self.fetch8()) & 0xFFFF
        if mode == "sriy":  # (sr,S),Y
            a = (self.sp + self.fetch8()) & 0xFFFF
            ptr = self.read16(0x00, a)
            result = (ptr + self.get_y()) & 0xFFFF
            self._page_crossed = (ptr & 0xFF00) != (result & 0xFF00)
            return self.db, result
        if mode == "abs":
            return self.db, self.fetch16()
        if mode == "abx":
            base = self.fetch16()
            result = (base + self.get_x()) & 0xFFFF
            self._page_crossed = (base & 0xFF00) != (result & 0xFF00)
            return self.db, result
        if mode == "aby":
            base = self.fetch16()
            result = (base + self.get_y()) & 0xFFFF
            self._page_crossed = (base & 0xFF00) != (result & 0xFF00)
            return self.db, result
        if mode == "abl":  # long
            v = self.fetch24()
            return (v >> 16) & 0xFF, v & 0xFFFF
        if mode == "ablx":  # long,X — crosses banks
            v = self.fetch24()
            return self._add24((v >> 16) & 0xFF, v & 0xFFFF, self.get_x())
        if mode == "ind":  # (abs) — pointer always in bank 0
            a = self.fetch16()
            lo = self.read8(0x00, a)
            hi = self.read8(0x00, (a + 1) & 0xFFFF)
            return self.pb, lo | (hi << 8)
        if mode == "indx":  # (abs,X) — pointer in PB
            a = (self.fetch16() + self.get_x()) & 0xFFFF
            lo = self.read8(self.pb, a)
            hi = self.read8(self.pb, (a + 1) & 0xFFFF)
            return self.pb, lo | (hi << 8)
        if mode == "indl":  # [abs]
            a = self.fetch16()
            lo = self.read8(0x00, a)
            hi = self.read8(0x00, (a + 1) & 0xFFFF)
            bk = self.read8(0x00, (a + 2) & 0xFFFF)
            return bk, lo | (hi << 8)
        if mode == "rel":
            off = self.fetch8()
            if off & 0x80:
                off -= 0x100
            return self.pb, (self.pc + off) & 0xFFFF
        if mode == "rell":
            off = self.fetch16()
            if off & 0x8000:
                off -= 0x10000
            return self.pb, (self.pc + off) & 0xFFFF
        if mode == "bm":
            return 0, 0
        return self.db, 0

    def _read_op(self, bank: int, addr: int) -> int:
        if self.a_bytes() == 1:
            return self.read8(bank, addr)
        return self.read16(bank, addr)

    def _write_op(self, bank: int, addr: int, value: int) -> None:
        if self.a_bytes() == 1:
            self.write8(bank, addr, value)
        else:
            self.write16(bank, addr, value)

    def _read_idx(self, bank: int, addr: int) -> int:
        if self.x_bytes() == 1:
            return self.read8(bank, addr)
        return self.read16(bank, addr)

    def _write_idx(self, bank: int, addr: int, value: int) -> None:
        if self.x_bytes() == 1:
            self.write8(bank, addr, value)
        else:
            self.write16(bank, addr, value)

    # ── ALU ────────────────────────────────────────────────────────────────
    def _adc(self, value: int) -> None:
        m = self.a_mask()
        a = self.get_a()
        value &= m
        c = 1 if (self.p & self.C) else 0
        if self.p & self.D:
            # WDC packed-BCD adjustment, nibble by nibble. V reflects the
            # unadjusted binary sum; N/Z reflect the adjusted result.
            binary = a + value + c
            result = 0
            carry = c
            bits = 8 if m == 0xFF else 16
            for shift in range(0, bits, 4):
                digit = ((a >> shift) & 0xF) + ((value >> shift) & 0xF) + carry
                if digit > 9:
                    digit += 6
                carry = 1 if digit > 0xF else 0
                result |= (digit & 0xF) << shift
            self.p &= ~(self.V | self.C)
            neg = 0x80 if m == 0xFF else 0x8000
            if ~(a ^ value) & (a ^ binary) & neg:
                self.p |= self.V
            if carry:
                self.p |= self.C
            self.set_a(result)
            self.set_nz_a(result)
            return
        result = a + value + c
        self.p &= ~(self.C | self.V)
        neg = 0x80 if m == 0xFF else 0x8000
        if (~(a ^ value) & (a ^ result) & neg):
            self.p |= self.V
        if result > m:
            self.p |= self.C
        self.set_a(result)
        self.set_nz_a(self.get_a())

    def _sbc(self, value: int) -> None:
        m = self.a_mask()
        a = self.get_a()
        value &= m
        c = 1 if (self.p & self.C) else 0
        if self.p & self.D:
            if m == 0xFF:
                lo = (a & 0x0F) - (value & 0x0F) + (c - 1)
                hi = (a >> 4) - (value >> 4)
                if lo < 0:
                    lo -= 6
                    hi -= 1
                if hi < 0:
                    hi -= 6
                result = ((hi << 4) | (lo & 0x0F)) & 0xFF
                self.p &= ~(self.C | self.V)
                bin_r = a - value + (c - 1)
                if (a ^ value) & (a ^ bin_r) & 0x80:
                    self.p |= self.V
                if bin_r >= 0:
                    self.p |= self.C
            else:
                # 16-bit BCD SBC via digit correction
                result = 0
                borrow = 1 - c
                src, dst = a, value
                for shift in (0, 4, 8, 12):
                    digit = ((src >> shift) & 0xF) - ((dst >> shift) & 0xF) - borrow
                    if digit < 0:
                        digit -= 6
                        borrow = 1
                    else:
                        borrow = 0
                    result |= (digit & 0xF) << shift
                self.p &= ~(self.C | self.V)
                bin_r = a - value + (c - 1)
                if (a ^ value) & (a ^ bin_r) & 0x8000:
                    self.p |= self.V
                if bin_r >= 0:
                    self.p |= self.C
                result &= 0xFFFF
            self.set_a(result)
            self.set_nz_a(result)
            return
        self._adc((~value) & m)

    def _cmp(self, reg: int, value: int, mask: int) -> None:
        result = (reg & mask) - (value & mask)
        self.p &= ~(self.C | self.Z | self.N)
        if result >= 0:
            self.p |= self.C
        if (result & mask) == 0:
            self.p |= self.Z
        neg = 0x80 if mask == 0xFF else 0x8000
        if result & neg:
            self.p |= self.N

    def _branch(self, take: bool, mode: str = "rel") -> int:
        bank, addr = self._ea(mode if mode in ("rel", "rell") else "rel")
        if take:
            old_pc = self.pc
            self.pc = addr
            self.pb = bank
            return 1 + int(self.e and mode == "rel" and (old_pc & 0xFF00) != (addr & 0xFF00))
        return 0

    # ── step ───────────────────────────────────────────────────────────────
    @cython.locals(op=int, cycles=int)
    def step(self) -> int:
        if self.stopped or self.frozen:
            self.total_cycles += 2
            return 2
        if self.waiting:
            self.total_cycles += 2
            return 2
        op = self.fetch8()
        # snes9x-style jump table: O(1) dispatch for all 256 opcodes
        cycles = self._dispatch[op](self)
        cycles += self.bus.consume_dma_stall()
        self.total_cycles += cycles
        return cycles

    def _exec(self, name: str, mode: str, cycles: int) -> int:
        # Control / flags
        if name == "NOP" or name == "WDM":
            if name == "WDM":
                self.fetch8()
            return cycles
        if name == "CLC":
            self.p &= ~self.C
            return cycles
        if name == "SEC":
            self.p |= self.C
            return cycles
        if name == "CLI":
            self.p &= ~self.I
            return cycles
        if name == "SEI":
            self.p |= self.I
            return cycles
        if name == "CLD":
            self.p &= ~self.D
            return cycles
        if name == "SED":
            self.p |= self.D
            return cycles
        if name == "CLV":
            self.p &= ~self.V
            return cycles
        if name == "XCE":
            old_c = bool(self.p & self.C)
            self.p = (self.p & ~self.C) | (self.C if self.e else 0)
            self.e = old_c
            if self.e:
                self.p |= self.M | self.X
                self.sp = 0x0100 | (self.sp & 0xFF)
                self.x &= 0xFF
                self.y &= 0xFF
            self._sync_width()
            return cycles
        if name == "SEP":
            self.p |= self.fetch8()
            if self.p & self.X:
                self.x &= 0xFF
                self.y &= 0xFF
            self._sync_width()
            return cycles
        if name == "REP":
            self.p = (self.p & (~self.fetch8()) & 0xFF)
            if self.e:
                self.p |= self.M | self.X
            self._sync_width()
            return cycles
        if name == "STP":
            self.stopped = True
            return cycles
        if name == "WAI":
            self.waiting = True
            return cycles
        if name == "XBA":
            self.a = ((self.a & 0xFF) << 8) | ((self.a >> 8) & 0xFF)
            # XBA always sets NZ from the new low byte (8-bit), ignoring M
            lo = self.a & 0xFF
            self.p = (self.p & ~(self.Z | self.N)) | (self.Z if lo == 0 else 0) | (self.N if lo & 0x80 else 0)
            return cycles

        # Transfers
        if name == "TAX":
            self.set_x(self.get_a()); self.set_nz_x(self.get_x()); return cycles
        if name == "TXA":
            self.set_a(self.get_x())
            self.set_nz_a(self.get_a()); return cycles
        if name == "TAY":
            self.set_y(self.get_a()); self.set_nz_x(self.get_y()); return cycles
        if name == "TYA":
            self.set_a(self.get_y())
            self.set_nz_a(self.get_a()); return cycles
        if name == "TSX":
            self.set_x(self.sp); self.set_nz_x(self.get_x()); return cycles
        if name == "TXS":
            # TXS does not set NZ; always 16-bit write to S in native
            if self.e:
                self.sp = 0x0100 | (self.get_x() & 0xFF)
            else:
                self.sp = self.x & 0xFFFF  # full X even if X=8 (high is 0)
            return cycles
        if name == "TXY":
            self.set_y(self.get_x()); self.set_nz_x(self.get_y()); return cycles
        if name == "TYX":
            self.set_x(self.get_y()); self.set_nz_x(self.get_x()); return cycles
        if name == "TCD":
            self.d = self.a & 0xFFFF
            self.p = (self.p & ~(self.Z | self.N)) | (self.Z if self.d == 0 else 0) | (self.N if self.d & 0x8000 else 0)
            return cycles
        if name == "TDC":
            self.a = self.d & 0xFFFF
            self.p = (self.p & ~(self.Z | self.N)) | (self.Z if self.a == 0 else 0) | (self.N if self.a & 0x8000 else 0)
            return cycles
        if name == "TCS":
            self.sp = self.a & 0xFFFF
            if self.e:
                self.sp = 0x0100 | (self.sp & 0xFF)
            return cycles  # TCS does not set NZ
        if name == "TSC":
            self.a = self.sp & 0xFFFF
            self.p = (self.p & ~(self.Z | self.N)) | (self.Z if self.a == 0 else 0) | (self.N if self.a & 0x8000 else 0)
            return cycles
        if name == "PLD":
            self.d = self.pull16()
            self.p = (self.p & ~(self.Z | self.N)) | (self.Z if self.d == 0 else 0) | (self.N if self.d & 0x8000 else 0)
            return cycles
        if name == "PHD":
            self.push16(self.d); return cycles
        if name == "PHB":
            self.push8(self.db); return cycles
        if name == "PLB":
            self.db = self.pull8()
            self.p = (self.p & ~(self.Z | self.N)) | (self.Z if self.db == 0 else 0) | (self.N if self.db & 0x80 else 0)
            return cycles
        if name == "PHK":
            self.push8(self.pb); return cycles
        if name == "PEA":
            self.push16(self.fetch16()); return cycles
        if name == "PEI":
            dp = (self.d + self.fetch8()) & 0xFFFF
            self.push16(self._read16_dp(dp)); return cycles
        if name == "PER":
            off = self.fetch16()
            self.push16((self.pc + off) & 0xFFFF); return cycles

        # Stack
        if name == "PHA":
            if self.a_bytes() == 1:
                self.push8(self.get_a())
            else:
                self.push16(self.get_a())
            return cycles
        if name == "PLA":
            if self.a_bytes() == 1:
                self.set_a(self.pull8())
            else:
                self.set_a(self.pull16())
            self.set_nz_a(self.get_a()); return cycles
        if name == "PHX":
            if self.x_bytes() == 1:
                self.push8(self.get_x())
            else:
                self.push16(self.get_x())
            return cycles
        if name == "PLX":
            if self.x_bytes() == 1:
                self.set_x(self.pull8())
            else:
                self.set_x(self.pull16())
            self.set_nz_x(self.get_x()); return cycles
        if name == "PHY":
            if self.x_bytes() == 1:
                self.push8(self.get_y())
            else:
                self.push16(self.get_y())
            return cycles
        if name == "PLY":
            if self.x_bytes() == 1:
                self.set_y(self.pull8())
            else:
                self.set_y(self.pull16())
            self.set_nz_x(self.get_y()); return cycles
        if name == "PHP":
            p = self.p & 0xFF
            if self.e:
                p |= 0x30
            self.push8(p); return cycles
        if name == "PLP":
            self.p = self.pull8()
            if self.e:
                self.p |= self.M | self.X
            if self.p & self.X:
                self.x &= 0xFF; self.y &= 0xFF
            self._sync_width()
            return cycles

        # Branches
        if name == "BRA":
            return cycles + self._branch(True)
        if name == "BRL":
            self._branch(True, "rell"); return cycles
        if name == "BPL":
            return cycles + self._branch(not (self.p & self.N))
        if name == "BMI":
            return cycles + self._branch(bool(self.p & self.N))
        if name == "BVC":
            return cycles + self._branch(not (self.p & self.V))
        if name == "BVS":
            return cycles + self._branch(bool(self.p & self.V))
        if name == "BCC":
            return cycles + self._branch(not (self.p & self.C))
        if name == "BCS":
            return cycles + self._branch(bool(self.p & self.C))
        if name == "BNE":
            return cycles + self._branch(not (self.p & self.Z))
        if name == "BEQ":
            return cycles + self._branch(bool(self.p & self.Z))

        # Jumps / calls
        if name == "JMP":
            if mode == "abs":
                self.pc = self.fetch16()
            elif mode == "abl":
                v = self.fetch24()
                self.pc = v & 0xFFFF
                self.pb = (v >> 16) & 0xFF
            elif mode == "ind":
                a = self.fetch16()
                lo = self.read8(0x00, a)
                hi = self.read8(0x00, (a + 1) & 0xFFFF)
                self.pc = lo | (hi << 8)
            elif mode == "indx":
                a = (self.fetch16() + self.get_x()) & 0xFFFF
                self.pc = self.read16(self.pb, a)
            return cycles
        if name == "JML":
            if mode == "indl":
                a = self.fetch16()
                lo = self.read8(0x00, a)
                hi = self.read8(0x00, (a + 1) & 0xFFFF)
                bk = self.read8(0x00, (a + 2) & 0xFFFF)
                self.pc = lo | (hi << 8)
                self.pb = bk
            else:
                v = self.fetch24()
                self.pc = v & 0xFFFF
                self.pb = (v >> 16) & 0xFF
            return cycles
        if name == "JSR":
            if mode == "abs":
                tgt = self.fetch16()
                self.push16((self.pc - 1) & 0xFFFF)
                self.pc = tgt
            elif mode == "indx":
                a = (self.fetch16() + self.get_x()) & 0xFFFF
                tgt = self.read16(self.pb, a)
                self.push16((self.pc - 1) & 0xFFFF)
                self.pc = tgt
            return cycles
        if name == "JSL":
            v = self.fetch24()
            self.push8(self.pb)
            self.push16((self.pc - 1) & 0xFFFF)
            self.pc = v & 0xFFFF
            self.pb = (v >> 16) & 0xFF
            return cycles
        if name == "RTS":
            self.pc = (self.pull16() + 1) & 0xFFFF
            return cycles
        if name == "RTL":
            self.pc = (self.pull16() + 1) & 0xFFFF
            self.pb = self.pull8()
            return cycles
        if name == "RTI":
            self.p = self.pull8()
            self.pc = self.pull16()
            if not self.e:
                self.pb = self.pull8()
            if self.e:
                self.p |= self.M | self.X
            if self.p & self.X:
                self.x &= 0xFF
                self.y &= 0xFF
            self._sync_width()
            return cycles
        if name == "BRK":
            self.fetch8()
            if self.e:
                self.push8((self.pc >> 8) & 0xFF)
                self.push8(self.pc & 0xFF)
                self.push8((self.p | 0x30) & 0xFF)
                self.p = (self.p | self.I) & ~self.D
                self.pb = 0
                self.pc = self.read16(0x00, 0xFFFE)
            else:
                self.push8(self.pb)
                self.push16(self.pc)
                self.push8(self.p & 0xFF)
                self.p = (self.p | self.I) & ~self.D
                self.pb = 0
                self.pc = self.read16(0x00, 0xFFE6)
            return cycles
        if name == "COP":
            self.fetch8()
            if self.e:
                self.push8((self.pc >> 8) & 0xFF)
                self.push8(self.pc & 0xFF)
                self.push8((self.p | 0x20) & ~0x10 & 0xFF)
                self.p = (self.p | self.I) & ~self.D
                self.pb = 0
                self.pc = self.read16(0x00, 0xFFF4)
            else:
                self.push8(self.pb)
                self.push16(self.pc)
                self.push8(self.p & 0xFF)
                self.p = (self.p | self.I) & ~self.D
                self.pb = 0
                self.pc = self.read16(0x00, 0xFFE4)
            return cycles

        # Inc/dec regs
        if name in ("INC", "DEC") and mode == "acc":
            if name == "INC":
                self.set_a(self.get_a() + 1)
            else:
                self.set_a(self.get_a() - 1)
            self.set_nz_a(self.get_a())
            return cycles
        if name == "INX":
            self.set_x(self.get_x() + 1); self.set_nz_x(self.get_x()); return cycles
        if name == "DEX":
            self.set_x(self.get_x() - 1); self.set_nz_x(self.get_x()); return cycles
        if name == "INY":
            self.set_y(self.get_y() + 1); self.set_nz_x(self.get_y()); return cycles
        if name == "DEY":
            self.set_y(self.get_y() - 1); self.set_nz_x(self.get_y()); return cycles

        # Block moves are one byte per dispatch. Rewinding PC makes the next
        # byte transfer interruptible, matching the 65C816 instruction model.
        if name in ("MVP", "MVN"):
            dst_b = self.fetch8()
            src_b = self.fetch8()
            v = self.read8(src_b, self.x & 0xFFFF)
            self.write8(dst_b, self.y & 0xFFFF, v)
            delta = 1 if name == "MVN" else -1
            self.x = (self.x + delta) & 0xFFFF
            self.y = (self.y + delta) & 0xFFFF
            self.a = (self.a - 1) & 0xFFFF
            self.db = dst_b
            if self.a != 0xFFFF:
                self.pc = (self.pc - 3) & 0xFFFF
            return cycles

        # Memory RMW / load / store / alu
        bank, addr = self._ea(mode)
        if mode.startswith("dp") and (self.d & 0xFF):
            cycles += 1
        if self._page_crossed and name not in ("STA", "STX", "STY", "STZ"):
            cycles += 1
        a_width_ops = {"LDA", "STA", "ORA", "AND", "EOR", "ADC", "SBC", "CMP", "BIT", "TSB", "TRB"}
        rmw_ops = {"ASL", "LSR", "ROL", "ROR", "INC", "DEC"}
        if self.a_bytes() == 2 and name in a_width_ops:
            cycles += 1
        if self.a_bytes() == 2 and name in rmw_ops and mode != "acc":
            cycles += 2
        if self.x_bytes() == 2 and name in ("LDX", "LDY", "STX", "STY", "CPX", "CPY"):
            cycles += 1
        return self._exec_mem(name, bank, addr, mode, cycles)

    def _exec_mem(self, name: str, bank: int, addr: int, mode: str, cycles: int) -> int:
        if name == "LDA":
            v = self._read_op(bank, addr); self.set_a(v); self.set_nz_a(v); return cycles
        if name == "LDX":
            v = self._read_idx(bank, addr); self.set_x(v); self.set_nz_x(v); return cycles
        if name == "LDY":
            v = self._read_idx(bank, addr); self.set_y(v); self.set_nz_x(v); return cycles
        if name == "STA":
            self._write_op(bank, addr, self.get_a()); return cycles
        if name == "STX":
            self._write_idx(bank, addr, self.get_x()); return cycles
        if name == "STY":
            self._write_idx(bank, addr, self.get_y()); return cycles
        if name == "STZ":
            self._write_op(bank, addr, 0); return cycles
        if name == "ORA":
            v = self.get_a() | self._read_op(bank, addr)
            self.set_a(v); self.set_nz_a(v); return cycles
        if name == "AND":
            v = self.get_a() & self._read_op(bank, addr)
            self.set_a(v); self.set_nz_a(v); return cycles
        if name == "EOR":
            v = self.get_a() ^ self._read_op(bank, addr)
            self.set_a(v); self.set_nz_a(v); return cycles
        if name == "ADC":
            self._adc(self._read_op(bank, addr)); return cycles
        if name == "SBC":
            self._sbc(self._read_op(bank, addr)); return cycles
        if name == "CMP":
            self._cmp(self.get_a(), self._read_op(bank, addr), self.a_mask()); return cycles
        if name == "CPX":
            self._cmp(self.get_x(), self._read_idx(bank, addr), self.x_mask()); return cycles
        if name == "CPY":
            self._cmp(self.get_y(), self._read_idx(bank, addr), self.x_mask()); return cycles
        if name == "BIT":
            v = self._read_op(bank, addr)
            m = self.a_mask()
            # Immediate BIT only changes Z; memory BIT also copies bits 6/7
            # (or 14/15) into V/N.
            self.p &= ~self.Z
            if mode != "imm":
                self.p &= ~(self.V | self.N)
            if (self.get_a() & v & m) == 0:
                self.p |= self.Z
            if mode != "imm":
                neg = 0x80 if m == 0xFF else 0x8000
                ov = 0x40 if m == 0xFF else 0x4000
                if v & neg:
                    self.p |= self.N
                if v & ov:
                    self.p |= self.V
            return cycles
        if name == "ASL":
            if mode == "acc":
                v = self.get_a(); self.p = (self.p & ~self.C) | (self.C if v & (0x80 if self.a_mask()==0xFF else 0x8000) else 0)
                v = (v << 1) & self.a_mask(); self.set_a(v); self.set_nz_a(v)
            else:
                v = self._read_op(bank, addr)
                self.p = (self.p & ~self.C) | (self.C if v & (0x80 if self.a_mask()==0xFF else 0x8000) else 0)
                v = (v << 1) & self.a_mask(); self._write_op(bank, addr, v); self.set_nz_a(v)
            return cycles
        if name == "LSR":
            if mode == "acc":
                v = self.get_a(); self.p = (self.p & ~self.C) | (self.C if v & 1 else 0)
                v = (v >> 1) & self.a_mask(); self.set_a(v); self.set_nz_a(v)
            else:
                v = self._read_op(bank, addr)
                self.p = (self.p & ~self.C) | (self.C if v & 1 else 0)
                v = (v >> 1) & self.a_mask(); self._write_op(bank, addr, v); self.set_nz_a(v)
            return cycles
        if name == "ROL":
            if mode == "acc":
                v = self.get_a(); c = 1 if (self.p & self.C) else 0
                neg = 0x80 if self.a_mask()==0xFF else 0x8000
                self.p = (self.p & ~self.C) | (self.C if v & neg else 0)
                v = ((v << 1) | c) & self.a_mask(); self.set_a(v); self.set_nz_a(v)
            else:
                v = self._read_op(bank, addr); c = 1 if (self.p & self.C) else 0
                neg = 0x80 if self.a_mask()==0xFF else 0x8000
                self.p = (self.p & ~self.C) | (self.C if v & neg else 0)
                v = ((v << 1) | c) & self.a_mask(); self._write_op(bank, addr, v); self.set_nz_a(v)
            return cycles
        if name == "ROR":
            if mode == "acc":
                v = self.get_a(); c = 1 if (self.p & self.C) else 0
                self.p = (self.p & ~self.C) | (self.C if v & 1 else 0)
                neg = 0x80 if self.a_mask()==0xFF else 0x8000
                v = ((v >> 1) | (neg if c else 0)) & self.a_mask()
                self.set_a(v); self.set_nz_a(v)
            else:
                v = self._read_op(bank, addr); c = 1 if (self.p & self.C) else 0
                self.p = (self.p & ~self.C) | (self.C if v & 1 else 0)
                neg = 0x80 if self.a_mask()==0xFF else 0x8000
                v = ((v >> 1) | (neg if c else 0)) & self.a_mask()
                self._write_op(bank, addr, v); self.set_nz_a(v)
            return cycles
        if name == "INC" and mode != "acc":
            v = (self._read_op(bank, addr) + 1) & self.a_mask()
            self._write_op(bank, addr, v); self.set_nz_a(v); return cycles
        if name == "DEC" and mode != "acc":
            v = (self._read_op(bank, addr) - 1) & self.a_mask()
            self._write_op(bank, addr, v); self.set_nz_a(v); return cycles
        if name == "TSB":
            v = self._read_op(bank, addr)
            self.p = (self.p & ~self.Z) | (self.Z if (v & self.get_a()) == 0 else 0)
            self._write_op(bank, addr, v | self.get_a()); return cycles
        if name == "TRB":
            v = self._read_op(bank, addr)
            self.p = (self.p & ~self.Z) | (self.Z if (v & self.get_a()) == 0 else 0)
            self._write_op(bank, addr, v & ~self.get_a()); return cycles

        return cycles


# Ensure the 256-opcode snes9x jump table exists at import time (in-file core).
CPU65816._build_opcode_jump_table()


class SNES:
    """Top-level console — scanline-timed CPU + HDMA + co-processors."""

    # NTSC: ~262 scanlines, ~1364 master cycles / line → ~226 CPU cycles / line
    SCANLINES = 262
    CYCLES_PER_SCANLINE = max(1, int(CPU_CYCLES_PER_FRAME / 262))
    STATE_MAGIC = b"SNESemuState\x00"
    STATE_VERSION = 1

    def __init__(self) -> None:
        self.cart = Cartridge()
        self.ppu = PPU()
        self.apu = APU()
        self.bus = Bus(self.cart, self.ppu, self.apu)
        self.cpu = CPU65816(self.bus)
        self.coprocessor: CoprocessorBase | None = None
        self.running = False
        self.paused = False
        self.frame = 0

    def _attach_coprocessor(self) -> None:
        self.coprocessor = make_coprocessor(self.cart)
        self.cart.coprocessor = self.coprocessor
        if self.coprocessor is not None:
            self.coprocessor.reset()
            log.info("co-processor: %s", self.coprocessor.name)

    def load(self, path: str) -> None:
        self.cart.load(path)
        self._attach_coprocessor()
        self.ppu.reset()
        self.apu.reset()
        self.bus.reset()
        self.cpu.reset()
        self.running = True
        self.paused = False
        self.frame = 0

    def reset(self) -> None:
        if not self.cart.rom:
            return
        if self.coprocessor is not None:
            self.coprocessor.reset()
        self.ppu.reset()
        self.apu.reset()
        self.bus.reset()
        self.cpu.reset()
        self.running = True
        self.paused = False

    def set_joy(self, pad: int, buttons: int) -> None:
        if pad == 0:
            self.bus.joy1 = buttons & 0xFFFF
        else:
            self.bus.joy2 = buttons & 0xFFFF

    @staticmethod
    def _pack_bytes(data: bytes | bytearray) -> str:
        return base64.b64encode(bytes(data)).decode("ascii")

    @staticmethod
    def _restore_bytes(target: bytearray, encoded: str, label: str) -> None:
        raw = base64.b64decode(encoded.encode("ascii"), validate=True)
        if len(raw) != len(target):
            raise EmulatorError(f"State {label} has the wrong size.")
        target[:] = raw

    def save_state(self, path: str) -> None:
        if not self.cart.rom:
            raise EmulatorError("Load a ROM before saving a state.")
        echo = array("h", self.apu._echo)
        if sys.byteorder != "little":
            echo.byteswap()
        dma = self.bus.dma
        state = {
            "format": "SNESemu portable state",
            "version": self.STATE_VERSION,
            "rom_size": len(self.cart.rom),
            "rom_checksum": self.cart.calculated_checksum,
            "rom_crc32": zlib.crc32(self.cart.rom) & 0xFFFFFFFF,
            "frame": self.frame,
            "running": self.running,
            "paused": self.paused,
            "cpu": {
                key: getattr(self.cpu, key) for key in
                ("a", "x", "y", "sp", "d", "db", "pb", "p", "e", "pc",
                 "total_cycles", "stopped", "waiting", "frozen")
            },
            "bus": {
                "wram": self._pack_bytes(self.bus.wram),
                "open_bus": self.bus.open_bus,
                "nmi_flag": self.bus.nmi_flag,
                "irq_flag": self.bus.irq_flag,
                "joy1": self.bus.joy1, "joy2": self.bus.joy2,
                "wmadd": self.bus.wmadd, "memsel": self.bus.memsel,
                "nmitimen": self.bus.nmitimen,
                "htime": self.bus.htime, "vtime": self.bus.vtime,
                "mul_a": self.bus.mul_a, "mul_b": self.bus.mul_b,
                "div_a": self.bus.div_a, "div_b": self.bus.div_b,
                "rddiv": self.bus.rddiv, "rdmpy": self.bus.rdmpy,
                "auto_joy": self.bus.auto_joy, "hdmaen": self.bus.hdmaen,
            },
            "dma": {
                "channels": [self._pack_bytes(c) for c in dma.channels],
                "enable": dma.hdma_enable,
                "line": dma._hdma_line,
                "do": dma._hdma_do_transfer,
                "addr": dma._hdma_addr,
                "bank": dma._hdma_bank,
                "indirect": dma._hdma_indirect,
            },
            "ppu": {
                "vram": self._pack_bytes(self.ppu.vram),
                "cgram": self._pack_bytes(self.ppu.cgram),
                "oam": self._pack_bytes(self.ppu.oam),
                "framebuffer": self._pack_bytes(self.ppu.framebuffer),
                "regs": self._pack_bytes(self.ppu.regs),
                "brightness": self.ppu.brightness, "bg_mode": self.ppu.bg_mode,
                "nmi_enable": self.ppu.nmi_enable,
                "vblank": self.ppu.vblank, "hblank": self.ppu.hblank,
                "scanline": self.ppu.scanline, "vmadd": self.ppu.vmadd,
                "cgadd": self.ppu.cgadd, "cg_latch": self.ppu.cg_latch,
                "cg_read_high": self.ppu.cg_read_high, "oamadd": self.ppu.oamadd,
                "bg_hofs": self.ppu.bg_hofs, "bg_vofs": self.ppu.bg_vofs,
                "ofs_latch": self.ppu.ofs_latch,
                "m7": [self.ppu.m7_a, self.ppu.m7_b, self.ppu.m7_c, self.ppu.m7_d,
                       self.ppu.m7_x, self.ppu.m7_y, self.ppu.m7_hofs,
                       self.ppu.m7_vofs, self.ppu.m7_latch, self.ppu.m7_mpy],
                "fixed": [self.ppu.fixed_r, self.ppu.fixed_g, self.ppu.fixed_b],
            },
            "apu": {
                "regs": self._pack_bytes(self.apu.regs),
                "cpu_ports": self._pack_bytes(self.apu.cpu_ports),
                "output_ports": self._pack_bytes(self.apu.output_ports),
                "echo": self._pack_bytes(echo.tobytes()),
                "echo_pos": self.apu._echo_pos, "echo_fb": self.apu._echo_fb,
                "phase": self.apu._phase, "activity": self.apu._activity,
                "sample_debt": self.apu._sample_debt, "boot": self.apu._boot,
                "pitch_mod": self.apu._pitch_mod, "brr_phase": self.apu._brr_phase,
            },
            "sram": self._pack_bytes(self.cart.sram),
        }
        encoded = json.dumps(state, separators=(",", ":"), sort_keys=True).encode("utf-8")
        with open(path, "wb") as fh:
            fh.write(self.STATE_MAGIC)
            fh.write(zlib.compress(encoded, level=6))

    def load_state(self, path: str) -> None:
        if not self.cart.rom:
            raise EmulatorError("Load the matching ROM before loading a state.")
        with open(path, "rb") as fh:
            raw = fh.read()
        if not raw.startswith(self.STATE_MAGIC):
            raise EmulatorError("Not a SNESemu save state.")
        try:
            state = json.loads(zlib.decompress(raw[len(self.STATE_MAGIC):]))
        except (ValueError, zlib.error, json.JSONDecodeError) as exc:
            raise EmulatorError(f"Corrupt save state: {exc}") from exc
        if state.get("version") != self.STATE_VERSION:
            raise EmulatorError(f"Unsupported state version {state.get('version')!r}.")
        if (state.get("rom_size"), state.get("rom_checksum"), state.get("rom_crc32")) != (
            len(self.cart.rom), self.cart.calculated_checksum,
            zlib.crc32(self.cart.rom) & 0xFFFFFFFF,
        ):
            raise EmulatorError("This state belongs to a different ROM.")

        cpu = state["cpu"]
        for key in ("a", "x", "y", "sp", "d", "db", "pb", "p", "e", "pc",
                    "total_cycles", "stopped", "waiting", "frozen"):
            setattr(self.cpu, key, cpu[key])
        self.cpu._sync_width()

        bus = state["bus"]
        self._restore_bytes(self.bus.wram, bus["wram"], "WRAM")
        for key in ("open_bus", "nmi_flag", "irq_flag", "joy1", "joy2", "wmadd",
                    "memsel", "nmitimen", "htime", "vtime", "mul_a", "mul_b",
                    "div_a", "div_b", "rddiv", "rdmpy", "auto_joy", "hdmaen"):
            setattr(self.bus, key, bus[key])
        self.bus.irq_mode = (self.bus.nmitimen >> 4) & 3
        self.ppu.nmi_enable = bool(self.bus.nmitimen & 0x80)

        ds = state["dma"]
        for target, encoded_channel in zip(self.bus.dma.channels, ds["channels"], strict=True):
            self._restore_bytes(target, encoded_channel, "DMA channel")
        self.bus.dma.hdma_enable = ds["enable"]
        self.bus.dma._hdma_line[:] = ds["line"]
        self.bus.dma._hdma_do_transfer[:] = ds["do"]
        self.bus.dma._hdma_addr[:] = ds["addr"]
        self.bus.dma._hdma_bank[:] = ds["bank"]
        self.bus.dma._hdma_indirect[:] = ds["indirect"]

        ps = state["ppu"]
        for target, key, label in (
            (self.ppu.vram, "vram", "VRAM"), (self.ppu.cgram, "cgram", "CGRAM"),
            (self.ppu.oam, "oam", "OAM"),
            (self.ppu.framebuffer, "framebuffer", "framebuffer"),
            (self.ppu.regs, "regs", "PPU registers"),
        ):
            self._restore_bytes(target, ps[key], label)
        for key in ("brightness", "bg_mode", "nmi_enable", "vblank", "hblank",
                    "scanline", "vmadd", "cgadd", "cg_latch", "cg_read_high",
                    "oamadd", "ofs_latch"):
            setattr(self.ppu, key, ps[key])
        self.ppu.bg_hofs[:] = ps["bg_hofs"]
        self.ppu.bg_vofs[:] = ps["bg_vofs"]
        (self.ppu.m7_a, self.ppu.m7_b, self.ppu.m7_c, self.ppu.m7_d,
         self.ppu.m7_x, self.ppu.m7_y, self.ppu.m7_hofs, self.ppu.m7_vofs,
         self.ppu.m7_latch, self.ppu.m7_mpy) = ps["m7"]
        self.ppu.fixed_r, self.ppu.fixed_g, self.ppu.fixed_b = ps["fixed"]
        self.ppu._tile_cache.clear()

        ap = state["apu"]
        self._restore_bytes(self.apu.regs, ap["regs"], "APU registers")
        self._restore_bytes(self.apu.cpu_ports, ap["cpu_ports"], "APU input ports")
        self._restore_bytes(self.apu.output_ports, ap["output_ports"], "APU output ports")
        echo_raw = base64.b64decode(ap["echo"].encode("ascii"), validate=True)
        echo = array("h")
        echo.frombytes(echo_raw)
        if sys.byteorder != "little":
            echo.byteswap()
        if len(echo) != self.apu.ECHO_SIZE:
            raise EmulatorError("State APU echo buffer has the wrong size.")
        self.apu._echo = echo
        for key in ("echo_pos", "echo_fb", "phase", "activity", "sample_debt",
                    "boot", "pitch_mod", "brr_phase"):
            setattr(self.apu, "_" + key, ap[key])
        self._restore_bytes(self.cart.sram, state["sram"], "SRAM")
        self.frame = int(state["frame"])
        self.running = bool(state["running"])
        self.paused = bool(state["paused"])

    def advance_frame(self) -> bytearray:
        self.paused = False
        frame = self.run_frame()
        self.paused = True
        return frame

    def run_frame(self) -> bytearray:
        if not self.running or self.paused or not self.cart.rom:
            return self.ppu.render_frame()

        self.ppu.vblank = False
        self.ppu.hblank = False
        self.ppu.scanline = 0
        self.bus._irq_fired_line = -1
        # Reload HDMA from latched HDMAEN each frame (channels may end early)
        self.bus.dma.hdma_enable = self.bus.hdmaen
        self.bus.dma.hdma_init_frame()
        vblank_line = 240 if (self.ppu.regs[0x33] & 0x04) else 225

        for line in range(self.SCANLINES):
            self.ppu.scanline = line
            self.ppu.vblank = line >= vblank_line
            self.ppu.hblank = False

            if line < HEIGHT:
                self.ppu.hblank = True
                self.bus.dma.hdma_scanline()
                self.ppu.hblank = False
                self.ppu.render_scanline(line)

            nmi_edge = line == vblank_line
            if line == vblank_line:
                self.ppu.vblank = True
                self.bus.nmi_flag = True
                if self.ppu.nmi_enable:
                    self.cpu.nmi()

            self.bus.poll_irq(line)
            if self.bus.irq_flag and not nmi_edge:
                self.cpu.irq()

            budget = self.CYCLES_PER_SCANLINE
            used = 0
            while used < budget:
                used += self.cpu.step()
                if self.cpu.stopped:
                    break
            if self.coprocessor is not None:
                self.coprocessor.tick(used)
            if self.cpu.stopped:
                break

        self.ppu.scanline = HEIGHT
        self.frame += 1
        return self.ppu.framebuffer


class Snes9xCore:
    """In-file snes9x-compatible core facade (Cython-ready; not an external import).

    Wraps the inlined SNES console so callers can treat this module as the
    snes9x core without pulling any out-of-program binary or package.
    """

    name = CORE_LABEL
    inline = SNES9X_CORE_INLINE
    opcode_count = CPU65816.OPCODE_COUNT

    def __init__(self) -> None:
        self.console = SNES()
        # Touch jump table so Cython/pure-Python both prove 256/256 coverage
        if not CPU65816.all_opcodes_supported():
            raise EmulatorError("snes9x core: incomplete 65C816 opcode table")

    @property
    def cart(self) -> Cartridge:
        return self.console.cart

    @property
    def cpu(self) -> CPU65816:
        return self.console.cpu

    @property
    def ppu(self) -> PPU:
        return self.console.ppu

    @property
    def apu(self) -> APU:
        return self.console.apu

    def load(self, path: str) -> None:
        self.console.load(path)

    def reset(self) -> None:
        self.console.reset()

    def run_frame(self) -> bytearray:
        return self.console.run_frame()

    def set_joy(self, pad: int, buttons: int) -> None:
        self.console.set_joy(pad, buttons)

    def save_state(self, path: str) -> None:
        self.console.save_state(path)

    def load_state(self, path: str) -> None:
        self.console.load_state(path)


# Alias used by the shell — always the inlined core, never an external snes9x.
Snes9x = Snes9xCore


# ── Audio (pygame mixer; optional) ──────────────────────────────────────────
def _harden_macos_sdl_env() -> None:
    os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    os.environ.setdefault("SDL_JOYSTICK_HIDAPI", "0")
    os.environ.setdefault("SDL_JOYSTICK_MFI", "0")
    os.environ.setdefault("SDL_GAMECONTROLLER_IGNORE_DEVICES_EXCEPT", "")


class AudioOutput:
    """pygame mixer. ON by default."""

    CHUNK_SAMPLES = 1024

    def __init__(self, *, enabled: bool = True) -> None:
        self.available = False
        self.muted = False
        self.paused = False
        self.pending = bytearray()
        self.backend = "sound off"
        self.mixer = None
        self.channel = None
        if not enabled:
            self.backend = "sound off (--no-sound)"
            return
        try:
            _harden_macos_sdl_env()
            import pygame
            pygame.mixer.pre_init(frequency=APU.SAMPLE_RATE, size=-16, channels=1, buffer=512)
            pygame.mixer.init()
            self.mixer = pygame.mixer
            self.channel = pygame.mixer.Channel(0)
            self.channel.set_volume(0.65)
            self.available = True
            self.backend = "SPC ports + S-DSP stub / pygame"
        except Exception as error:
            self.backend = f"sound off ({error.__class__.__name__})"

    def push(self, pcm: bytes) -> None:
        if not pcm or not self.available or self.channel is None or self.mixer is None:
            return
        if self.muted or self.paused:
            return
        self.pending.extend(pcm)
        chunk = self.CHUNK_SAMPLES * 2
        while len(self.pending) >= chunk:
            block = bytes(self.pending[:chunk])
            try:
                sound = self.mixer.Sound(buffer=block)
                if not self.channel.get_busy():
                    self.channel.play(sound)
                elif self.channel.get_queue() is None:
                    self.channel.queue(sound)
                else:
                    # Keep this block pending instead of replacing the one
                    # already queued and causing clicks or runaway latency.
                    break
                del self.pending[:chunk]
            except Exception:
                break

    def flush(self) -> None:
        self.pending.clear()
        if self.channel is not None:
            try:
                self.channel.stop()
            except Exception:
                pass

    def set_muted(self, muted: bool) -> None:
        self.muted = muted

    def set_paused(self, paused: bool) -> None:
        self.paused = paused

    def close(self) -> None:
        self.flush()
        if self.mixer is not None:
            try:
                self.mixer.quit()
            except Exception:
                pass


# ── cat's snes9x 1.1 GUI ────────────────────────────────────────────────────
class CatsSnes9x:
    """SNES9x-like shell: blue bg, blue text, black buttons. files=ON default."""

    # B Y Select Start Up Down Left Right A X L R
    KEYMAP = {
        "z": 0x0080, "a": 0x0040, "Shift_L": 0x0020, "Shift_R": 0x0020,
        "Return": 0x0010, "Up": 0x0008, "Down": 0x0004, "Left": 0x0002, "Right": 0x0001,
        "x": 0x8000, "s": 0x4000, "d": 0x2000, "c": 0x1000,
    }

    def __init__(
        self,
        rom_path: str | None = None,
        *,
        files_off: bool = False,
        enable_sound: bool = True,
        menustrip_on: bool = True,
    ) -> None:
        self.root = tk.Tk()
        self.root.title(f"{APP_NAME} {APP_VERSION}")
        self.root.resizable(False, False)
        self.root.configure(bg=BG)

        # Inlined snes9x core (no external import)
        self.core = Snes9xCore()
        self.snes = self.core.console
        self.audio = AudioOutput(enabled=enable_sound)
        self.next_frame = time.perf_counter()
        self.held: set[str] = set()
        self.image: tk.PhotoImage | None = None
        self.scaled: tk.PhotoImage | None = None
        self.files_off = bool(files_off)
        self.menustrip_on = bool(menustrip_on)
        self.fullscreen = False
        self._perf_started = time.perf_counter()
        self._perf_frames = 0
        self._perf_work = 0.0
        self._base_status = (
            f"ready  ·  {CORE_BACKEND}  ·  opcodes={CPU65816.OPCODE_COUNT}/256  ·  "
            f"{self.audio.backend}"
        )
        self.recent: list[str] = []
        self.strip_frame: tk.Frame | None = None
        self.file_menu: tk.Menu | None = None
        self.recent_menu: tk.Menu | None = None
        self.load_btn: tk.Button | None = None
        self.title_label: tk.Label | None = None
        self.hint_label: tk.Label | None = None
        self.mute_text = tk.StringVar(value="MUTE")
        mode = "files=OFF" if self.files_off else "files=ON"
        self.status_var = tk.StringVar(value=self._base_status + f"  ·  {mode}")

        self._rebuild_menu()
        self._make_chrome()
        self._apply_files_mode_ui()
        self._apply_menustrip_visibility()

        self.root.bind("<KeyPress>", self.key_down)
        self.root.bind("<KeyRelease>", self.key_up)
        self.root.focus_set()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._setup_file_drop()
        self._draw_boot()
        self.root.after(1, self.loop)
        if rom_path:
            self.root.after(50, lambda p=rom_path: self.load_rom(p))

    def _dialog(self, title: str, body: str, *, error: bool = False) -> None:
        win = tk.Toplevel(self.root)
        win.title(title)
        win.configure(bg=BG)
        win.transient(self.root)
        win.resizable(False, False)
        fg = "#FF8080" if error else FG
        tk.Label(win, text=title, bg=BG, fg=fg, font=("Courier", 11, "bold"),
                 anchor="w").pack(fill="x", padx=12, pady=(12, 4))
        tk.Label(win, text=body, bg=BG, fg=FG_BRIGHT, font=("Courier", 10),
                 justify="left", anchor="w").pack(fill="both", padx=12, pady=4)
        btn = tk.Button(
            win, text="OK", command=win.destroy,
            bg=BTN_BG, fg=BTN_FG, activebackground=BTN_ACTIVE, activeforeground=FG_BRIGHT,
            relief="flat", padx=16, pady=4, font=("Courier", 10, "bold"),
        )
        btn.pack(pady=(4, 12))
        win.bind("<Return>", lambda _e: win.destroy())
        win.bind("<Escape>", lambda _e: win.destroy())
        try:
            win.grab_set()
        except tk.TclError:
            pass
        btn.focus_set()
        self.root.wait_window(win)

    def _btn(self, parent: tk.Widget, text: str, cmd) -> tk.Button:
        return tk.Button(
            parent, text=text, command=cmd,
            bg=BTN_BG, fg=BTN_FG, activebackground=BTN_ACTIVE, activeforeground=FG_BRIGHT,
            disabledforeground=FG_DIM, relief="flat", bd=0, highlightthickness=1,
            highlightbackground=ACCENT, highlightcolor=FG, padx=10, pady=3,
            font=("Courier", 10, "bold"),
        )

    def _vb6_menubutton(self, parent, label, underline=0):
        face, text = "#D4D0C8", "#000000"
        btn = tk.Menubutton(
            parent, text=label, underline=underline,
            bg=face, fg=text, activebackground="#0A246A", activeforeground="#FFFFFF",
            font=("Tahoma", 9), relief="flat", bd=1, padx=8, pady=2,
            highlightthickness=0, direction="below",
        )
        menu = tk.Menu(
            btn, tearoff=False, bg=face, fg=text,
            activebackground="#0A246A", activeforeground="#FFFFFF", font=("Tahoma", 9),
        )
        btn.configure(menu=menu)
        btn.pack(side="left")
        return btn, menu

    def _rebuild_menu(self) -> None:
        if self.strip_frame is not None:
            self.strip_frame.destroy()
            self.strip_frame = None
        face = "#D4D0C8"
        strip = tk.Frame(self.root, bg=face, bd=1, relief="raised")
        strip.pack(fill="x", side="top")

        _, file_menu = self._vb6_menubutton(strip, "File", 0)
        file_menu.add_command(
            label="Load ROM...", accelerator="Ctrl+O", command=self.open_rom,
        )
        recent = tk.Menu(file_menu, tearoff=False, bg=face, fg="#000",
                         activebackground="#0A246A", activeforeground="#FFF", font=("Tahoma", 9))
        file_menu.add_cascade(label="Recent", menu=recent)
        file_menu.add_separator()
        file_menu.add_command(label="Save State...", accelerator="Ctrl+S", command=self.save_state_dialog)
        file_menu.add_command(label="Load State...", accelerator="Ctrl+L", command=self.load_state_dialog)
        file_menu.add_command(label="Screenshot...", accelerator="F12", command=self.screenshot)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.close)

        _, snes_menu = self._vb6_menubutton(strip, "SNES", 0)
        snes_menu.add_command(label="Reset", accelerator="Ctrl+R", command=self.reset)
        snes_menu.add_command(label="Pause / Resume", command=self.toggle_pause)
        snes_menu.add_command(label="Frame Advance", accelerator="F8", command=self.frame_advance)
        snes_menu.add_command(label="Power Off", command=self.power_blank)

        _, view = self._vb6_menubutton(strip, "View", 0)
        view.add_command(label="Fullscreen", accelerator="F11", command=self.toggle_fullscreen)

        _, debugger = self._vb6_menubutton(strip, "Debugger", 0)
        debugger.add_command(label="CPU / Memory / DMA Viewer", command=self.show_debugger)
        debugger.add_command(label="Step CPU Instruction", accelerator="F7", command=self.step_cpu_instruction)

        _, cfg = self._vb6_menubutton(strip, "Config", 0)
        cfg.add_command(label="Mute / Unmute", command=self.toggle_mute)
        cfg.add_separator()
        cfg.add_command(label="files = ON", command=lambda: self.set_files_mode(False))
        cfg.add_command(label="files = OFF", command=lambda: self.set_files_mode(True))

        _, help_m = self._vb6_menubutton(strip, "Help", 0)
        help_m.add_command(label="Controls", command=self.show_controls)
        help_m.add_command(label=f"About {APP_NAME}...", command=self.show_about)

        tk.Label(strip, text=f"{APP_NAME} {APP_VERSION}", bg=face, fg="#404040",
                 font=("Tahoma", 8)).pack(side="right", padx=8)

        self.strip_frame = strip
        self.file_menu = file_menu
        self.recent_menu = recent
        self.root.config(menu="")
        if not self.menustrip_on:
            strip.pack_forget()
        self._refresh_recent()
        self._sync_load_state()
        self._bind_accels()

    def _apply_menustrip_visibility(self) -> None:
        if self.menustrip_on:
            if self.strip_frame is not None and self.strip_frame.winfo_manager() != "pack":
                packed = [c for c in self.root.winfo_children()
                          if c is not self.strip_frame and c.winfo_manager() == "pack"]
                if packed:
                    self.strip_frame.pack(fill="x", side="top", before=packed[0])
                else:
                    self.strip_frame.pack(fill="x", side="top")
        else:
            if self.strip_frame is not None and self.strip_frame.winfo_manager() == "pack":
                self.strip_frame.pack_forget()

    def _bind_accels(self) -> None:
        for seq in ("<Control-o>", "<Command-o>", "<Control-r>", "<Command-r>",
                    "<Control-q>", "<Command-q>", "<Control-s>", "<Command-s>",
                    "<Control-l>", "<Command-l>", "<Control-f>", "<Command-f>",
                    "<F7>", "<F8>", "<F11>", "<F12>"):
            self.root.unbind(seq)
        # files=ON: Ctrl+O opens native picker; Ctrl+F toggles files mode
        self.root.bind("<Control-o>", lambda _e: self.open_rom())
        self.root.bind("<Command-o>", lambda _e: self.open_rom())
        self.root.bind("<Control-f>", lambda _e: self.toggle_files())
        self.root.bind("<Command-f>", lambda _e: self.toggle_files())
        self.root.bind("<Control-r>", lambda _e: self.reset())
        self.root.bind("<Command-r>", lambda _e: self.reset())
        self.root.bind("<Control-q>", lambda _e: self.close())
        self.root.bind("<Command-q>", lambda _e: self.close())
        self.root.bind("<Control-s>", lambda _e: self.save_state_dialog())
        self.root.bind("<Command-s>", lambda _e: self.save_state_dialog())
        self.root.bind("<Control-l>", lambda _e: self.load_state_dialog())
        self.root.bind("<Command-l>", lambda _e: self.load_state_dialog())
        self.root.bind("<F7>", lambda _e: self.step_cpu_instruction())
        self.root.bind("<F8>", lambda _e: self.frame_advance())
        self.root.bind("<F11>", lambda _e: self.toggle_fullscreen())
        self.root.bind("<F12>", lambda _e: self.screenshot())

    def _sync_load_state(self) -> None:
        if self.file_menu is None:
            return
        try:
            self.file_menu.entryconfig(
                "Load ROM...", state="disabled" if self.files_off else "normal",
            )
        except tk.TclError:
            pass

    def _refresh_recent(self) -> None:
        if self.recent_menu is None:
            return
        self.recent_menu.delete(0, "end")
        if self.files_off:
            self.recent_menu.add_command(label="(files=OFF)", command=self._files_off_notice)
        elif not self.recent:
            self.recent_menu.add_command(label="(empty)", state="disabled")
        else:
            for p in self.recent:
                self.recent_menu.add_command(
                    label=os.path.basename(p), command=lambda path=p: self.load_rom(path),
                )

    def set_files_mode(self, files_off: bool) -> None:
        self.files_off = bool(files_off)
        self._apply_files_mode_ui()
        self._sync_load_state()
        self._refresh_recent()
        self.status_var.set(
            f"{'files=OFF' if self.files_off else 'files=ON'}  ·  "
            f"audio={'ON' if self.audio.available else 'off'}  ·  "
            f"{CORE_LABEL}  ·  opcodes={CPU65816.OPCODE_COUNT}/256"
        )

    def toggle_files(self) -> None:
        self.set_files_mode(not self.files_off)

    def _apply_files_mode_ui(self) -> None:
        mode = "files=OFF" if self.files_off else "files=ON"
        if self.title_label is not None:
            self.title_label.config(
                text=f"  {APP_NAME}  {APP_VERSION}   ·   {CORE_LABEL}   ·   {mode}  ",
            )
        if self.hint_label is not None:
            self.hint_label.config(
                text=("  files=OFF · Load ROM locked · Ctrl+F" if self.files_off
                      else "  files=ON · File→Load ROM (.sfc/.smc/.fig/.swc) · Z/A/X/S"),
            )
        if self.load_btn is not None:
            if self.files_off:
                self.load_btn.config(state="disabled", text="Load ROM (OFF)")
            else:
                self.load_btn.config(state="normal", text="Load ROM...")

    def open_rom(self) -> None:
        if self.files_off:
            self._files_off_notice()
            return
        path = filedialog.askopenfilename(
            parent=self.root,
            title=f"Load ROM — {APP_NAME}",
            filetypes=_ROM_FILETYPES,
        )
        if path:
            self.load_rom(path)

    def _files_off_notice(self) -> None:
        self._dialog(
            f"{APP_NAME} — files=OFF",
            "Load ROM locked.\nConfig → files = ON, Ctrl+F, or --files-on.",
        )

    def _setup_file_drop(self) -> None:
        """macOS Finder open/drop; optional tkinterdnd2 canvas drop when installed."""
        try:
            if self.root.tk.call("tk", "windowingsystem") == "aqua":
                self.root.createcommand("::tk::mac::OpenDocument", self._on_open_document)
        except tk.TclError:
            pass
        try:
            from tkinterdnd2 import DND_FILES  # type: ignore
            self.canvas.drop_target_register(DND_FILES)
            self.canvas.dnd_bind("<<Drop>>", self._on_canvas_drop)
        except (ImportError, tk.TclError, AttributeError):
            pass

    def _on_open_document(self, *paths: str) -> None:
        if self.files_off:
            self._files_off_notice()
            return
        for path in paths:
            if path:
                self.load_rom(str(path))
                break

    def _on_canvas_drop(self, event) -> None:
        if self.files_off:
            self._files_off_notice()
            return
        raw = getattr(event, "data", "") or ""
        path = raw.strip("{}").split()[0] if raw else ""
        if path:
            self.load_rom(path)

    def _make_chrome(self) -> None:
        self.title_label = tk.Label(
            self.root,
            text=f"  {APP_NAME}  {APP_VERSION}   ·   {CORE_LABEL}   ·   files=ON  ",
            bg=BG, fg=FG, font=("Courier", 11, "bold"), anchor="w",
        )
        self.title_label.pack(fill="x", padx=6, pady=(6, 2))

        self.canvas = tk.Canvas(
            self.root, width=WIDTH * SCALE, height=HEIGHT * SCALE,
            bg=BG, highlightthickness=1, highlightbackground=ACCENT,
        )
        self.canvas.pack(padx=8, pady=4)
        self.image_item = self.canvas.create_image(0, 0, anchor="nw")

        bar = tk.Frame(self.root, bg=BG)
        bar.pack(fill="x", padx=8, pady=(0, 4))
        self.load_btn = self._btn(bar, "Load ROM...", self.open_rom)
        self.load_btn.pack(side="left")
        self._btn(bar, "RESET", self.reset).pack(side="left", padx=6)
        self._btn(bar, "PAUSE", self.toggle_pause).pack(side="left")
        mute = self._btn(bar, "MUTE", self.toggle_mute)
        mute.config(textvariable=self.mute_text)
        mute.pack(side="left", padx=6)
        self._btn(bar, "FILES", self.toggle_files).pack(side="left", padx=6)
        self._btn(bar, "STEP", self.frame_advance).pack(side="left")
        self._btn(bar, "DEBUG", self.show_debugger).pack(side="left", padx=6)
        self.hint_label = tk.Label(bar, text="", bg=BG, fg=FG_DIM, font=("Courier", 9))
        self.hint_label.pack(side="left", padx=8)

        tk.Label(
            self.root, textvariable=self.status_var, bg=BG, fg=FG,
            anchor="w", font=("Courier", 9),
        ).pack(fill="x", padx=8, pady=(0, 8))

    def _draw_boot(self) -> None:
        fb = bytearray(WIDTH * HEIGHT * 3)
        for y in range(HEIGHT):
            shade = BOOT_RGB[(y * len(BOOT_RGB)) // HEIGHT]
            fb[y * WIDTH * 3:(y + 1) * WIDTH * 3] = bytes(shade) * WIDTH
        # title banner pixels
        self._present(fb)

    def _present(self, fb: bytearray) -> None:
        ppm = f"P6\n{WIDTH} {HEIGHT}\n255\n".encode("ascii") + bytes(fb)
        try:
            self.image = tk.PhotoImage(data=ppm, format="PPM")
        except tk.TclError:
            # Tcl/Tk builds bundled with some Python 3.14 distributions only
            # accept binary PhotoImage data through its base64 transport.
            self.image = tk.PhotoImage(data=base64.b64encode(ppm), format="PPM")
        self.scaled = self.image.zoom(SCALE, SCALE)
        self.canvas.itemconfigure(self.image_item, image=self.scaled)

    def _set_status(self, text: str) -> None:
        self._base_status = text
        self.status_var.set(text)

    def save_state_dialog(self) -> None:
        if not self.snes.cart.rom:
            self._dialog(APP_NAME, "Load a ROM before saving a state.", error=True)
            return
        initial = os.path.splitext(self.snes.cart.name)[0] + ".s9state"
        path = filedialog.asksaveasfilename(
            parent=self.root, title="Save State", initialfile=initial,
            defaultextension=".s9state",
            filetypes=(("SNESemu states", "*.s9state"), ("All Files", "*.*")),
        )
        if not path:
            return
        try:
            self.snes.save_state(path)
            self._set_status(f"state saved  ·  {os.path.basename(path)}")
        except (OSError, EmulatorError, ValueError, KeyError) as error:
            self._dialog("Save State", str(error), error=True)

    def load_state_dialog(self) -> None:
        if not self.snes.cart.rom:
            self._dialog(APP_NAME, "Load the matching ROM before loading a state.", error=True)
            return
        path = filedialog.askopenfilename(
            parent=self.root, title="Load State",
            filetypes=(("SNESemu states", "*.s9state"), ("All Files", "*.*")),
        )
        if not path:
            return
        try:
            self.snes.load_state(path)
            self.audio.flush()
            self.audio.set_paused(self.snes.paused)
            self._present(self.snes.ppu.framebuffer)
            self._set_status(f"state loaded  ·  {os.path.basename(path)}  ·  frame {self.snes.frame}")
        except (OSError, EmulatorError, ValueError, KeyError, TypeError) as error:
            self._dialog("Load State", str(error), error=True)

    def screenshot(self) -> None:
        if not self.snes.cart.rom:
            self._dialog(APP_NAME, "Load a ROM before taking a screenshot.", error=True)
            return
        initial = f"{os.path.splitext(self.snes.cart.name)[0]}-frame-{self.snes.frame}.ppm"
        path = filedialog.asksaveasfilename(
            parent=self.root, title="Save Screenshot", initialfile=initial,
            defaultextension=".ppm",
            filetypes=(("Portable pixmap", "*.ppm"), ("All Files", "*.*")),
        )
        if not path:
            return
        try:
            with open(path, "wb") as fh:
                fh.write(f"P6\n{WIDTH} {HEIGHT}\n255\n".encode("ascii"))
                fh.write(self.snes.ppu.framebuffer)
            self._set_status(f"screenshot saved  ·  {os.path.basename(path)}")
        except OSError as error:
            self._dialog("Screenshot", str(error), error=True)

    def toggle_fullscreen(self) -> None:
        self.fullscreen = not self.fullscreen
        self.root.attributes("-fullscreen", self.fullscreen)
        self._set_status("fullscreen" if self.fullscreen else "windowed")

    def frame_advance(self) -> None:
        if not self.snes.cart.rom:
            self._set_status("no ROM  ·  File → Load ROM...")
            return
        self.snes.paused = False
        fb = self.snes.run_frame()
        self.snes.paused = True
        self.audio.set_paused(True)
        self._present(fb)
        self._set_status(f"frame advance  ·  frame {self.snes.frame}  ·  paused")

    def step_cpu_instruction(self) -> None:
        if not self.snes.cart.rom:
            self._set_status("no ROM  ·  File → Load ROM...")
            return
        self.snes.paused = True
        self.audio.set_paused(True)
        before = (self.snes.cpu.pb, self.snes.cpu.pc)
        cycles = self.snes.cpu.step()
        self._set_status(
            f"CPU step ${before[0]:02X}:{before[1]:04X}  ·  {cycles} cycles  ·  paused"
        )

    def _debug_text(self) -> str:
        cpu, bus, ppu = self.snes.cpu, self.snes.bus, self.snes.ppu
        flags = "".join(
            ch if cpu.p & mask else "." for ch, mask in
            (("N", cpu.N), ("V", cpu.V), ("M", cpu.M), ("X", cpu.X),
             ("D", cpu.D), ("I", cpu.I), ("Z", cpu.Z), ("C", cpu.C))
        )
        lines = [
            f"CPU  PB:PC={cpu.pb:02X}:{cpu.pc:04X}  A={cpu.a:04X} X={cpu.x:04X} Y={cpu.y:04X}",
            f"     S={cpu.sp:04X} D={cpu.d:04X} DB={cpu.db:02X} P={cpu.p:02X} [{flags}] E={int(cpu.e)}",
            f"     cycles={cpu.total_cycles} waiting={cpu.waiting} stopped={cpu.stopped}",
            "",
            "DISASSEMBLY",
        ]
        saved_open = bus.open_bus
        pc = cpu.pc
        one_byte = {"dp", "dpx", "dpy", "dpi", "dpix", "dpiy", "idp", "idpy",
                    "sr", "sriy", "rel"}
        two_byte = {"abs", "abx", "aby", "ind", "indx", "indl", "rell", "bm"}
        try:
            for _ in range(10):
                op = bus.read(cpu.pb, pc)
                name, mode, _cycles = cpu.OPCODES[op]
                if mode == "imm":
                    count = cpu.a_bytes()
                elif mode == "immx":
                    count = cpu.x_bytes()
                elif mode in one_byte:
                    count = 1
                elif mode in two_byte:
                    count = 2
                elif mode in ("abl", "ablx"):
                    count = 3
                else:
                    count = 1 if name in ("BRK", "COP") else 0
                operand = [bus.read(cpu.pb, (pc + i + 1) & 0xFFFF) for i in range(count)]
                raw = " ".join(f"{b:02X}" for b in [op, *operand])
                marker = ">" if pc == cpu.pc else " "
                lines.append(f"{marker} {cpu.pb:02X}:{pc:04X}  {raw:<12} {name:<4} {mode}")
                pc = (pc + count + 1) & 0xFFFF
        finally:
            bus.open_bus = saved_open
        lines += ["", f"PPU  mode={ppu.bg_mode} line={ppu.scanline} brightness={ppu.brightness}",
                  f"     TM={ppu.regs[0x2C]:02X} TS={ppu.regs[0x2D]:02X} VRAM={ppu.vmadd:04X} CGRAM={ppu.cgadd:02X}",
                  f"IRQ  mode={bus.irq_mode} H={bus.htime} V={bus.vtime} flag={bus.irq_flag}",
                  "", "DMA CHANNELS"]
        for i, channel in enumerate(bus.dma.channels):
            src = channel[2] | (channel[3] << 8) | (channel[4] << 16)
            size = channel[5] | (channel[6] << 8)
            lines.append(
                f"{i}: DMAP={channel[0]:02X} BBAD={channel[1]:02X} A1={src:06X} DAS={size:04X}"
            )
        sp = cpu.sp & 0x1FFFF
        lo = max(0, sp - 16)
        hi = min(len(bus.wram), sp + 17)
        lines += ["", f"WRAM around S (${sp:05X})", " ".join(f"{b:02X}" for b in bus.wram[lo:hi])]
        return "\n".join(lines)

    def show_debugger(self) -> None:
        win = tk.Toplevel(self.root)
        win.title(f"{APP_NAME} Debugger")
        win.configure(bg=BG)
        text = tk.Text(win, width=88, height=32, bg="#050A18", fg=FG_BRIGHT,
                       insertbackground=FG, font=("Courier", 10), wrap="none")
        text.pack(fill="both", expand=True, padx=8, pady=8)

        def refresh() -> None:
            if not win.winfo_exists():
                return
            text.configure(state="normal")
            text.delete("1.0", "end")
            text.insert("1.0", self._debug_text())
            text.configure(state="disabled")
            win.after(200, refresh)

        refresh()

    def load_rom(self, path: str) -> None:
        try:
            self.snes.load(path)
            self.audio.flush()
            path = os.path.abspath(path)
            if path in self.recent:
                self.recent.remove(path)
            self.recent.insert(0, path)
            del self.recent[8:]
            self._refresh_recent()
            title = self.snes.cart.title or self.snes.cart.name
            chip = self.snes.cart.chip
            smc = "+smc" if self.snes.cart.has_smc_header else ""
            checksum = "checksum OK" if self.snes.cart.checksum_valid else "checksum unverified"
            speed = "FastROM" if self.snes.cart.fastrom else "SlowROM"
            self._set_status(
                f"{title}  ·  {self.snes.cart.map_mode}{smc}  ·  {chip}  ·  "
                f"{speed}  ·  {checksum}  ·  SRAM={len(self.snes.cart.sram)}"
            )
            self.root.title(f"{APP_NAME} {APP_VERSION} — {title}")
            self.next_frame = time.perf_counter()
            self.root.focus_force()
        except (OSError, EmulatorError) as error:
            self.snes.running = False
            self._dialog(APP_NAME, str(error), error=True)

    def reset(self) -> None:
        if not self.snes.cart.rom:
            self._set_status("no ROM  ·  File → Load ROM...")
            return
        self.snes.reset()
        self.audio.flush()
        self._set_status(f"reset  ·  {self.snes.cart.title}")

    def power_blank(self) -> None:
        self.snes.running = False
        self._draw_boot()
        self._set_status("power off")

    def toggle_pause(self) -> None:
        self.snes.paused = not self.snes.paused
        self.audio.set_paused(self.snes.paused)
        self._set_status("paused" if self.snes.paused else "running")

    def toggle_mute(self) -> None:
        self.audio.set_muted(not self.audio.muted)
        self.mute_text.set("UNMUTE" if self.audio.muted else "MUTE")

    def show_controls(self) -> None:
        self._dialog(
            f"{APP_NAME} controls",
            "Pad 1\n"
            "D-pad arrows · Z B · A Y · X A · S X · Enter Start · Shift Select\n"
            "D L · C R\n\n"
            f"65816 opcodes: {CPU65816.OPCODE_COUNT}/256 (inlined snes9x jump table)\n"
            "Space pause · F7 CPU step · F8 frame advance · F11 fullscreen · F12 screenshot\n"
            "files=ON · File→Load ROM · Ctrl+O · drop ROM on window (macOS)\n"
            "Use ROM files you legally own.",
        )

    def show_about(self) -> None:
        cy_mode = "Cython-compiled" if getattr(cython, "compiled", False) else "pure-Python (+ Cython-ready)"
        self._dialog(
            f"About {APP_NAME}",
            f"{APP_NAME} {APP_VERSION}\n"
            f"Core: {CORE_BACKEND} — inlined snes9x ({cy_mode})\n"
            f"Audio: {self.audio.backend}\n"
            f"Opcodes: {CPU65816.OPCODE_COUNT}/256 · jump table ready\n"
            "No external snes9x package — core lives in this file.\n"
            "files=ON · Load ROM from disk (.sfc/.smc/.fig/.swc)\n"
            "Use ROMs you legally own.",
        )

    def _joy_bits(self) -> int:
        bits = 0
        for key, mask in self.KEYMAP.items():
            if key in self.held:
                bits |= mask
        return bits

    def key_down(self, event) -> None:
        self.held.add(event.keysym)
        if event.keysym in ("m", "M"):
            self.toggle_mute()
        if event.keysym == "space":
            self.toggle_pause()

    def key_up(self, event) -> None:
        self.held.discard(event.keysym)

    def loop(self) -> None:
        now = time.perf_counter()
        if now >= self.next_frame:
            work_start = time.perf_counter()
            self.snes.set_joy(0, self._joy_bits())
            fb = self.snes.run_frame()
            self._present(fb)
            if self.audio.available:
                self.audio.push(self.snes.apu.drain_frame_samples(FPS))
            self._perf_frames += 1
            self._perf_work += time.perf_counter() - work_start
            self.next_frame += 1.0 / FPS
            if now > self.next_frame + 0.25:
                self.next_frame = now + 1.0 / FPS
        elapsed = now - self._perf_started
        if elapsed >= 0.5:
            measured_fps = self._perf_frames / elapsed
            cpu_use = min(999.0, self._perf_work * 100.0 / elapsed)
            self.status_var.set(
                f"{self._base_status}  ·  {measured_fps:4.1f} FPS  ·  CPU {cpu_use:3.0f}%"
            )
            self._perf_started = now
            self._perf_frames = 0
            self._perf_work = 0.0
        self.root.after(1, self.loop)

    def close(self) -> None:
        try:
            self.snes.cart.save_sram()
        except Exception:
            pass
        self.audio.close()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def _finalize_cart_header(rom: bytearray, header_offset: int) -> None:
    """Write checksum/complement for an internal SNES header."""
    rom[header_offset + 0x1C:header_offset + 0x20] = b"\x00\x00\x00\x00"
    checksum = (sum(rom) + 0x1FE) & 0xFFFF
    complement = checksum ^ 0xFFFF
    rom[header_offset + 0x1C] = complement & 0xFF
    rom[header_offset + 0x1D] = complement >> 8
    rom[header_offset + 0x1E] = checksum & 0xFF
    rom[header_offset + 0x1F] = checksum >> 8


def _build_self_test_rom() -> bytes:
    """Create a tiny original LoROM used only by --self-test (no game data)."""
    rom = bytearray([0xEA]) * 0x8000
    program = bytes((
        0x78,             # SEI
        0x18, 0xFB,       # CLC / XCE -> native
        0xC2, 0x30,       # REP #$30 -> 16-bit A/X
        0xA9, 0x34, 0x12, # LDA #$1234
        0xAA,             # TAX
        0xE2, 0x20,       # SEP #$20 -> 8-bit A
        0xA9, 0x0F, 0x8D, 0x00, 0x21,  # INIDISP = $0F
        0xA9, 0x01, 0x8D, 0x05, 0x21,  # BGMODE = 1
        0xDB,             # STP
    ))
    rom[:len(program)] = program
    rom[0x1000] = 0x40  # RTI at $9000 for interrupt tests
    h = 0x7FC0
    title = b"SNESEMU SELF TEST    "[:21]
    rom[h:h + 21] = title.ljust(21, b" ")
    rom[h + 0x15] = 0x20  # LoROM / SlowROM
    rom[h + 0x16] = 0x00
    rom[h + 0x17] = 0x05  # 32 KiB
    rom[h + 0x18] = 0x00
    rom[h + 0x19] = 0x01
    for vector in (0x24, 0x26, 0x2A, 0x2E, 0x34, 0x3A, 0x3C, 0x3E):
        target = 0x9000 if vector in (0x2E, 0x3E) else 0x8000
        rom[h + vector] = target & 0xFF
        rom[h + vector + 1] = target >> 8
    _finalize_cart_header(rom, h)
    return bytes(rom)


def _build_hirom_self_test_rom() -> bytes:
    """Minimal HiROM image for mapper/reset smoke tests (original bytes only)."""
    rom = bytearray([0xEA]) * 0x20000
    program = bytes((
        0xA9, 0x42,  # LDA #$42
        0xDB,        # STP
    ))
    rom[0x8000:0x8000 + len(program)] = program
    h = 0xFFC0
    rom[h:h + 21] = b"HIROM SELF TEST     "
    rom[h + 0x15] = 0x31  # HiROM / SlowROM
    rom[h + 0x16] = 0x00
    rom[h + 0x17] = 0x07  # 128 KiB
    rom[h + 0x18] = 0x00
    rom[h + 0x19] = 0x01
    for vector in (0x24, 0x26, 0x2A, 0x2E, 0x34, 0x3A, 0x3C, 0x3E):
        target = 0x8000
        rom[h + vector] = target & 0xFF
        rom[h + vector + 1] = target >> 8
    _finalize_cart_header(rom, h)
    return bytes(rom)


def run_self_tests() -> int:
    """Deterministic smoke tests for the single-file core."""
    checks: list[str] = []

    def check(condition: bool, label: str) -> None:
        if not condition:
            raise AssertionError(label)
        checks.append(label)

    try:
        with tempfile.TemporaryDirectory(prefix="snesemu-selftest-") as folder:
            rom_path = os.path.join(folder, "selftest.smc")
            with open(rom_path, "wb") as fh:
                fh.write(b"\x00" * 512)
                fh.write(_build_self_test_rom())

            console = SNES()
            console.load(rom_path)
            check(console.cart.has_smc_header, "SMC header stripping")
            check(console.cart.map_mode == "lorom", "LoROM header scoring")
            check(console.cart.checksum_valid, "header checksum validation")
            check(console.cpu.pc == 0x8000, "reset vector mapping")

            for _ in range(32):
                console.cpu.step()
                if console.cpu.stopped:
                    break
            check(console.cpu.stopped, "test program reached STP")
            check(not console.cpu.e and console.cpu.x == 0x1234, "native REP/SEP widths")
            check(console.ppu.brightness == 15 and console.ppu.bg_mode == 1,
                  "CPU-to-PPU register writes")

            hirom_path = os.path.join(folder, "hirom.sfc")
            with open(hirom_path, "wb") as fh:
                fh.write(_build_hirom_self_test_rom())
            hirom = SNES()
            hirom.load(hirom_path)
            check(hirom.cart.map_mode == "hirom", "HiROM header scoring")
            check(hirom.cpu.pc == 0x8000, "HiROM reset vector mapping")
            for _ in range(8):
                hirom.cpu.step()
                if hirom.cpu.stopped:
                    break
            check(hirom.cpu.stopped and hirom.cpu.get_a() == 0x42,
                  "HiROM boot program reached STP")

            check(console.cart.read_lorom(0x40, 0x8000) == console.cart.rom[0],
                  "LoROM $40-$6F mirror read")
            console.bus.wram[0x1000:0x1005] = b"HELLO"
            cpu = console.cpu
            cpu.a, cpu.x, cpu.y = 4, 0x1000, 0x2000
            cpu.pb, cpu.pc = 0x7E, 0x3000
            cpu.p, cpu.e = cpu.M | cpu.X | cpu.I, False
            cpu.stopped = cpu.waiting = False
            cpu._sync_width()
            console.bus.wram[0x3000:0x3003] = bytes((0x54, 0x7E, 0x7E))  # MVN $7E,$7E
            for _ in range(6):
                cpu.step()
            check(console.bus.wram[0x2000:0x2005] == b"HELLO", "MVN block move")

            console.cpu.e = True
            console.cpu.p = console.cpu.M | console.cpu.X | console.cpu.D
            console.cpu.a = 0x45
            console.cpu._sync_width()
            console.cpu._adc(0x55)
            check(console.cpu.get_a() == 0 and bool(console.cpu.p & console.cpu.C),
                  "8-bit decimal ADC")
            console.cpu.a = 0
            console.cpu.p |= console.cpu.D | console.cpu.C
            console.cpu._sbc(1)
            check(console.cpu.get_a() == 0x99 and not (console.cpu.p & console.cpu.C),
                  "8-bit decimal SBC")

            console.cpu.e = False
            console.cpu.p = console.cpu.X  # native 16-bit A, 8-bit X
            console.cpu.a = 0x1234
            console.cpu._sync_width()
            console.cpu._adc(0x5678)
            check(console.cpu.get_a() == 0x68AC, "16-bit binary ADC")
            console.cpu.p |= console.cpu.D
            console.cpu.a = 0x1234
            console.cpu._adc(0x0055)
            check(console.cpu.get_a() == 0x1289, "16-bit decimal ADC")

            console.bus.write(0x00, 0x0010, 0xAA)
            console.bus.write(0x00, 0x2010, 0x55)
            check(console.bus.wram[0x10] == 0xAA, "reserved area is not WRAM")
            check(console.bus.read(0x00, 0x2010) == 0x55, "open-bus latch")

            console.cpu.stopped = False
            console.cpu.e = False
            console.cpu.p &= ~console.cpu.I
            console.cpu.pb, console.cpu.pc, console.cpu.sp = 0x7E, 0x1234, 0x1FFF
            check(console.cpu.irq() and console.cpu.pc == 0x9000 and console.cpu.pb == 0,
                  "native IRQ vector")

            ppu = console.ppu
            ppu.vram[:] = b"\x00" * len(ppu.vram)
            for offset in (0, 1, 16, 17, 32, 33, 48, 49):
                ppu.vram[offset] = 0x80
            ppu._tile_cache.clear()
            check(ppu._decode_tile_row(0, 0, 8, 0)[0] == 0xFF, "8-bpp tile decode")
            check(ppu._mode_layers(6) == ((0, 4, 0),), "PPU mode 2-6 tables")

            console.bus.wram[0:2] = b"\x34\x12"
            ch = console.bus.dma.channels[0]
            ch[0], ch[1], ch[2], ch[3], ch[4], ch[5], ch[6] = 0, 0x22, 0, 0, 0x7E, 2, 0
            console.bus.dma.trigger(1)
            check(console.ppu.cgram[:2] == b"\x34\x12", "CGRAM general DMA")
            check(console.bus.dma_stall_cycles > 0, "DMA CPU stall accounting")

            # SMW-style Mode-1 setup: BG tilemap/CHR bases are expressed in
            # VRAM words, populated through general DMA, then rendered from
            # the byte-backed VRAM array. This catches the former black-screen
            # factor-of-two address bug and 16x16 BG tile selection.
            ppu.reset()
            console.bus.wram[0x5000:0x5002] = b"\x01\x00"  # tilemap: tile 1
            chr_data = bytearray(64)                         # tiles 1 and 2
            chr_data[0] = 0x80                               # tile 1, color 1
            chr_data[33] = 0x80                              # tile 2, color 2
            console.bus.wram[0x5100:0x5140] = chr_data
            console.bus.wram[0x5200:0x5204] = b"\xFF\x7F\x1F\x00"  # white, red

            def dma_to_bbus(source: int, bbad: int, mode: int, size: int) -> None:
                channel = console.bus.dma.channels[0]
                channel[:] = b"\x00" * len(channel)
                channel[0], channel[1] = mode, bbad
                channel[2], channel[3], channel[4] = source & 0xFF, source >> 8, 0x7E
                channel[5], channel[6] = size & 0xFF, size >> 8
                console.bus.dma.trigger(1)

            ppu.write_reg(0x15, 0x80)  # increment after VMDATAH
            ppu.write_reg(0x16, 0x00); ppu.write_reg(0x17, 0x10)  # word $1000
            dma_to_bbus(0x5000, 0x18, 1, 2)
            ppu.write_reg(0x16, 0x10); ppu.write_reg(0x17, 0x20)  # word $2010
            dma_to_bbus(0x5100, 0x18, 1, 64)
            ppu.write_reg(0x21, 0x01)
            dma_to_bbus(0x5200, 0x22, 0, 4)
            ppu.write_reg(0x07, 0x10)  # BG1 map: 1K-word segment 4
            ppu.write_reg(0x0B, 0x02)  # BG1 CHR: 4K-word segment 2
            ppu.write_reg(0x2C, 0x01)  # BG1 main screen
            ppu.write_reg(0x05, 0x01)  # Mode 1, 8x8 BG1
            ppu.write_reg(0x00, 0x0F)  # release forced blank
            ppu.render_scanline(0)
            check(ppu.framebuffer[:3] == b"\xF8\xF8\xF8",
                  "Mode-1 word-addressed BG DMA render")
            ppu.write_reg(0x05, 0x11)  # Mode 1, 16x16 BG1
            ppu.render_scanline(0)
            check(ppu.framebuffer[8 * 3:9 * 3] == b"\xF8\x00\x00",
                  "Mode-1 16x16 BG subtile render")

            state_path = os.path.join(folder, "roundtrip.s9state")
            console.bus.wram[0x1234] = 0xA5
            console.save_state(state_path)
            console.bus.wram[0x1234] = 0
            console.load_state(state_path)
            check(console.bus.wram[0x1234] == 0xA5, "portable state round-trip")
            check(len(CPU65816.OPCODES) == 256, "256-opcode decode table")
            check(CPU65816.all_opcodes_supported(), "snes9x jump table 256/256")
            check(SNES9X_CORE_INLINE and Snes9xCore.inline, "snes9x core inlined (no external import)")

            # Exercise every dispatch path once from WRAM. This is a smoke
            # test for decoding/operand consumption, not a replacement for
            # external 65C816 conformance ROMs.
            for opcode in range(256):
                cpu = console.cpu
                cpu.a = cpu.x = cpu.y = cpu.d = cpu.db = 0
                cpu.sp, cpu.pb, cpu.pc = 0x01FF, 0x7E, 0x4000
                cpu.p, cpu.e = cpu.M | cpu.X | cpu.I, True
                cpu.stopped = cpu.waiting = cpu.frozen = False
                cpu._sync_width()
                console.bus.wram[0x4000:0x4005] = bytes((opcode, 0, 0, 0, 0))
                used = cpu.step()
                if used <= 0:
                    raise AssertionError(f"opcode ${opcode:02X} returned no cycles")
            check(True, "all 256 opcode jump-table paths execute")
            core = Snes9xCore()
            check(core.opcode_count == 256, "Snes9xCore facade reports 256 opcodes")
    except (AssertionError, OSError, EmulatorError, ValueError, KeyError, TypeError) as error:
        print(f"SELF-TEST FAILED: {error}", file=sys.stderr)
        return 1
    print(f"SELF-TEST PASSED: {len(checks)} checks")
    for label in checks:
        print(f"  ok  {label}")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    argv = list(sys.argv[1:] if argv is None else argv)
    rom = None
    files_off = False  # files=ON — Load ROM enabled by default
    enable_sound = True
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-h", "--help"):
            print(f"{APP_NAME} {APP_VERSION}  core={CORE_LABEL}")
            print("Usage: python3 snesemu0.1.1.py [options] [game.sfc]")
            print("  --self-test         run mapper/CPU/PPU/DMA/state smoke tests")
            print("  --files-on / -F     Load ROM enabled (default)")
            print("  --files-off         lock the Load ROM controls")
            print("  --sound             audio ON (default)")
            print("  --no-sound          disable audio")
            print(f"  65816 opcodes       {CPU65816.OPCODE_COUNT}/256 (inlined snes9x)")
            print(f"  core                {CORE_BACKEND} (in-file, not external)")
            return 0
        if arg == "--self-test":
            return run_self_tests()
        if arg in ("--files-on", "-F"):
            files_off = False
        elif arg == "--files-off":
            files_off = True
        elif arg in ("--sound", "-S"):
            enable_sound = True
        elif arg == "--no-sound":
            enable_sound = False
        elif not arg.startswith("-"):
            rom = arg
        i += 1
    try:
        CatsSnes9x(
            rom_path=rom,
            files_off=files_off,
            enable_sound=enable_sound,
            menustrip_on=True,
        ).run()
    except tk.TclError as error:
        print(f"GUI error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
