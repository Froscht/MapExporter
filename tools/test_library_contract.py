"""Regression guards for the library contract of arcpak (no game data needed).

    python -m pytest tools/test_library_contract.py -q     (or: python tools/test_library_contract.py)

Why this exists: arcpak used to read sys.argv at import time and mount the containers on import,
which made the step modules unimportable, untestable and impossible to run twice per process.
These tests fail if that ever comes back.
"""
import ast
import importlib
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
