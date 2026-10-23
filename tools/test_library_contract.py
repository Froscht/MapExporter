"""Regression guards for the library contract of arcpak (no game data needed).

    python -m pytest tools/test_library_contract.py -q     (or: python tools/test_library_contract.py)

Why this exists: arcpak used to read sys.argv at import time and mount the containers on import,
which made the step modules unimportable, untestable and impossible to run twice per process.
These tests fail if that ever comes back.
"""
import ast
import importlib
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import unittest

HERE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))          # the tools scripts run with the repo root on sys.path
STEP_MODULES = ("arcpak.placements", "arcpak.playarea", "arcpak.mapglb")


def _run_py(code: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", code], cwd=HERE, capture_output=True, text=True,
                          env={"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
                               **({"SYSTEMROOT": os.environ["SYSTEMROOT"]} if "SYSTEMROOT" in os.environ else {})})


class TestNoImportSideEffects(unittest.TestCase):
    def test_step_modules_import_without_argv(self):
        """Importing a step module must not raise StopIteration from a missing positional arg."""
        for mod in STEP_MODULES:
            with self.subTest(mod=mod):
                r = _run_py(f"import {mod}; print('ok')")
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn("ok", r.stdout)

    def test_import_does_not_mount_containers(self):
        """Importing must stay cheap: the context is built on first access, not on import."""
        code = (
            "import arcpak.game as g\n"
            "assert g._CACHE == {}, g._CACHE\n"
            "import arcpak.placements, arcpak.playarea, arcpak.mapglb\n"
            "assert g._CACHE == {}, g._CACHE\n"
            "print('ok')\n"
        )
        r = _run_py(code)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ok", r.stdout)

    def test_context_is_lazy_and_shared(self):
        code = (
            "import arcpak.game as g\n"
            "assert g.containers() is g.containers()\n"
            "assert g.C is g.containers()\n"          # PEP 562 proxy resolves to the same list
            "assert g.smap is g.script_map()\n"
            "assert g.templates() is g.templates()\n"
            "g.reset_context()\n"
            "assert g._CACHE == {}\n"
            "print('ok')\n"
        )
        r = _run_py(code)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ok", r.stdout)

    def test_no_sys_argv_on_module_level(self):
        """sys.argv may only be read inside main(), never at module level."""
        for path in sorted((HERE / "arcpak").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:                    # module level only
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue                          # inside a function = called on demand
                if (isinstance(node, ast.If) and ast.unparse(node.test).strip() ==
                        "__name__ == '__main__'"):
                    continue                          # the __main__ block runs only as a script
                for sub in ast.walk(node):
                    if (isinstance(sub, ast.Attribute) and sub.attr == "argv"
                            and isinstance(sub.value, ast.Name) and sub.value.id == "sys"):
                        self.fail(f"{path.name}: sys.argv read at module level (line {sub.lineno})")


class TestStepApi(unittest.TestCase):
    def test_every_step_has_run_and_main(self):
        for mod in STEP_MODULES:
            with self.subTest(mod=mod):
                m = importlib.import_module(mod)
                self.assertTrue(callable(getattr(m, "run", None)), f"{mod}.run fehlt")
                self.assertTrue(callable(getattr(m, "main", None)), f"{mod}.main fehlt")

    def test_parse_options_maps_the_documented_flags(self):
        glb = importlib.import_module("arcpak.mapglb")
        o = glb.parse_options(["PracticeRangeArray", "--crop", "--ohne-blaetter", "--ohne-decals",
                               "--ohne-terrain", "--spielbereich"])
        self.assertEqual(o["name"], "PracticeRangeArray")
        self.assertTrue(o["crop"])
        self.assertTrue(o["ohne_blaetter"])
        self.assertTrue(o["ohne_decals"])
        self.assertFalse(o["terrain"])
        self.assertTrue(o["spielbereich"])
        self.assertIsNone(o["out"])               # run() fills in out/map/<name>.glb

        pa = importlib.import_module("arcpak.playarea")
        o = pa.parse_options(["Maps/X/", "--cell=50", "--hang=aus", "--strikt", "--seed=1,2"])
        self.assertEqual(o["filter_sub"], "Maps/X/")
        self.assertEqual(o["cell"], 50.0)
        self.assertIsNone(o["hang"])
        self.assertTrue(o["strikt"])
        self.assertEqual(o["seed"], (1.0, 2.0))

        pl = importlib.import_module("arcpak.placements")
        o = pl.parse_options(["Maps/X/"])
        self.assertTrue(o.out.endswith("X_placements.jsonl"))

    def test_missing_map_argument_is_a_clean_error(self):
        """A bare `mapexport.py glb` must print usage instead of raising StopIteration."""
        for mod, argv in (("arcpak.mapglb", []), ("arcpak.placements", []), ("arcpak.playarea", [])):
            with self.subTest(mod=mod):
                m = importlib.import_module(mod)
                with self.assertRaises(SystemExit) as cm:
                    m.main(argv)
                self.assertIn("braucht", str(cm.exception))

    def test_main_forwards_options_by_keyword(self):
        """main() must translate the parsed options into run()'s parameters (a positional
        pass-through of the Options object was a real bug)."""
        pl = importlib.import_module("arcpak.placements")
        o = pl.parse_options(["Maps/X/"])
        self.assertEqual(o.map_filter, "Maps/X/")
        self.assertTrue(o.out.endswith("X_placements.jsonl"))
        self.assertEqual(list(o.__dict__), ["map_filter", "out"])

        glb = importlib.import_module("arcpak.mapglb")
        names = set(glb.parse_options(["X"]))
        import inspect
        params = set(inspect.signature(glb.run).parameters)
        self.assertEqual(names, params, "parse_options() and run() must agree")

    def test_no_play_area_signal(self):
        """NoPlayArea is a SystemExit carrying exit code 3 (the GUI/`all` key off it)."""
        pa = importlib.import_module("arcpak.playarea")
        e = pa.NoPlayArea("test")
        self.assertIsInstance(e, SystemExit)
        self.assertEqual(e.code, 3)


class TestPureHelpers(unittest.TestCase):
    def test_progress_protocol(self):
        """The GUI parses '@@PROGRESS <done> <total>'; keep the wire format stable."""
        from arcpak.config import PROGRESS_PREFIX, parse_progress
        self.assertEqual(parse_progress(f"{PROGRESS_PREFIX} 7 10"), (7, 10))
        self.assertIsNone(parse_progress("normale Ausgabe"))
        self.assertIsNone(parse_progress(f"{PROGRESS_PREFIX} kaputt"))

    def test_bounds_check_is_scale_aware(self):
        """bounds_check compares a transformed mesh box against the cooked FBoxSphereBounds."""
        from arcpak.placements import bounds_check
        ident = [[1.0, 0, 0, 0], [0, 1.0, 0, 0], [0, 0, 1.0, 0], [0, 0, 0, 1.0]]
        mesh_bounds = [0.0, 0.0, 0.0, 100.0, 100.0, 100.0]
        do, de, ok = bounds_check([ident], mesh_bounds, [0.0, 0.0, 0.0, 100.0, 100.0, 100.0])
        self.assertTrue(ok)
        self.assertAlmostEqual(do, 0.0)
        self.assertAlmostEqual(de, 0.0)
        moved = [[1.0, 0, 0, 500.0], [0, 1.0, 0, 0], [0, 0, 1.0, 0], [0, 0, 0, 1.0]]
        do, de, ok = bounds_check([moved], mesh_bounds, [0.0, 0.0, 0.0, 100.0, 100.0, 100.0])
        self.assertFalse(ok)
        self.assertAlmostEqual(do, 500.0)

    def test_gltf_axis_convention(self):
        """UE cm left-handed Z-up -> glTF m right-handed Y-up: (x, z, y) * 0.01. Do not "fix" this."""
        from arcpak.uetransform import to_gltf_matrix
        ident = [[1.0, 0, 0, 100.0], [0, 1.0, 0, 200.0], [0, 0, 1.0, 300.0], [0, 0, 0, 1.0]]
        flat = to_gltf_matrix(ident)              # flattened column-major
        self.assertEqual(len(flat), 16)
        # translation 100/200/300 cm -> 1/3/2 m (x, z, y)
        self.assertAlmostEqual(flat[12], 1.0)
        self.assertAlmostEqual(flat[13], 3.0)
        self.assertAlmostEqual(flat[14], 2.0)


class TestAudioLayer(unittest.TestCase):
    """Guards for the sound path: the UEBA container layout, the sample check that decides whether
    an FFmpeg build may produce a WAV, and the search for that build. None of these need game data."""

    def test_audio_import_stays_cheap(self):
        """arcpak.audio is imported by the GUI and by tests: it must not mount the containers."""
        code = ("import arcpak.audio\nimport arcpak.game as g\n"
                "assert g._CACHE == {}, g._CACHE\nprint('ok')\n")
        r = _run_py(code)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ok", r.stdout)

    def test_ueba_container_constants(self):
        """The header the game's SoundWave decoder and FFmpeg's binka demuxer both expect. The u32
        0x55454241 little-endian is b"ABEU" on disk — writing b"UEBA" instead makes the file unusable."""
        import struct
        from arcpak import audio
        self.assertEqual(audio.UEBA_MAGIC, b"ABEU")
        self.assertEqual(audio.UEBA_MAGIC, struct.pack("<I", 0x55454241))
        self.assertEqual(audio.BLOCK_MAGIC, 0x9999)
        self.assertEqual(audio.SEEK_MAGIC, b"SEEK")

    @staticmethod
    def _wav(pcm: bytes, width=2, rate=48000, ch=1) -> bytes:
        import io
        import wave
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(ch); w.setsampwidth(width); w.setframerate(rate)
            w.writeframes(pcm)
        return buf.getvalue()

    def test_wav_quality_is_the_real_gatekeeper(self):
        """ffmpeg_status() only trusts a build whose decoded samples look like audio: silence,
        wrong bit width, wrong length and full-scale garbage must all be rejected, a tone accepted."""
        import numpy as np
        from arcpak import audio
        tone = (np.sin(2 * np.pi * 1000 * np.arange(48000) / 48000) * 12000).astype("<i2").tobytes()
        self.assertIsNone(audio._wav_quality(self._wav(tone), 48000))
        self.assertIn("Stille", audio._wav_quality(self._wav(np.zeros(48000, "<i2").tobytes()), 48000))
        self.assertIn("16-bit", audio._wav_quality(self._wav(tone, width=4), 48000))
        self.assertIn("Länge", audio._wav_quality(self._wav(tone), 4 * 48000))
        garbage = np.full(48000, 32700, dtype="<i2").tobytes()
        self.assertIn("Vollausschlag", audio._wav_quality(self._wav(garbage), 48000))
        self.assertIsNotNone(audio._wav_quality(b"kein WAV", 48000))

    def test_ffmpeg_candidate_search(self):
        """prefer first, duplicates dropped, folders expanded, non-executables ignored."""
        import tempfile
        from arcpak import audio
        with tempfile.TemporaryDirectory() as td:
            night = os.path.join(td, "bin", audio._exe("ffmpeg"))
            os.makedirs(os.path.dirname(night))
            with open(night, "w") as f:
                f.write("#!/bin/sh\n")
            os.chmod(night, 0o755)
            broken = os.path.join(td, audio._exe("ffmpeg"))      # not executable
            with open(broken, "w") as f:
                f.write("kein Programm\n")
            got = audio.ffmpeg_candidates(night)
            self.assertEqual(got[0], os.path.realpath(night))    # the just typed path leads
            self.assertNotIn(os.path.realpath(broken), got)
            self.assertEqual(len(got), len(set(got)), "duplicates would mean probing twice")

    def test_probe_cache_keyed_by_build(self):
        """A replaced nightly build must not inherit the old verdict (mtime+size is the stamp)."""
        import tempfile
        from arcpak import audio
        with tempfile.TemporaryDirectory() as td:
            exe = os.path.join(td, audio._exe("ffmpeg"))
            with open(exe, "w") as f:
                f.write("v1")
            os.chmod(exe, 0o755)
            first = audio._stamp(exe)
            self.assertNotEqual(first, "?")
            with open(exe, "w") as f:
                f.write("v2, longer content")
            self.assertNotEqual(first, audio._stamp(exe))
            self.assertEqual(audio._stamp(os.path.join(td, "missing")), "?")

    @unittest.skipUnless(importlib.util.find_spec("PyQt6"), "the Audio tab needs PyQt6")
    def test_gui_play_helpers(self):
        """preview_path() keeps previews in one folder (repeat play overwrites instead of piling up)
        and play_line() formats every placeholder — an unfilled {…} would show up in the window."""
        sys.path.insert(0, str(HERE / "tools"))
        from audio_tab import play_line, preview_path
        a, b = preview_path("Grab_GuitarAcoustic_04"), preview_path("../../etc/passwd")
        self.assertEqual(os.path.dirname(a), os.path.dirname(b))
        self.assertTrue(a.endswith(".wav") and b.endswith(".wav"))
        self.assertNotEqual(a, b)
        self.assertEqual(a, preview_path("Grab_GuitarAcoustic_04"))
        for line in (play_line("/tmp/x.wav", "ffmpeg", {"dauer": 1.234, "rate": 48000, "kanal": 2,
                                                       "peak": 0.51, "player": "ffplay"}),
                     play_line("/tmp/x.wav", "pcm", {}),
                     play_line("/tmp/x.wav", "pcm", {"player_error": "kein Audioausgang"})):
            self.assertNotIn("{", line, line)
        self.assertIn("ffplay", play_line("/tmp/x.wav", "ffmpeg", {"player": "ffplay"}))


class TestTranslations(unittest.TestCase):
    """tools/lang/*.json is keyed by the German source text: a language missing a key silently shows
    German, and a {placeholder} lost in translation raises inside tr() while the tab is running."""

    LANG_DIR = HERE / "tools" / "lang"

    def _tables(self):
        return {p.name: json.loads(p.read_text(encoding="utf-8")) for p in self.LANG_DIR.glob("*.json")}

    def test_languages_agree_on_keys(self):
        tables = self._tables()
        self.assertGreater(len(tables), 1, "the language files disappeared")
        reference = sorted(tables["en.json"])
        for name, table in tables.items():
            self.assertEqual(sorted(table), reference, f"{name} covers a different set of texts")

    def test_placeholders_survive_translation(self):
        import re
        want = lambda s: sorted(set(re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)", s)))  # noqa: E731
        for name, table in self._tables().items():
            for key, value in table.items():
                if isinstance(value, str):
                    self.assertEqual(want(key), want(value),
                                     f"{name}: {key[:50]!r} -> {value[:50]!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
