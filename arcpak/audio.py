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

The container header (BinkAudioFileHeader, tag 'UEBA': version, channels, rate, sample_count,
max block size, flags, file size, seek entry count, blocks per entry) was stripped when the sound
was moved into the .ubulk — 'UEBA' appears nowhere in the shipped game files. Re-embedding
therefore rebuilds it from the block walk plus the SoundWave properties.

A small family (~320 of the streamed sounds — short weapon/launcher mechanics and similar) stores
raw 16-bit PCM instead of BinkA blocks: no 'SEEK' chunk at all, and the .ubulk is exactly
duration * rate * channels * 2 bytes long, interleaved stereo. Those export as playable WAV.

BinkA is a proprietary codec of RAD Game Tools. MapExporter reads and re-embeds its container
(playable with Unreal Engine tooling) and leaves the block bitstreams exactly as the game stored
them; it ships no BinkA decoder. For listening, bink_wav() re-decodes the blocks with an external
FFmpeg (its own clean-room Bink Audio decoders). Whether that works depends on the FFmpeg build,
not its version: released FFmpeg (8.x/9.x) only knows the legacy '1FCB' layout and decodes ARCs
BA2 blocks to garbage, so the capability is probed once and the decoded audio is checked.
"""
from __future__ import annotations

import json
import os
import re
import struct
import sys
import time
from dataclasses import dataclass

from . import game
from . import models
from .config import report_progress

OUT_DIR = os.path.join(models.HERE, "out", "audio")
AUDIO_DIR = "/Pioneer/Audio/"                   # container path fragment of all cooked sound
SEEK_MAGIC = b"SEEK"
UEBA_MAGIC = b"UEBA"
BLOCK_MAGIC = 0x9999
TRIM_MARK = 0xFFFF
SEEK_HEADER_SIZE = 15
UEBA_HEADER_SIZE = 28
# the game's decoder never validates the version (FFmpeg master's UEBA branch probes for 1)
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
        ukey = ukeys.get(stem)
        bkey = bkeys.get(stem)
        gpath = models.to_gpath(ukey or bkey)
        meta = _sound_wave(gpath) if ukey else None
        dur, rate, ch = meta if meta else (None, None, None)
        size = _sizes(bkey) if bkey else 0
        item = Item(name=ukey.rsplit("/", 1)[-1][:-len(".uasset")] if ukey else stem,
                    gpath=gpath, folder=_folder(ukey or bkey), duration=dur, rate=rate, channels=ch,
                    size=size, ubulk=bool(bkey))
        if dur and rate:
            item.samples = int(round(dur * rate))
        item.pcm = bool(bkey and _pcm_shape(gpath, size=size, meta=meta))
        out.append(item)
        if n % 1000 == 999:
            report_progress(n + 1, len(todo))
    report_progress(len(todo), len(todo))
    return out


def find(name: str, cat: list[Item] | None = None) -> list[Item]:
    """Catalogue items matching a name: exact first, then substring, case-insensitive."""
    cat = catalogue() if cat is None else cat
    n = name.lower()
    return [it for it in cat if it.name.lower() == n] or [it for it in cat if n in it.name.lower()]


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


def stream(gpath: str) -> dict:
    """Shape of a streamed sound: its segments with the seek tables and the block sizes."""
    key = models.to_key(gpath, ".ubulk")
    hits = models.lookup(key)
    if not hits:
        raise KeyError(key)
    data = hits[0][0].get_chunk(hits[0][1])
    segs, p, offset = [], 0, 0
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
                     "max_block": max((s for s, _ in sizes), default=0), "bytes": SEEK_HEADER_SIZE + 2 * count + used})
        p = base + used
    blocks = [b for s in segs for b in s.get("blocks", [])]
    samples = None
    if all(s.get("seek") for s in segs) and all(s.get("read") == s.get("entries") for s in segs):
        samples = sum((s["read"] - 1) * s["samples_per_entry"] + (s["trim"] if s["trim"] is not None
                                                             else s["samples_per_entry"]) for s in segs)
    return {"bytes": len(data), "seek": bool(segs) and segs[0]["seek"], "segments": segs,
            "blocks": len(blocks), "max_block": max((s for s, _ in blocks), default=0),
            "trim": segs[-1].get("trim") if segs else None, "samples": samples}


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


def binka_bytes(gpath: str) -> tuple[bytes, dict]:
    """UEBA container of one sound: rebuilt header + one seek table + the blocks as stored.

    version/flags are the documented UE defaults (they are nowhere in the game data), rate and
    channels come from the SoundWave properties, sample_count from the block walk."""
    key = models.to_key(gpath, ".ubulk")
    hits = models.lookup(key)
    if not hits:
        raise KeyError(key)
    data = hits[0][0].get_chunk(hits[0][1])
    info = stream(gpath)
    if not info["seek"]:
        raise ValueError("kein SEEK-Chunk — nur Roh-Export möglich")
    blks = _payloads(data)
    if not blks:
        raise ValueError("keine Blöcke")
    meta = _sound_wave(gpath) or ()
    rate = int((meta[1] if len(meta) > 1 else None) or 48000)
    ch = int((meta[2] if len(meta) > 2 else None) or 1)
    samples = info["samples"] if info["samples"] is not None else int(round((meta[0] or 0) * rate))
    table = bytearray()
    prev = 0
    pos = 0
    for payload, trim in blks:                 # deltas between consecutive block starts
        table += struct.pack("<H", pos - prev)
        prev = pos
        pos += 4 + len(payload) + (4 if trim is not None else 0)
    body = b"".join(struct.pack("<HH", BLOCK_MAGIC, len(p) if trim is None else TRIM_MARK)
                    + (b"" if trim is None else struct.pack("<HH", len(p), trim)) + p for p, trim in blks)
    total = UEBA_HEADER_SIZE + len(table) + len(body)
    head = struct.pack("<4sBBHIIHHIHH", UEBA_MAGIC, UEBA_VERSION, ch, 0, rate, samples,
                       max(len(p) for p, _ in blks), BA2_FLAGS, total, len(blks), 1)
    assert len(head) == UEBA_HEADER_SIZE
    return head + bytes(table) + body, dict(info, rate=rate, channels=ch, samples=samples, file_size=total)


def wav_bytes(gpath: str) -> tuple[bytes, dict]:
    """16-bit WAV of a raw-PCM sound (PCM family only — BinkA blocks are not decoded)."""
    shape = _pcm_shape(gpath)
    if not shape:
        raise ValueError("kein Roh-PCM — BinkA-Blöcke werden nicht dekodiert (--raw für die .ubulk)")
    frames, rate, ch = shape
    key = models.to_key(gpath, ".ubulk")
    hits = models.lookup(key)
    if not hits:
        raise KeyError(key)
    data = hits[0][0].get_chunk(hits[0][1])
    if len(data) != frames * ch * 2:
        raise ValueError(f"{len(data)} Bytes passen nicht zu {frames} PCM-Rahmen à {ch * 2} Bytes")
    head = struct.pack("<4sI4s", b"RIFF", 36 + len(data), b"WAVE")
    head += struct.pack("<4sIHHIIHH", b"fmt ", 16, 1, ch, rate, rate * ch * 2, ch * 2, 16)
    head += struct.pack("<4sI", b"data", len(data))
    return head + data, dict(rate=rate, channels=ch, samples=frames, file_size=len(head) + len(data))


# ---------- playback ---------------------------------------------------------------------------
def ffmpeg_exe():
    import shutil
    return shutil.which("ffmpeg")


def ffmpeg_version() -> str:
    """FFmpeg's first version line, or '' when FFmpeg is not installed."""
    exe = ffmpeg_exe()
    if not exe:
        return ""
    import subprocess
    try:
        r = subprocess.run([exe, "-hide_banner", "-version"], capture_output=True, timeout=20)
        return r.stdout.decode("utf-8", "replace").splitlines()[0].strip()
    except Exception:
        return exe


_ueba_ok: bool | None = None        # None = not probed; False = this FFmpeg cannot decode BinkA

# Whether FFmpeg can decode a UEBA container depends on the BUILD, not on the version number.
# Released FFmpeg (8.x, 9.x) ships a 'binka' demuxer that only knows the legacy '1FCB' layout, and
# that path never sets the decoder's "Bink Audio 2" extradata — ARCs BA2 blocks then decode to
# full-scale garbage of the right length. FFmpeg master has a UEBA branch, but it tests
# AV_RL32(buf) == MKBETAG('U','E','B','A'), which a real "UEBA" file can never satisfy (a
# little-endian read never equals a big-endian tag). So: never guess from a version string —
# probe once with a real sound and check the decoded audio.
BINKA_HINT = ("Dieser FFmpeg-Build dekodiert kein BinkA: der 'binka'-Demuxer kennt nur das alte "
              "1FCB-Layout und setzt die BA2-Kennung nicht, die ARCs Blöcke brauchen. "
              "Der .binka-Export (UEBA) bleibt in Unreal abspielbar")

def _wav_quality(data: bytes, expect_samples: int | None) -> str | None:
    """None if the decoded WAV looks like audio, else a reason. Catches the classic BinkA failure
    mode: the demuxer lacks the BA2 extradata, the decoder reads the wrong bit widths and emits
    full-scale garbage that still has the right length."""
    import array
    import io
    import wave
    try:
        with wave.open(io.BytesIO(data)) as w:
            n, rate = w.getnframes(), w.getframerate()
            pcm = array.array("h", w.readframes(n))
    except Exception as e:
        return f"kein lesbares WAV ({type(e).__name__})"
    if not pcm:
        return "leeres WAV"
    if expect_samples and n and abs(n - expect_samples) > max(expect_samples // 2, rate // 10):
        return f"Länge {n / max(rate, 1):.2f}s statt {expect_samples / max(rate, 1):.2f}s"
    clipped = sum(1 for v in pcm if v <= -32768 or v >= 32767)
    if clipped > len(pcm) // 2:
        return f"{100 * clipped // len(pcm)} % der Samples am Vollausschlag (Dekoder-Müll)"
    return None

def _decode(blob: bytes, exe: str) -> bytes:
    """Run FFmpeg on a container in a temp dir and return the WAV bytes (raises on failure)."""
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        src, dst = os.path.join(td, "s.binka"), os.path.join(td, "s.wav")
        with open(src, "wb") as f:
            f.write(blob)
        try:
            r = subprocess.run([exe, "-hide_banner", "-loglevel", "error", "-i", src,
                                "-vn", "-acodec", "pcm_s16le", "-y", dst],
                               capture_output=True, timeout=300)
        except subprocess.TimeoutExpired:
            raise RuntimeError("FFmpeg time out beim Dekodieren")
        if r.returncode != 0 or not os.path.exists(dst) or os.path.getsize(dst) < 44:
            err = [l for l in r.stderr.decode("utf-8", "replace").splitlines() if l.strip()]
            raise RuntimeError(err[-1] if err else "FFmpeg erzeugte kein WAV")
        with open(dst, "rb") as f:
            return f.read()

def ueba_supported() -> bool:
    """Can the installed FFmpeg decode a UEBA container? Probed once with a real sound and cached."""
    global _ueba_ok
    if _ueba_ok is not None:
        return _ueba_ok
    exe = ffmpeg_exe()
    if not exe:
        return False
    try:
        gpath = _probe_sound()
        if gpath is None:
            return False
        ueba, info = binka_bytes(gpath)
        _ueba_ok = _wav_quality(_decode(ueba, exe), info.get("samples")) is None
    except Exception:
        _ueba_ok = False
    return _ueba_ok

def _probe_sound() -> str | None:
    """Smallest BinkA sound of the catalogue (the probe should stay cheap)."""
    cands = [it for it in catalogue() if not it.pcm and it.ubulk]
    if not cands:
        return None
    return min(cands, key=lambda it: it.size or 0).gpath

def bink_wav(gpath: str) -> tuple[bytes, dict]:
    """WAV for a BinkA sound via external FFmpeg (its clean-room Bink Audio decoder):
    write the rebuilt UEBA container to a temp file, let ffmpeg decode it, return the WAV bytes.

    Depends on the FFmpeg build, not on its version (see BINKA_HINT): ueba_supported() probes it
    once, and the decoded audio is quality-checked so garbage is never passed on as a result."""
    global _ueba_ok
    exe = ffmpeg_exe()
    if not exe:
        raise RuntimeError("FFmpeg ist nicht installiert — BinkA-Klänge brauchen FFmpeg zum Abspielen")
    if not ueba_supported():
        raise RuntimeError(f"{BINKA_HINT} ({ffmpeg_version() or exe})")
    ueba, info = binka_bytes(gpath)
    data = _decode(ueba, exe)
    bad = _wav_quality(data, info.get("samples"))
    if bad:
        _ueba_ok = False
        raise RuntimeError(f"BinkA ließ sich nicht dekodieren ({bad}); {BINKA_HINT}")
    return data, dict(info, file_size=len(data))

def playback_wav(gpath: str) -> tuple[bytes, str]:
    """Playable WAV bytes of any sound: raw PCM directly, BinkA via external FFmpeg.
    Returns (bytes, way) with way 'pcm' or 'ffmpeg'."""
    if _pcm_shape(gpath):
        data, _info = wav_bytes(gpath)
        return data, "pcm"
    data, _info = bink_wav(gpath)
    return data, "ffmpeg"


# ---------- export ----------------------------------------------------------------------------
def _safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text or "").strip("_")


def out_path(item, ext: str = ".binka", base: str = OUT_DIR) -> str:
    sub = _safe(getattr(item, "folder", ""))
    name = _safe(getattr(item, "name", "") or item.rsplit("/", 1)[-1])
    return os.path.join(base, sub, name + ext) if sub else os.path.join(base, name + ext)


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
    key = models.to_key(gpath, ".ubulk")
    hits = models.lookup(key)
    if not hits:
        raise KeyError(key)
    data = hits[0][0].get_chunk(hits[0][1])
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
HELP = """audio – Klang-Katalog, UEBA- und WAV-Export

    python tools/mapexport.py audio list   [--filter=NAME] [--limit=N] [--json=DATEI] [--alle]
    python tools/mapexport.py audio info   NAME
    python tools/mapexport.py audio export NAME [NAME …] [--out=ORDNER] [--raw] [--wav]
    python tools/mapexport.py audio export --alle [--filter=NAME] [--limit=N]

list/info/export lesen die SoundWave-Eigenschaften (Dauer, Samplerate, Kanäle). export schreibt
Roh-PCM-Klänge als WAV und BinkA-Klänge in einen neu aufgebauten UEBA-Container (.binka, in
Unreal abspielbar); --raw kopiert die .ubulk unverändert. --wav versucht auch BinkA-Klänge als
WAV — dafür wird ein externes FFmpeg benutzt; kann der lokale Build BinkA nicht dekodieren (die
veröffentlichten FFmpeg-Versionen können es nicht), bleibt es bei .binka. BinkA ist ein
proprietärer Codec von RAD Game Tools — MapExporter selbst hat
keinen BinkA-Decoder. Ohne --alle blendet list Meta-Assets (SoundCues, Attenuationen …) ohne
Klangdaten aus.
"""


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        raise SystemExit(HELP)
    cmd, rest = argv[0], argv[1:]
    flags = [a for a in rest if a.startswith("--")]
    words = [a for a in rest if not a.startswith("--")]

    def opt(name, default=None):
        return next((a[len(name) + 1:] for a in flags if a.startswith(name)), default)

    if cmd not in ("list", "info", "export"):
        raise SystemExit(HELP)

    t0 = time.time()
    flt = (opt("--filter") or "").lower()
    limit = int(opt("--limit") or 0)
    cat = [it for it in catalogue(limit=limit)
           if not flt or flt in it.name.lower() or flt in it.folder.lower()]

    if cmd == "list":
        shown = cat if "--alle" in flags else [it for it in cat if it.ubulk]
        hidden = len(cat) - len(shown)
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
        hits = find(words[0], cat)
        if not hits:
            raise SystemExit(f"kein Klang-Asset '{words[0]}'")
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
                          "segmente": [{"start": s.get("start"), "eintraege": s.get("entries"),
                                        "samples_pro_block": s.get("samples_per_entry"), "trim": s.get("trim"),
                                        "groesse": s.get("bytes"), "max_block": s.get("max_block")}
                                       for s in st.get("segments", [])],
                          "blocks": st.get("blocks"), "max_block": st.get("max_block"),
                          "blockgroessen": [s for s, _ in sum((s2.get("blocks", []) for s2 in st.get("segments", [])), [])],
                          }, ensure_ascii=False, indent=1))
        return

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
