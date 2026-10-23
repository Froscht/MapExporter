"""Audio: catalogue the cooked sound assets and re-embed their sound blocks into a UEBA container.

ARC Raiders stores every cooked sound as RAD **BinkA** blocks — there is no WAV, OGG or opus
anywhere in the game data. Almost all of it is *streamed*: the sound bytes live in the .ubulk,
which starts with a 15-byte streaming seek chunk

    'SEEK' | mode u8 | samples per entry u16 | start time u32 (samples) | entry count u32

followed by the entry table (u16 byte-deltas between consecutive block starts, first entry 0)
and the blocks, each framed by  u16 0x9999 marker + u16 payload size; a trimmed final block uses
size 0xFFFF followed by u32 = payload size (low half) | trimmed sample count (high half).

Long sounds hold several such segments one after another in the same .ubulk (streaming chunk
groups); their start times continue exactly at samples_per_entry * entries of the previous
segment, so total samples = sum over segments of (entries - 1) * samples_per_entry + trim, which
matches the SoundWave Duration exactly.

The container header (BinkAudioFileHeader: version, channels, rate, sample_count, max block size,
flags, file size, seek entry count, blocks per entry) was stripped when the sound was moved into
the .ubulk — neither spelling of its magic appears in the shipped game files (checked the first
bytes of 2500 audio .ubulk: all start with 'SEEK'). Re-embedding therefore rebuilds it from the
block walk plus the SoundWave properties.

The magic is a **u32 written little-endian**, i.e. on disk the four bytes are b"ABEU"; read as a
big-endian fourcc it spells "UEBA". That is what Unreal and FFmpeg's UEBA branch compare against
(`AV_RL32(buf) == MKBETAG('U','E','B','A')`). Writing the ASCII bytes "UEBA" instead — the obvious
guess — produces a file that no decoder accepts, so this is not cosmetic.

A small family (~320 of the streamed sounds — short weapon/launcher mechanics and similar) stores
raw 16-bit PCM instead of BinkA blocks: no 'SEEK' chunk at all, and the .ubulk is exactly
duration * rate * channels * 2 bytes long, interleaved stereo. Those export as playable WAV.

BinkA is a proprietary codec of RAD Game Tools. MapExporter reads and re-embeds its container
(playable with Unreal Engine tooling) and leaves the block bitstreams exactly as the game stored
them; it ships no BinkA decoder. For listening, bink_wav() decodes the blocks with an external
FFmpeg (its own clean-room Bink Audio decoders). Which build can do that is a matter of the build,
not of a version number — see FFMPEG_ENV/ffmpeg_status() below — so every candidate build is
probed with a real sound and the decoded samples are checked before it is trusted.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import struct
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from . import game
from . import models
from .config import report_progress

OUT_DIR = os.path.join(models.HERE, "out", "audio")
CACHE_DIR = os.path.join(models.HERE, "cache")
AUDIO_DIR = "/Pioneer/Audio/"                   # container path fragment of all cooked sound
SEEK_MAGIC = b"SEEK"
# u32 0x55454241 little-endian -> the bytes on disk are b"ABEU"; "UEBA" is that same u32 read
# big-endian. UE and FFmpeg both compare the little-endian u32 against MKBETAG('U','E','B','A').
UEBA_MAGIC = struct.pack("<I", 0x55454241)
BLOCK_MAGIC = 0x9999
TRIM_MARK = 0xFFFF
SEEK_HEADER_SIZE = 15
# (name, size) of the container header, in file order; see the offsets in test_audio_ueba_header
UEBA_FIELDS = (("magic", 4), ("version", 1), ("channels", 1), ("pad", 2), ("rate", 4),
               ("samples", 4), ("max_block", 2), ("flags", 2), ("file_size", 4),
               ("entries", 2), ("blocks_per_entry", 2))
UEBA_HEADER_SIZE = sum(size for _name, size in UEBA_FIELDS)
# the game's decoder never validates the version (FFmpeg's UEBA branch probes for 1)
UEBA_VERSION = 1
BA2_FLAGS = 1                                   # "is BA2" marker: always set by UE


@dataclass
class Item:
    name: str
    gpath: str                                  # /Game/... of the sound package
    folder: str = ""                            # grouping label below the audio folder
    duration: float | None = None               # seconds (SoundWave properties)
    rate: int | None = None
    channels: int | None = None
    size: int = 0                               # .ubulk bytes
    samples: int | None = None                  # duration * rate
    ubulk: bool = False
    pcm: bool = False                           # .ubulk holds raw 16-bit PCM (no 'SEEK')


# ---------- catalogue ------------------------------------------------------------------------
def _sound_wave(gpath: str):
    """(Duration, SampleRate, NumChannels) of a package's SoundWave export, or None."""
    try:
        raw, pkg = models.package(gpath)
    except KeyError:
        return None
    for e in pkg.exports:
        if pkg.export_class(e, game.smap) != "SoundWave":
            continue
        a = pkg.header_size + e.offset
        try:
            _q, props = game.read_unversioned(raw, a, a + e.size, "SoundWave")
        except Exception:
            continue
        d = dict(props)
        try:
            dur = float(d["Duration"]) if d.get("Duration") is not None else None
        except (TypeError, ValueError):
            dur = None
        return dur, d.get("SampleRate"), d.get("NumChannels")
    return None


def _item(ukey: str | None, bkey: str | None) -> Item:
    """One catalogue row from the .uasset/.ubulk container keys (reads its SoundWave properties)."""
    gpath = models.to_gpath(ukey or bkey)
    meta = _sound_wave(gpath) if ukey else None
    dur, rate, ch = meta if meta else (None, None, None)
    size = _sizes(bkey) if bkey else 0
    item = Item(name=ukey.rsplit("/", 1)[-1][:-len(".uasset")] if ukey
                else bkey.rsplit("/", 1)[-1][:-len(".ubulk")],
                gpath=gpath, folder=_folder(ukey or bkey), duration=dur, rate=rate, channels=ch,
                size=size, ubulk=bool(bkey))
    if dur and rate:
        item.samples = int(round(dur * rate))
    item.pcm = bool(bkey and _pcm_shape(gpath, size=size, meta=meta))
    return item


def _folder(key: str) -> str:
    """Folder below the audio directory, used as the catalogue label and the export subfolder."""
    tail = key.split(AUDIO_DIR, 1)[1] if AUDIO_DIR in key else key
    head, _, _ = tail.rpartition("/")
    return head


def _sizes(key: str) -> int:
    """Chunk length of a container entry, without decompressing it."""
    hits = models.lookup(key)
    return 0 if not hits else int(hits[0][0].chunk_offsets[hits[0][1]][1])


def _pcm_shape(gpath: str, size: int = 0, meta=None):
    """(frames, rate, channels) when the .ubulk is raw 16-bit PCM, else None.

    Raw PCM has no 'SEEK' chunk and its length is exactly frames * channels * 2 bytes, where
    frames come from the SoundWave Duration — both have to hold, so a BinkA sound is never
    mistaken for PCM. Only the size test is cheap; the 4-byte probe runs on its matches alone.
    """
    meta = meta if meta is not None else _sound_wave(gpath)
    dur, rate, ch = (meta or (None, None, None))
    if not (dur and rate and ch):
        return None
    frames = int(round(dur * rate))
    if frames <= 0 or frames * int(ch) * 2 <= 0:
        return None
    key = models.to_key(gpath, ".ubulk")
    hits = models.lookup(key)
    if not hits:
        return None
    c, ud = hits[0]
    size = size or int(c.chunk_offsets[ud][1])
    if size != frames * int(ch) * 2:
        return None
    if c.get_chunk(ud, 4)[:4] == SEEK_MAGIC:
        return None
    return frames, int(rate), int(ch)


def catalogue(limit: int = 0) -> list[Item]:
    """Every cooked sound: name, SoundWave properties and .ubulk size (no block reading).

    All ARC sound is streamed, so an asset is a stem that has a .uasset and/or a .ubulk under
    the audio folder; properties come from the SoundWave export (fast, ~1 min for the whole game).
    """
    idx = models.index()
    ukeys: dict[str, str] = {}
    bkeys: dict[str, str] = {}
    for k in idx:
        if AUDIO_DIR not in k:
            continue
        if k.endswith(".uasset"):
            ukeys.setdefault(k.rsplit("/", 1)[-1][:-len(".uasset")].lower(), k)
        elif k.endswith(".ubulk"):
            bkeys.setdefault(k.rsplit("/", 1)[-1][:-len(".ubulk")].lower(), k)
    todo = sorted(bkeys, key=lambda s: (s, ukeys.get(s, "")))
    todo += [s for s in sorted(ukeys) if s not in bkeys]
    if limit:
        todo = todo[:limit]
    out: list[Item] = []
    for n, stem in enumerate(todo):
        out.append(_item(ukeys.get(stem), bkeys.get(stem)))
        if n % 1000 == 999:
            report_progress(n + 1, len(todo))
    report_progress(len(todo), len(todo))
    return out


def find(name: str, cat: list[Item] | None = None, max_hits: int = 40) -> list[Item]:
    """Catalogue items matching a name: exact first, then substring, case-insensitive.

    Without a pre-built catalogue the name is matched against the container index and properties
    are read for the hits only — `audio info X`/`audio play X` then cost a second, not the minute
    the full catalogue takes."""
    if cat is not None:
        n = name.lower()
        return [it for it in cat if it.name.lower() == n] or [it for it in cat if n in it.name.lower()]
    n = name.lower()
    pairs: dict[str, list] = {}
    for k in models.index():
        if AUDIO_DIR not in k:
            continue
        tail = k.rsplit("/", 1)[-1]
        stem, _, ext = tail.rpartition(".")
        if ext not in ("uasset", "ubulk"):
            continue
        slot = pairs.setdefault(stem.lower(), [None, None])
        slot[0 if ext == "uasset" else 1] = k
    exact = {s: v for s, v in pairs.items() if s == n}
    pool = exact or {s: v for s, v in pairs.items() if n in s}
    keys = sorted(pool)[:max_hits]
    return [_item(v[0], v[1]) for v in (pool[k] for k in keys) if any(v)]


# ---------- seek table / blocks ---------------------------------------------------------------
def _walk(data: bytes, base: int, count: int, limit: int = 0):
    """Blocks of one segment: ([(payload size, trim)], bytes, last trim). limit stops the walk early."""
    sizes, p, trim = [], 0, None
    for _ in range(count):
        if base + p + 4 > len(data) or (limit and p >= limit):
            break
        mark, size = struct.unpack_from("<HH", data, base + p)
        if mark != BLOCK_MAGIC:
            break
        if size == TRIM_MARK:
            if base + p + 8 > len(data):
                break
            size, trim = struct.unpack_from("<HH", data, base + p + 4)
            sizes.append((size, trim))
            p += 8 + size
            break
        sizes.append((size, None))
        p += 4 + size
    return sizes, p, trim


def ubulk_bytes(gpath: str) -> bytes:
    """The whole sound stream of a sound package (.ubulk chunk), decrypted/decompressed."""
    key = models.to_key(gpath, ".ubulk")
    hits = models.lookup(key)
    if not hits:
        raise KeyError(key)
    return hits[0][0].get_chunk(hits[0][1])


def _segments(data: bytes) -> list[dict]:
    """Every streaming segment of a .ubulk: its seek table and the blocks it announces (pure)."""
    segs, p = [], 0
    while p + SEEK_HEADER_SIZE <= len(data):
        if data[p:p + 4] != SEEK_MAGIC:
            segs.append({"offset": p, "seek": False, "bytes": len(data) - p})
            break
        afpe, = struct.unpack_from("<H", data, p + 5)
        start, = struct.unpack_from("<I", data, p + 7)
        count, = struct.unpack_from("<I", data, p + 11)
        base = p + SEEK_HEADER_SIZE + 2 * count
        sizes, used, trim = _walk(data, base, count)
        segs.append({"offset": p, "seek": True, "samples_per_entry": afpe, "start": start,
                     "entries": count, "read": len(sizes), "blocks": sizes, "trim": trim,
                     "max_block": max((s for s, _ in sizes), default=0),
                     "bytes": SEEK_HEADER_SIZE + 2 * count + used})
        p = base + used
    return segs


def stream_stats(data: bytes) -> dict:
    """Aggregate of _segments(data): segments, blocks, max block size, total samples (pure)."""
    segs = _segments(data)
    blocks = [b for s in segs for b in s.get("blocks", [])]
    samples = None
    if all(s.get("seek") for s in segs) and all(s.get("read") == s.get("entries") for s in segs):
        samples = sum((s["read"] - 1) * s["samples_per_entry"] + (s["trim"] if s["trim"] is not None
                                                             else s["samples_per_entry"]) for s in segs)
    return {"bytes": len(data), "seek": bool(segs) and segs[0]["seek"], "segments": segs,
            "blocks": len(blocks), "max_block": max((s for s, _ in blocks), default=0),
            "trim": segs[-1].get("trim") if segs else None, "samples": samples}


def stream(gpath: str) -> dict:
    """Shape of a streamed sound: its segments with the seek tables and the block sizes."""
    return stream_stats(ubulk_bytes(gpath))


def _payloads(data: bytes) -> list[tuple[bytes, int | None]]:
    """Every block payload of the .ubulk, in file order."""
    out, p = [], 0
    while p + SEEK_HEADER_SIZE <= len(data):
        if data[p:p + 4] != SEEK_MAGIC:
            break
        count, = struct.unpack_from("<I", data, p + 11)
        base = p + SEEK_HEADER_SIZE + 2 * count
        q = 0
        for _ in range(count):
            if base + q + 4 > len(data):
                return out
            mark, size = struct.unpack_from("<HH", data, base + q)
            if mark != BLOCK_MAGIC:
                return out
            if size == TRIM_MARK:
                size, trim = struct.unpack_from("<HH", data, base + q + 4)
                out.append((data[base + q + 8:base + q + 8 + size], trim))
                return out
            out.append((data[base + q + 4:base + q + 4 + size], None))
            q += 4 + size
        p = base + q
    return out


def block_frames(data: bytes) -> bytes:
    """Re-frame every block payload into the plain `u16 marker + u16 size + payload` layout that
    both the legacy '1FCB' and the UEBA container use (trim block: 0xFFFF + u16 size + u16 samples).
    Intermediate 'SEEK' headers and their tables drop out — one flat block stream remains."""
    out = bytearray()
    for payload, trim in _payloads(data):
        if trim is None:
            out += struct.pack("<HH", BLOCK_MAGIC, len(payload))
        else:
            out += struct.pack("<HHHH", BLOCK_MAGIC, TRIM_MARK, len(payload), trim)
        out += payload
    return bytes(out)


def walk_frames(blocks: bytes) -> list[tuple[int, int, int | None]]:
    """(byte offset, payload size, trim) of every block of a framed block stream. Pure, and the
    walk a demuxer does: u16 0x9999 + u16 size; the trimmed tail carries u16 0xFFFF + u16 size + u16."""
    out, p = [], 0
    while p + 4 <= len(blocks):
        mark, size = struct.unpack_from("<HH", blocks, p)
        if mark != BLOCK_MAGIC:
            break
        start = p
        p += 4
        trim = None
        if size == TRIM_MARK:
            size, trim = struct.unpack_from("<HH", blocks, p)
            p += 4
        out.append((start, size, trim))
        p += size
    return out


def build_ueba(blocks: bytes, rate: int, channels: int, samples: int) -> bytes:
    """UEBA container: header + seek table (delta between block starts) + `blocks` as framed.

    Field order and offsets are the ones Unreal writes and FFmpeg's binka demuxer reads:
    magic 0, version 4, channels 5, rate 8, samples 12, max block 16, flags 18, file size 20,
    seek entries 24, blocks per entry 26, table from 28 (2 bytes per entry, first delta 0).
    """
    frames = walk_frames(blocks)
    if not frames:
        raise ValueError("keine Blöcke")
    table = b"".join(struct.pack("<H", start - (frames[i - 1][0] if i else 0))
                     for i, (start, _size, _trim) in enumerate(frames))
    total = UEBA_HEADER_SIZE + len(table) + len(blocks)
    head = struct.pack("<4sBBHIIHHIHH", UEBA_MAGIC, UEBA_VERSION, channels, 0, rate, samples,
                       max(size for _start, size, _trim in frames), BA2_FLAGS, total, len(frames), 1)
    if len(head) != UEBA_HEADER_SIZE:
        raise AssertionError(f"UEBA-Kopf {len(head)} Bytes, erwartet {UEBA_HEADER_SIZE}")
    return head + table + blocks


def ueba_from_ubulk(data: bytes, rate: int, channels: int,
                    samples_hint: int | None = None) -> tuple[bytes, dict]:
    """Re-embed one .ubulk stream into a UEBA container (pure byte work, no game access)."""
    stats = stream_stats(data)
    if not stats["seek"]:
        raise ValueError("kein SEEK-Chunk — nur Roh-Export möglich")
    if not stats["blocks"]:
        raise ValueError("keine Blöcke")
    samples = stats["samples"] if stats["samples"] is not None else int(round((samples_hint or 0) * rate))
    blocks = block_frames(data)
    container = build_ueba(blocks, rate, channels, samples)
    return container, dict(stats, rate=rate, channels=channels, samples=samples,
                           file_size=len(container))


def binka_bytes(gpath: str) -> tuple[bytes, dict]:
    """UEBA container of one sound: rebuilt header + one seek table + the blocks as stored.

    version/flags are the documented UE defaults (they are nowhere in the game data), rate and
    channels come from the SoundWave properties, sample_count from the block walk."""
    data = ubulk_bytes(gpath)
    meta = _sound_wave(gpath) or ()
    rate = int((meta[1] if len(meta) > 1 else None) or 48000)
    ch = int((meta[2] if len(meta) > 2 else None) or 1)
    return ueba_from_ubulk(data, rate, ch, meta[0])


def wav_bytes(gpath: str) -> tuple[bytes, dict]:
    """16-bit WAV of a raw-PCM sound (PCM family only — BinkA blocks are not decoded)."""
    shape = _pcm_shape(gpath)
    if not shape:
        raise ValueError("kein Roh-PCM — BinkA-Blöcke werden nicht dekodiert (--raw für die .ubulk)")
    frames, rate, ch = shape
    data = ubulk_bytes(gpath)
    if len(data) != frames * ch * 2:
        raise ValueError(f"{len(data)} Bytes passen nicht zu {frames} PCM-Rahmen à {ch * 2} Bytes")
    return pcm_wav(data, rate, ch), dict(rate=rate, channels=ch, samples=frames,
                                         file_size=len(data) + 44)


def pcm_wav(data: bytes, rate: int, channels: int) -> bytes:
    """16-bit WAV container around interleaved PCM (pure byte work)."""
    head = struct.pack("<4sI4s", b"RIFF", 36 + len(data), b"WAVE")
    head += struct.pack("<4sIHHIIHH", b"fmt ", 16, 1, channels, rate, rate * channels * 2,
                        channels * 2, 16)
    head += struct.pack("<4sI", b"data", len(data))
    return head + data


# ---------- playback (external FFmpeg decodes BinkA) -------------------------------------------
# MapExporter ships no BinkA decoder — the codec is RAD Game Tools'. An external FFmpeg decodes the
# rebuilt UEBA container, and whether a build can do that is a question of the BUILD, not of its
# version string:
#   * the 'binka' demuxer learned the UEBA variant on 2026-09-25 (FFmpeg master 0d64b5ad6c, plus
#     the follow-ups c2394a0ff3 and 6213ad9ec7). No release branch has it: 8.0.x, 8.1.x and 9.0.x
#     through 9.0.2 (2026-09-17) only know the legacy '1FCB' layout;
#   * those releases read the sample rate as u16 at offset 6, which in a UEBA header is UE's zero
#     padding, so they fail with "Invalid sample rate: 0";
#   * even with a parsed header only the UEBA branch sets the one-byte '2' extradata that switches
#     libavcodec's binkaudio decoder to Bink Audio 2 (7-bit quant indices, all magnitudes of a run
#     before all signs). Without it the blocks decode to full-scale garbage of the right length.
# So nothing is guessed from a version string: each candidate build is probed with a real sound and
# the decoded samples are checked, and the result is remembered per build (PROBE_CACHE).
FFMPEG_ENV = "ARC_FFMPEG"                   # may name an ffmpeg binary or a folder holding one
FFMPEG_SETTING = "ffmpeg"                   # settings.json key with the same meaning
PROBE_CACHE = os.path.join(CACHE_DIR, "ffmpeg_probe.json")
# places a user may drop a nightly build without touching PATH
EXTRA_FFMPEG_DIRS = ("~/.local/bin", "~/bin", "~/Apps", "~/bin/ffmpeg-master-latest-linux64-gpl/bin",
                     "/opt/ffmpeg/bin", "/usr/local/bin", os.path.join(models.HERE, "tools", "vendor"))

BINKA_HINT = ("dieser FFmpeg-Build dekodiert kein BinkA: der 'binka'-Demuxer kennt nur das alte "
              "1FCB-Layout und setzt die BA2-Kennung nicht, die ARCs Blöcke braucht. Nötig ist ein "
              "Build von FFmpeg master seit 2026-09-25 (kein Release bis 9.0.2). Pfad angeben: "
              f"{FFMPEG_ENV}=/pfad/zu/ffmpeg oder in den Einstellungen; der .binka-Export (UEBA) "
              "bleibt in Unreal abspielbar")


def _exe(base: str) -> str:
    return base + ".exe" if os.name == "nt" else base


def ffmpeg_candidates(prefer: str = "") -> list[str]:
    """FFmpeg builds to try, best first: a path the caller just typed, $ARC_FFMPEG,
    settings["ffmpeg"], PATH, common drop-in dirs.

    An entry may be a file or a folder (then ./ffmpeg, ./ffmpeg*, ./*/ffmpeg and ./*/bin/ffmpeg are
    searched). Duplicated paths are dropped; nothing here decides whether a build can decode UEBA —
    see ffmpeg_status()."""
    from .config import Config
    out, seen = [], set()

    def push(file):
        try:
            real = os.path.realpath(os.path.expanduser(os.path.expandvars(str(file))))
        except (TypeError, ValueError):
            return
        if real not in seen and os.path.isfile(real) and os.access(real, os.X_OK):
            seen.add(real)
            out.append(real)

    def push_dir(folder):
        d = Path(os.path.expanduser(os.path.expandvars(str(folder))))
        if not d.is_dir():
            return
        for pat in (_exe("ffmpeg"), _exe("ffmpeg") + "*",
                    f"*/{_exe('ffmpeg')}", f"*/bin/{_exe('ffmpeg')}"):
            for f in sorted(d.glob(pat)):
                if f.is_file():
                    push(f)

    for raw in (prefer, os.environ.get(FFMPEG_ENV), getattr(Config.load(), FFMPEG_SETTING, "")):
        if not raw:
            continue
        p = Path(os.path.expanduser(os.path.expandvars(str(raw))))
        if p.is_dir():
            push_dir(p)
        else:
            push(raw)
    if (w := shutil.which(_exe("ffmpeg"))):
        push(w)
    for d in EXTRA_FFMPEG_DIRS:
        push_dir(d)
    return out


def ffmpeg_version(exe: str = "") -> str:
    """FFmpeg's first version line (or the path when it does not answer), '' for "no FFmpeg"."""
    exe = exe or ffmpeg_status()["exe"]
    if not exe:
        return ""
    import subprocess
    try:
        r = subprocess.run([exe, "-hide_banner", "-version"], capture_output=True, timeout=20)
        return r.stdout.decode("utf-8", "replace").splitlines()[0].strip()
    except Exception:
        return exe


def _stamp(exe: str) -> str:
    try:
        st = os.stat(exe)
        return f"{int(st.st_mtime)}-{st.st_size}"
    except OSError:
        return "?"


def _probe_cache_load() -> dict:
    try:
        with open(PROBE_CACHE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _probe_cache_save(cache: dict) -> None:
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = PROBE_CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=1)
        os.replace(tmp, PROBE_CACHE)
    except OSError:
        pass


def _wav_quality(data: bytes, expect_samples: int | None) -> str | None:
    """None if the decoded WAV looks like audio, else a reason. Catches the classic BinkA failure
    mode: the demuxer lacks the BA2 extradata, the decoder reads the wrong bit widths and emits
    full-scale garbage that still has the right length."""
    import io
    import wave
    try:
        with wave.open(io.BytesIO(data)) as w:
            n, rate, _ch, width = w.getnframes(), w.getframerate(), w.getnchannels(), w.getsampwidth()
            pcm = w.readframes(n)
    except Exception as e:
        return f"kein lesbares WAV ({type(e).__name__})"
    if width != 2:
        return f"{width * 8}-Bit statt 16-bit PCM"
    import numpy as np
    a = np.frombuffer(pcm, dtype="<i2")
    if a.size == 0:
        return "leeres WAV"
    if expect_samples and n and abs(n - expect_samples) > max(expect_samples // 2, (rate or 48000) // 10):
        return f"Länge {n / max(rate, 1):.2f}s statt {expect_samples / max(rate, 1):.2f}s"
    peak = int(np.abs(a).max())
    if peak == 0:
        return "nur Stille"
    clipped = int(np.count_nonzero(np.abs(a) >= 32000))
    if clipped > a.size // 2:
        return f"{100 * clipped // a.size} % der Samples am Vollausschlag (Dekoder-Müll)"
    return None


def _decode(blob: bytes, exe: str) -> bytes:
    """Run FFmpeg on a container in a temp dir and return the WAV bytes (raises on failure)."""
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        src, dst = os.path.join(td, "s.binka"), os.path.join(td, "s.wav")
        with open(src, "wb") as f:
            f.write(blob)
        # -f binka: never rely on the probe, whose UEBA branch only accepts 1/2 channels
        try:
            r = subprocess.run([exe, "-hide_banner", "-loglevel", "error", "-f", "binka", "-i", src,
                                "-vn", "-acodec", "pcm_s16le", "-y", dst],
                               capture_output=True, timeout=300)
        except subprocess.TimeoutExpired:
            raise RuntimeError("FFmpeg time out beim Dekodieren")
        if r.returncode != 0 or not os.path.exists(dst) or os.path.getsize(dst) < 44:
            err = [l for l in r.stderr.decode("utf-8", "replace").splitlines() if l.strip()]
            raise RuntimeError(err[-1] if err else "FFmpeg erzeugte kein WAV")
        with open(dst, "rb") as f:
            return f.read()


def _probe_sound() -> str | None:
    """Cheapest BinkA sound to probe with — deliberately not catalogue(): that reads every
    SoundWave property (~1 min), the probe needs exactly one. Picks the smallest .ubulk of the
    audio folder whose stream starts with 'SEEK' and whose properties FFmpeg's parser accepts."""
    cands = [(size, key) for key in (k for k in models.index()
                                     if AUDIO_DIR in k and k.endswith(".ubulk"))
             if (size := _sizes(key)) > 200]
    cands.sort()
    for _size, key in cands[:200]:
        hits = models.lookup(key)
        if not hits:
            continue
        c, ud = hits[0]
        try:
            if c.get_chunk(ud, 4)[:4] != SEEK_MAGIC:      # raw PCM family: not a BinkA probe
                continue
        except Exception:
            continue
        gpath = models.to_gpath(key)
        _dur, rate, ch = _sound_wave(gpath) or (None, None, None)
        if rate and ch in (1, 2):                         # FFmpeg's UEBA probe wants 1 or 2 channels
            return gpath
    return None


def _probe(exe: str) -> bool:
    """Decode one real BinkA sound with this build and check the samples."""
    try:
        gpath = _probe_sound()
        if not gpath:
            return False
        ueba, info = binka_bytes(gpath)
        return _wav_quality(_decode(ueba, exe), info.get("samples")) is None
    except Exception:
        return False


_FFMPEG: dict | None = None               # memoized ffmpeg_status()


def ffmpeg_status(refresh: bool = False, prefer: str = "") -> dict:
    """Which FFmpeg build to use: {exe, capable, version, candidates, tried, hint}.

    Probes candidates in order and stops at the first one whose decoded audio passes
    _wav_quality(); results are stored per build in cache/ffmpeg_probe.json (keyed by mtime+size),
    so a rebuild or a new nightly is picked up but the probe itself never runs twice. `prefer` puts
    a path the user just typed (not saved yet) at the front of the search.
    """
    global _FFMPEG
    if _FFMPEG is not None and not refresh and not prefer:
        return _FFMPEG
    cands = ffmpeg_candidates(prefer)
    cache = _probe_cache_load()
    good, tried = "", []
    for exe in cands:
        rec = cache.get(exe)
        if rec and rec.get("stamp") == _stamp(exe):
            ok = bool(rec.get("ok"))
        else:
            ok = _probe(exe)
            cache[exe] = {"stamp": _stamp(exe), "ok": ok, "version": ffmpeg_version(exe)}
            _probe_cache_save(cache)
        tried.append((exe, ok))
        if ok:
            good = exe
            break
    first = cands[0] if cands else ""
    _FFMPEG = {"exe": good or first, "capable": bool(good), "version": ffmpeg_version(good or first),
               "candidates": cands, "tried": tried,
               "hint": "" if good else (BINKA_HINT if cands else
                                        f"kein FFmpeg gefunden — Suchpfade: {FFMPEG_ENV}, "
                                        f"Einstellung {FFMPEG_SETTING}, PATH")}
    return _FFMPEG


def mark_uncapable(exe: str) -> None:
    """Remember that a build decoded garbage, and drop the memo so the next call re-probes."""
    if not exe:
        return
    cache = _probe_cache_load()
    cache[os.path.realpath(exe)] = {"stamp": _stamp(exe), "ok": False, "version": ffmpeg_version(exe)}
    _probe_cache_save(cache)
    globals()["_FFMPEG"] = None


def ffmpeg_exe() -> str:
    """The FFmpeg build to use ('' when there is none). Use ffmpeg_status()["capable"] to know
    whether it can actually decode BinkA."""
    return ffmpeg_status()["exe"]


def ffmpeg_cached() -> dict:
    """Like ffmpeg_status() but never probes: the memo if this process already determined it, else
    what cache/ffmpeg_probe.json says. Cheap enough to call from a GUI thread (no subprocess), and
    a miss simply reports capable=False — call ffmpeg_status() when an answer is really needed."""
    if _FFMPEG is not None:
        return _FFMPEG
    cands = ffmpeg_candidates()
    disk = _probe_cache_load()
    tried, good = [], ""
    for exe in cands:
        rec = disk.get(exe)
        if rec and rec.get("stamp") == _stamp(exe):
            ok = bool(rec.get("ok"))
            tried.append((exe, ok))
            if ok and not good:
                good = exe
    first = good or (cands[0] if cands else "")
    version = (disk.get(first) or {}).get("version", "") if first else ""
    return {"exe": first, "capable": bool(good), "version": version or (ffmpeg_version(first) if first else ""),
            "candidates": cands, "tried": tried, "cached": True,
            "hint": "" if good else (BINKA_HINT if cands else
                                     f"kein FFmpeg gefunden — Suchpfade: {FFMPEG_ENV}, "
                                     f"Einstellung {FFMPEG_SETTING}, PATH")}


def ueba_supported() -> bool:
    """Can the selected FFmpeg decode a UEBA container? Probed with a real sound, then cached."""
    return ffmpeg_status()["capable"]


def bink_wav(gpath: str, exe: str = "") -> tuple[bytes, dict]:
    """WAV of a BinkA sound through an external FFmpeg: rebuild the UEBA container in a temp file,
    let FFmpeg decode it, return the WAV bytes. Refuses to hand out garbage — the decoded audio is
    checked (_wav_quality) and a build that fails is remembered as unusable."""
    st = ffmpeg_status()
    use = os.path.realpath(exe) if exe else st["exe"]
    if not use:
        raise RuntimeError("FFmpeg ist nicht installiert — BinkA-Klänge brauchen FFmpeg zum Abspielen")
    ueba, info = binka_bytes(gpath)
    try:
        data = _decode(ueba, use)
    except RuntimeError as e:
        raise RuntimeError(f"BinkA-Dekodierung schlug fehl ({e}); {BINKA_HINT} — {ffmpeg_version(use)}") \
            from None
    bad = _wav_quality(data, info.get("samples"))
    if bad:
        if not exe:
            mark_uncapable(use)
        raise RuntimeError(f"BinkA ließ sich nicht dekodieren ({bad}); {BINKA_HINT} — "
                           f"{ffmpeg_version(use)}")
    return data, dict(info, file_size=len(data))


def playback_wav(gpath: str) -> tuple[bytes, str]:
    """Playable WAV bytes of any sound: raw PCM directly, BinkA via external FFmpeg.
    Returns (bytes, way) with way 'pcm' or 'ffmpeg'."""
    if _pcm_shape(gpath):
        data, _info = wav_bytes(gpath)
        return data, "pcm"
    data, _info = bink_wav(gpath)
    return data, "ffmpeg"


def decode_wav(item_or_gpath, path: str | None = None, binka_fallback: bool = True) -> tuple[str, str]:
    """Write one sound as a WAV that a normal player opens: raw PCM straight away, BinkA through
    FFmpeg. Returns (path, kind) with kind 'wav', or 'binka: <reason>' when the FFmpeg build could
    not decode — then the UEBA container was written instead, so the sound is not lost, only not
    playable on this machine."""
    gpath = item_or_gpath if isinstance(item_or_gpath, str) else item_or_gpath.gpath
    if path is None:
        path = out_path(item_or_gpath, ".wav")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    try:
        data = (wav_bytes(gpath) if _pcm_shape(gpath) else bink_wav(gpath))[0]
    except (RuntimeError, ValueError) as e:
        if not binka_fallback:
            raise
        alt = out_path(item_or_gpath, ".binka")
        export_binka(item_or_gpath, alt)
        return alt, f"binka: {e}"
    with open(path, "wb") as f:
        f.write(data)
    return path, "wav"


def player_exe(ffmpeg_path: str = "") -> str:
    """Player for decoded sound: ffplay next to the FFmpeg build, else on PATH, else ''."""
    cands = ([os.path.join(os.path.dirname(ffmpeg_path), _exe("ffplay"))] if ffmpeg_path else [])
    cands += [shutil.which(_exe("ffplay")), shutil.which("paplay"), shutil.which("aplay"),
              shutil.which("cvlc")]
    for c in cands:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return ""


def play_wav(path: str, ffmpeg_path: str = "", stderr_to: str = "") -> tuple[str, "object"]:
    """Start a player for a WAV in the background. Returns (player name, Popen or None): ffplay of
    the FFmpeg build if present, else ffplay/paplay/aplay/cvlc from PATH, on Windows the file
    association. ('', None) when nothing was started; call proc.wait() to block until the end.
    stderr_to: write the player's stderr to this file instead of throwing it away (diagnosis)."""
    import subprocess
    p = player_exe(ffmpeg_path)
    if not p:
        if os.name == "nt":
            os.startfile(path)                      # noqa: S606 - documented Windows fallback
            return "startfile", None
        return "", None
    argv = ([p, "-nodisp", "-autoexit", "-loglevel", "error", path]
            if os.path.basename(p).startswith("ffplay") else [p, path])
    err, fh = subprocess.DEVNULL, None
    if stderr_to:
        try:
            fh = open(stderr_to, "wb")
            err = fh
        except OSError:
            pass
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err)
    if fh:
        fh.close()
    return os.path.basename(p), proc


def wav_stats(data: bytes) -> dict:
    """(duration, rate, channels, peak, rms) of decoded WAV bytes — used to report playback."""
    import io
    import wave
    import numpy as np
    with wave.open(io.BytesIO(data)) as w:
        n, rate, ch, width = w.getnframes(), w.getframerate(), w.getnchannels(), w.getsampwidth()
        pcm = w.readframes(n)
    if width != 2:
        return {"dauer": n / max(rate, 1), "rate": rate, "kanal": ch, "peak": None, "rms": None}
    a = np.frombuffer(pcm, dtype="<i2").astype(np.float64) / 32768.0
    return {"dauer": n / max(rate, 1), "rate": rate, "kanal": ch,
            "peak": float(np.abs(a).max()), "rms": float(np.sqrt((a ** 2).mean()))}


# ---------- export ----------------------------------------------------------------------------
def _safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text or "").strip("_")


def out_path(item, ext: str = ".binka", base: str = OUT_DIR) -> str:
    sub = _safe(getattr(item, "folder", ""))
    name = _safe(getattr(item, "name", "") or item.rsplit("/", 1)[-1])
    return os.path.join(base, sub, name + ext) if sub else os.path.join(base, name + ext)


PREVIEW_DIR = os.path.join(tempfile.gettempdir(), "mapexport_klang")


def preview_wav(name: str) -> str:
    """WAV path for a decoded preview: one stable file per sound (playing the same sound again
    overwrites it) instead of a fresh temp file per play, so repeats leave no junk in /tmp."""
    return os.path.join(PREVIEW_DIR, _safe(str(name).rsplit("/", 1)[-1]) or "klang") + ".wav"


def export_binka(item_or_gpath, path: str | None = None) -> str:
    """Write the rebuilt UEBA container (.binka) of one sound."""
    gpath = item_or_gpath if isinstance(item_or_gpath, str) else item_or_gpath.gpath
    if path is None:
        path = out_path(item_or_gpath)
    data, _info = binka_bytes(gpath)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return path


def export_wav(item_or_gpath, path: str | None = None) -> str:
    """Write the 16-bit WAV of a raw-PCM sound (PCM family only)."""
    gpath = item_or_gpath if isinstance(item_or_gpath, str) else item_or_gpath.gpath
    if path is None:
        path = out_path(item_or_gpath, ".wav")
    data, _info = wav_bytes(gpath)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return path


def export_raw(item_or_gpath, path: str | None = None) -> str:
    """Copy the .ubulk byte-for-byte (also works for the sounds without a 'SEEK' chunk)."""
    gpath = item_or_gpath if isinstance(item_or_gpath, str) else item_or_gpath.gpath
    if path is None:
        path = out_path(item_or_gpath, ".ubulk")
    data = ubulk_bytes(gpath)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return path


def write_catalog(cat: list[Item], path: str) -> str:
    """JSON catalogue (name, path, folder, duration, rate, channels, bytes, samples)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump([{"name": it.name, "pfad": it.gpath, "ordner": it.folder, "dauer_s": it.duration,
                    "rate": it.rate, "kanal": it.channels, "bytes": it.size, "samples": it.samples,
                    "format": "pcm" if it.pcm else ("binka" if it.ubulk else None)} for it in cat],
                  f, ensure_ascii=False, indent=1)
    return path


# ---------- command line ----------------------------------------------------------------------
HELP = """audio – Klang-Katalog, UEBA-/WAV-Export und Wiedergabe

    python tools/mapexport.py audio list   [--filter=NAME] [--limit=N] [--json=DATEI] [--alle]
                                           [--sort=name|dauer|groesse|format] [--format=binka|pcm]
    python tools/mapexport.py audio info   NAME
    python tools/mapexport.py audio play   NAME [--out=DATEI] [--ohne-play] [--player=PROGRAMM]
    python tools/mapexport.py audio export NAME [NAME …] [--out=ORDNER] [--raw] [--wav]
    python tools/mapexport.py audio export --alle [--filter=NAME] [--limit=N]
    python tools/mapexport.py audio ffmpeg [--neu] [--alle] [--ffmpeg=PFAD]

list/info/export lesen die SoundWave-Eigenschaften (Dauer, Samplerate, Kanäle). export schreibt
Roh-PCM-Klänge als WAV und BinkA-Klänge in einen neu aufgebauten UEBA-Container (.binka, in Unreal
abspielbar); --raw kopiert die .ubulk unverändert. --wav will auch BinkA-Klänge als WAV — dafür
übersetzt ein externes FFmpeg die Blöcke, denn BinkA ist ein proprietärer Codec von RAD Game Tools
und MapExporter selbst hat keinen Decoder. Welcher FFmpeg-Build das kann, steht nirgends in einer
Versionsnummer: `audio ffmpeg` probiert alle gefundenen Builds an einem echten Klang und prüft die
dekodierten Samples (Ergebnis pro Build in cache/ffmpeg_probe.json). Gesucht wird in $ARC_FFMPEG,
in der Einstellung "ffmpeg" (Datei oder Ordner), in PATH und in üblichen Ordner wie
~/.local/bin oder tools/vendor. Ohne passenden Build bleibt es bei .binka.
play dekodiert einen Klang und spielt ihn sofort ab (ffplay desselben Builds, sonst paplay/aplay/
ffplay aus PATH); die Vorschau-WAV liegt in <Temp>/mapexport_klang/<Name>.wav (beim erneuten
Abspielen desselben Klangs wird sie überschrieben) — --out schreibt sie zusätzlich wohin du willst,
--ohne-play dekodiert nur.
info/play brauchen keinen vollen Katalog — sie suchen im Container-Index (in ~1 s statt ~1 min).
Ohne --alle blendet list Meta-Assets (SoundCues, Attenuationen …) ohne Klangdaten aus.
"""


def _cmd_ffmpeg(flags, t0, opt):
    """Report which FFmpeg builds exist and which of them can decode a UEBA container."""
    st = ffmpeg_status(refresh="--neu" in flags, prefer=opt("--ffmpeg") or "")
    print(f"benutzter Build : {st['exe'] or '— kein FFmpeg gefunden'}")
    print(f"Version         : {st['version'] or '—'}")
    if st["capable"]:
        print("BinkA (UEBA)    : JA — dekodiert und als WAV brauchbar")
    else:
        print(f"BinkA (UEBA)    : NEIN — {st['hint']}")
    others = [e for e, _ok in st["tried"]]
    if "--alle" in flags or not st["capable"]:
        print(f"geprüft         : {len(others)} Build(s)")
        for exe, ok in st["tried"]:
            print(f"   {'ok  ' if ok else 'nein'}  {exe}  [{ffmpeg_version(exe)}]")
        if len(st["candidates"]) > len(others):
            print(f"   … {len(st['candidates']) - len(others)} weitere Kandidaten wurden nicht geprüft "
                  f"(der erste passende reicht): {', '.join(st['candidates'][len(others):])}")
    print(f"({time.time() - t0:.0f} s)")
    return 0 if st["capable"] else 1


def _cmd_play(words, flags, opt, t0):
    """Decode one sound and play it right away (the point of the whole audio path)."""
    if not words:
        raise SystemExit("play braucht einen Asset-Namen")
    hits = find(words[0])
    if not hits:
        raise SystemExit(f"kein Klang-Asset '{words[0]}'")
    it = next((h for h in hits if h.ubulk), hits[0])
    if not it.ubulk:
        raise SystemExit(f"{it.name} hat keine Klangdaten (nur Meta-Asset)")
    exe = opt("--player") or ""
    st = ffmpeg_status()
    t = time.time()
    try:
        if exe:                              # force this build instead of the probed one
            data, way = ((wav_bytes(it.gpath)[0], "pcm") if _pcm_shape(it.gpath, size=it.size)
                         else (bink_wav(it.gpath, exe)[0], "ffmpeg"))
        else:
            data, way = playback_wav(it.gpath)
    except (RuntimeError, ValueError) as e:
        raise SystemExit(str(e))
    s = wav_stats(data)
    target = opt("--out") or preview_wav(it.name)
    os.makedirs(os.path.dirname(os.path.abspath(target)) or ".", exist_ok=True)
    with open(target, "wb") as f:
        f.write(data)
    peak = f"{s['peak']:.3f}" if s["peak"] is not None else "?"
    rms = f"{20 * math.log10(s['rms']):.1f} dBFS" if s["rms"] else "?"
    print(f"{it.name}: {s['dauer']:.2f} s · {s['rate']} Hz · {s['kanal']} ch · Peak {peak} · {rms} "
          f"· {'Roh-PCM' if way == 'pcm' else 'BinkA über FFmpeg dekodiert'} "
          f"({ffmpeg_version(st['exe']) if way == 'ffmpeg' else 'kein FFmpeg nötig'})")
    print(f"WAV → {target}  ({len(data) / 1e6:.2f} MB, {time.time() - t:.1f} s)")
    if "--ohne-play" in flags:
        return
    errfile = target + ".player.log"
    player, proc = play_wav(target, exe or st["exe"], stderr_to=errfile)
    if not player:
        print("kein Abspieler gefunden (ffplay/paplay/aplay) — WAV manuell öffnen")
        return
    time.sleep(0.7)                       # a missing audio output aborts immediately
    if proc is not None and proc.poll() is not None and proc.returncode:
        why = ""
        try:
            lines = [l for l in Path(errfile).read_text(encoding="utf-8", errors="replace")
                     .splitlines() if l.strip()]
            why = lines[-1] if lines else ""
        except OSError:
            pass
        print(f"Abspielen fehlgeschlagen ({player}, Code {proc.returncode})"
              + (f": {why}" if why else " — kein Audioausgang?"))
        print(f"dekodiert ist der Klang trotzdem: {target}")
        _rm(errfile)
        return
    print(f"▶ {player} läuft …")
    try:
        if proc is not None:
            proc.wait(timeout=s["dauer"] + 30)
    except KeyboardInterrupt:
        proc.terminate()
        print("(abgebrochen)")
    except Exception:
        pass                              # a player without audio device support simply exits
    _rm(errfile)
    print(f"fertig in {time.time() - t0:.0f} s")


def _rm(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        raise SystemExit(HELP)
    cmd, rest = argv[0], argv[1:]
    flags = [a for a in rest if a.startswith("--")]
    words = [a for a in rest if not a.startswith("--")]

    def opt(name, default=None):
        return next((a[len(name) + 1:] for a in flags if a.startswith(name)), default)

    if cmd not in ("list", "info", "export", "play", "ffmpeg"):
        raise SystemExit(HELP)

    t0 = time.time()
    if cmd == "ffmpeg":
        raise SystemExit(_cmd_ffmpeg(flags, t0, opt))
    if cmd == "play":
        _cmd_play(words, flags, opt, t0)
        return

    flt = (opt("--filter") or "").lower()
    limit = int(opt("--limit") or 0)
    cat = [it for it in catalogue(limit=limit)
           if not flt or flt in it.name.lower() or flt in it.folder.lower()]

    if cmd == "list":
        shown = cat if "--alle" in flags else [it for it in cat if it.ubulk]
        hidden = len(cat) - len(shown)
        if (want := (opt("--format") or "").lower()):
            shown = [it for it in shown
                     if want in (("pcm" if it.pcm else "binka") if it.ubulk else "meta")]
        key = {"name": lambda it: it.name.lower(), "dauer": lambda it: -(it.duration or 0),
               "groesse": lambda it: -it.size, "format": lambda it: ("pcm" if it.pcm else "binka")}
        shown.sort(key=key.get((opt("--sort") or "name").lower(), key["name"]))
        print(f"{len(shown)} Klang-Assets ({time.time() - t0:.0f} s), Suche '{flt or '—'}'"
              + (f" — {hidden} Meta-Assets ohne Klangdaten ausgeblendet (--alle zeigt sie)" if hidden else ""))
        for it in shown:
            d = f"{it.duration:6.2f} s" if it.duration else "   —   "
            fmt = "PCM" if it.pcm else ("BinkA" if it.ubulk else "—")
            print(f"{d}  {it.rate or 0:>5} Hz  {(it.channels or 0) or '?'}ch  {it.size / 1e6:7.3f} MB  "
                  f"{fmt:>5}  {it.name}  [{it.folder}]")
        if (jp := opt("--json")):
            print("Katalog geschrieben:", write_catalog(shown, jp))
        return

    if cmd == "info":
        if not words:
            raise SystemExit("info braucht einen Asset-Namen")
        hits = find(words[0])                 # index-only lookup: no need for the full catalogue
        if not hits:
            raise SystemExit(f"kein Klang-Asset '{words[0]}'")
        if len(hits) > 1:
            print("Treffer:", ", ".join(it.name for it in hits))
        it = hits[0]
        st = stream(it.gpath) if it.ubulk else {}
        pcm = _pcm_shape(it.gpath, size=it.size) if it.ubulk else None
        if pcm:
            st = {"seek": False, "segments": [], "blocks": 0, "max_block": 0, "trim": None,
                  "samples": pcm[0], "bytes": it.size}
        print(json.dumps({"name": it.name, "pfad": it.gpath, "ordner": it.folder, "dauer_s": it.duration,
                          "rate": it.rate, "kanal": it.channels, "ubulk_bytes": it.size,
                          "format": "pcm" if pcm else ("binka" if st.get("seek") else None),
                          "samples": st.get("samples") or it.samples, "seek": st.get("seek"),
                          "ffmpeg_kann_binka": (None if pcm else ffmpeg_status()["capable"]),
                          "segmente": [{"start": s.get("start"), "eintraege": s.get("entries"),
                                        "samples_pro_block": s.get("samples_per_entry"), "trim": s.get("trim"),
                                        "groesse": s.get("bytes"), "max_block": s.get("max_block")}
                                       for s in st.get("segments", [])],
                          "blocks": st.get("blocks"), "max_block": st.get("max_block"),
                          "blockgroessen": [s for s, _ in sum((s2.get("blocks", []) for s2 in st.get("segments", [])), [])],
                          }, ensure_ascii=False, indent=1))
        return

    if cmd == "export":
        _cmd_export(cat, words, flags, opt, t0)


def _cmd_export(cat, words, flags, opt, t0):
    names = [w.lower() for w in words]
    todo = cat if (not names or "alle" in names) else [it for it in cat
                                                       if any(n == it.name.lower() or n in it.name.lower() for n in names)]
    raw, want_wav = "--raw" in flags, "--wav" in flags
    wav_mode = want_wav                             # stays True only while FFmpeg reads UEBA
    out_dir = opt("--out") or OUT_DIR
    done = fails = skipped = 0
    kind_count = {"wav": 0, "binka": 0, "raw": 0}
    total = 0
    for n, it in enumerate(todo):
        if not it.ubulk:
            skipped += 1                          # SoundCue/Attenuation/Curve: nothing to export
            continue
        try:
            if raw:
                p, kind = export_raw(it, out_path(it, ".ubulk", out_dir)), "raw"
            elif it.pcm:
                p, kind = export_wav(it, out_path(it, ".wav", out_dir)), "wav"
            elif wav_mode:
                try:
                    data, _info = bink_wav(it.gpath)
                except (RuntimeError, ValueError) as e:
                    wav_mode = False
                    print(f"HINWEIS {it.name}: {e} → wieder als .binka")
                    p, kind = export_binka(it, out_path(it, ".binka", out_dir)), "binka"
                else:
                    p = out_path(it, ".wav", out_dir)
                    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
                    with open(p, "wb") as f:
                        f.write(data)
                    kind = "wav"
            else:
                p, kind = export_binka(it, out_path(it, ".binka", out_dir)), "binka"
            total += os.path.getsize(p)
            done += 1
            kind_count[kind] += 1
        except Exception as e:
            fails += 1
            print(f"FEHLER {it.name}: {type(e).__name__}: {e}")
        if n % 200 == 199:
            report_progress(n + 1, len(todo))
    report_progress(len(todo), len(todo))
    parts = [f"{n} {k}" for k, n in (("WAV", kind_count["wav"]), ("UEBA", kind_count["binka"]),
                                     (".ubulk", kind_count["raw"])) if n]
    print(f"{done} exportiert ({', '.join(parts) or '—'}, {total / 1e6:.1f} MB) nach {out_dir}"
          f", {fails} Fehler" + (f", {skipped} ohne Klangdaten übersprungen" if skipped else "")
          + f", {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()
