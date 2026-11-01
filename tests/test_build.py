import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

from PIL import Image, ImageDraw

from handwrite.build import build_full, ResumeBuilder
from handwrite.state import BuildState, BuildStateError, BuildConflictError
from handwrite.sheettopng import ALL_CHARS

STUB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fontforge_stub.py")
SHEET = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "test_data",
    "sheettopng",
    "excellent.jpg",
)
CONFIG = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "test_data",
    "config_data",
    "default.json",
)
METADATA = {"filename": "MyFont"}


class BuildTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.mkdtemp()
        self.chars = os.path.join(self.temp, "characters")
        self.out = os.path.join(self.temp, "font")
        os.makedirs(self.chars)
        os.makedirs(self.out)
        # Put a fontforge-compatible stub first on PATH so the tests do not
        # require a native fontforge installation.
        self.bindir = os.path.join(self.temp, "bin")
        os.makedirs(self.bindir)
        stub_exec = os.path.join(self.bindir, "fontforge")
        shutil.copyfile(STUB, stub_exec)
        os.chmod(stub_exec, os.stat(stub_exec).st_mode | stat.S_IEXEC | stat.S_IXGRP)
        self._old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = self.bindir + os.pathsep + self._old_path

    def tearDown(self):
        os.environ["PATH"] = self._old_path
        shutil.rmtree(self.temp)

    # ------------------------------------------------------------- helpers

    def full_build(self, force=False):
        return build_full(
            SHEET,
            self.out,
            self.chars,
            CONFIG,
            dict(METADATA),
            persistent=True,
            force=force,
        )

    def resume(self, **kwargs):
        kwargs.setdefault("metadata", {})
        return ResumeBuilder(self.chars, output_directory=self.out, **kwargs).run()

    def state(self):
        return BuildState.open(self.chars)

    def paths(self, cp):
        d = os.path.join(self.chars, str(cp))
        return (
            os.path.join(d, "{}.png".format(cp)),
            os.path.join(d, "{}.bmp".format(cp)),
            os.path.join(d, "{}.svg".format(cp)),
        )

    def font_path(self):
        return os.path.join(self.out, "MyFont.ttf")

    def hash(self, path):
        with open(path, "rb") as f:
            return f.read()

    def sha(self, path):
        import hashlib

        return hashlib.sha256(self.hash(path)).hexdigest()

    def snapshot_svgs(self):
        snap = {}
        for cp in ALL_CHARS:
            _p, _b, svg = self.paths(cp)
            if os.path.isfile(svg):
                snap[cp] = (self.hash(svg), os.path.getmtime(svg))
        return snap

    def unchanged(self, snap, codepoints):
        changed = []
        for cp in codepoints:
            _p, _b, svg = self.paths(cp)
            data, mtime = self.hash(svg), os.path.getmtime(svg)
            if snap[cp] != (data, mtime):
                changed.append(cp)
        return changed

    def edit_png(self, cp):
        png, _bmp, _svg = self.paths(cp)
        img = Image.open(png).convert("RGBA")
        draw = ImageDraw.Draw(img)
        draw.rectangle([2, 2, img.width - 3, 10], fill=(0, 0, 0, 255))
        img.save(png)

    def edit_svg(self, cp):
        _png, _bmp, svg = self.paths(cp)
        with open(svg) as f:
            data = f.read()
        with open(svg, "w") as f:
            f.write(data.replace("<svg", "<!-- manual fix --><svg", 1))

    def write_config(self, threshold):
        with open(CONFIG) as f:
            cfg = json.load(f)
        cfg["threshold_value"] = threshold
        path = os.path.join(self.temp, "changed_config.json")
        with open(path, "w") as f:
            json.dump(cfg, f)
        return path

    # ----------------------------------------------------- initial build

    def test_full_build_records_state(self):
        self.full_build()
        state = self.state()
        self.assertTrue(os.path.isfile(state.manifest_path))
        self.assertTrue(state.manifest["run"]["completed"])
        self.assertEqual(len(state.codepoints()), len(ALL_CHARS))
        for cp in ALL_CHARS:
            rec = state.record(cp)
            png, bmp, svg = self.paths(cp)
            # original crop and tool-generated outline are archived
            self.assertTrue(os.path.isfile(state.original_png(cp)))
            self.assertTrue(os.path.isfile(state.generated_svg(cp)))
            self.assertEqual(rec["png"]["origin"], "sheet")
            self.assertEqual(rec["svg"]["origin"], "potrace")
            self.assertEqual(rec["bmp"]["from_png"], rec["png"]["sha256"])
            self.assertEqual(rec["svg"]["from_bmp"], rec["bmp"]["sha256"])
        self.assertTrue(os.path.isfile(self.font_path()))
        self.assertEqual(state.manifest["font"]["sha256"], self.sha(self.font_path()))

    # ---------------------------------------------------------- no-op run

    def test_resume_noop_rebuilds_nothing(self):
        self.full_build()
        before = self.hash(self.font_path())
        snap = self.snapshot_svgs()
        report = self.resume()
        self.assertFalse(report["font_rebuilt"])
        self.assertEqual(report["svgs_rebuilt"], [])
        self.assertEqual(report["bmps_rebuilt"], [])
        self.assertEqual(self.hash(self.font_path()), before)
        self.assertEqual(self.unchanged(snap, ALL_CHARS), [])

    # ----------------------------------------------------- single SVG edit

    def test_resume_adopts_manual_svg_and_keeps_others(self):
        self.full_build()
        snap = self.snapshot_svgs()
        before = self.hash(self.font_path())
        self.edit_svg(65)
        time.sleep(0.01)
        report = self.resume()
        self.assertEqual(report["svg_edits"], [65])
        self.assertTrue(report["font_rebuilt"])
        self.assertNotEqual(self.hash(self.font_path()), before)
        state = self.state()
        rec = state.record(65)
        self.assertEqual(rec["svg"]["origin"], "manual")
        self.assertEqual(rec["svg"]["sha256"], self.sha(self.paths(65)[2]))
        # every other glyph is byte and mtime identical
        self.assertEqual(self.unchanged(snap, [cp for cp in ALL_CHARS if cp != 65]), [])

    # ----------------------------------------------------- single PNG edit

    def test_resume_adopts_manual_png_and_rebuilds_chain(self):
        self.full_build()
        snap = self.snapshot_svgs()
        old_svg_66 = self.hash(self.paths(66)[2])
        self.edit_png(66)
        time.sleep(0.01)
        report = self.resume()
        self.assertEqual(report["png_edits"], [66])
        self.assertEqual(report["bmps_rebuilt"], [66])
        self.assertEqual(report["svgs_rebuilt"], [66])
        self.assertTrue(report["font_rebuilt"])
        # the modification reached the effective outline ...
        self.assertNotEqual(self.hash(self.paths(66)[2]), old_svg_66)
        state = self.state()
        self.assertEqual(state.record(66)["png"]["origin"], "manual")
        self.assertEqual(state.record(66)["svg"]["origin"], "potrace")
        # ... and no other glyph moved
        self.assertEqual(self.unchanged(snap, [cp for cp in ALL_CHARS if cp != 66]), [])

    # --------------------------------------------- png and svg same codepoint

    def test_resume_conflicts_when_png_and_svg_both_edited(self):
        self.full_build()
        self.edit_png(69)
        self.edit_svg(69)
        before_png = self.hash(self.paths(69)[0])
        before_font = self.hash(self.font_path())
        with self.assertRaises(BuildConflictError) as ctx:
            self.resume()
        self.assertTrue(any("69" in c for c in ctx.exception.conflicts))
        self.assertEqual(self.hash(self.paths(69)[0]), before_png)
        self.assertEqual(self.hash(self.font_path()), before_font)

    # ------------------------------------------------------- config conflicts

    def test_resume_rejects_config_change_over_edit(self):
        self.full_build()
        self.edit_png(67)
        changed = self.write_config(150)
        before_png = self.hash(self.paths(67)[0])
        before_font = self.hash(self.font_path())
        with self.assertRaises(BuildConflictError) as ctx:
            self.resume(config=changed)
        self.assertTrue(any("67" in c for c in ctx.exception.conflicts))
        # nothing was touched
        self.assertEqual(self.hash(self.paths(67)[0]), before_png)
        self.assertEqual(self.hash(self.font_path()), before_font)
        self.assertNotEqual(
            self.state().manifest["config"]["path"], os.path.abspath(changed)
        )

    def test_resume_force_takes_new_crops_and_backs_up_edit(self):
        self.full_build()
        self.edit_png(67)
        changed = self.write_config(150)
        report = self.resume(config=changed, force=True)
        self.assertIn(67, report["crops_replaced"])
        state = self.state()
        self.assertEqual(state.record(67)["png"]["origin"], "sheet")
        self.assertTrue(state.manifest["run"]["completed"])
        backups = []
        for root, _d, files in os.walk(os.path.join(state.state_dir, "backup")):
            backups.extend(os.path.join(root, f) for f in files)
        self.assertTrue(any(p.endswith("67.png") for p in backups))

    # --------------------------------------------------------- interruption

    def test_interrupted_run_is_not_trusted(self):
        self.full_build()
        before = self.hash(self.font_path())
        state = self.state()
        state.manifest["run"]["completed"] = False
        state.manifest["run"]["id"] = "interrupted"
        state.save()
        # stale partial outputs from the interrupted run
        with open(os.path.join(self.out, ".MyFont.interrupted.ttf"), "wb") as f:
            f.write(b"PARTIAL")
        _png, bmp, _svg = self.paths(66)
        os.remove(bmp)
        leftover = os.path.join(os.path.dirname(bmp), ".tmp-66.bmp.leftover.bmp")
        with open(leftover, "wb") as f:
            f.write(b"x")
        report = self.resume()
        # bmp rebuilt but identical content -> svg is NOT re-traced
        self.assertEqual(report["bmps_rebuilt"], [66])
        self.assertEqual(report["svgs_rebuilt"], [])
        self.assertTrue(report["font_rebuilt"])
        self.assertFalse(
            os.path.exists(os.path.join(self.out, ".MyFont.interrupted.ttf"))
        )
        self.assertFalse(os.path.exists(leftover))
        self.assertTrue(self.state().manifest["run"]["completed"])
        self.assertEqual(self.hash(self.font_path()), before)

    # ------------------------------------- pixel-identical png resave

    def test_png_resave_with_same_pixels_skips_later_stages(self):
        self.full_build()
        png, _bmp, _svg = self.paths(68)
        Image.open(png).convert("RGBA").save(png)
        report = self.resume()
        self.assertEqual(report["png_edits"], [68])
        self.assertEqual(report["svgs_rebuilt"], [])
        self.assertFalse(report["font_rebuilt"])

    # ------------------------------------------------- one-shot protection

    def test_full_rebuild_into_edited_dir_is_refused(self):
        self.full_build()
        self.edit_png(68)
        with self.assertRaises(BuildConflictError):
            self.full_build(force=False)
        # --force performs the clean rebuild and backs the edit up
        state = self.full_build(force=True)
        self.assertTrue(state.manifest["run"]["completed"])
        self.assertEqual(state.record(68)["png"]["origin"], "sheet")

    # ------------------------------------------------------ missing state

    def test_resume_without_state_is_an_error(self):
        empty = os.path.join(self.temp, "empty")
        os.makedirs(empty)
        with self.assertRaises(BuildStateError):
            ResumeBuilder(empty, output_directory=self.out).run()

    # ------------------------------------------------------------- CLI e2e

    def test_cli_resume_and_conflict_exit_code(self):
        env = dict(os.environ)
        base = [
            sys.executable,
            "-m",
            "handwrite.cli",
        ]
        initial = subprocess.run(
            base
            + [
                SHEET,
                self.out,
                "--directory",
                self.chars,
                "--config",
                CONFIG,
                "--filename",
                "MyFont",
            ],
            env=env,
        )
        self.assertEqual(initial.returncode, 0)
        resumed = subprocess.run(base + ["resume", "--directory", self.chars], env=env)
        self.assertEqual(resumed.returncode, 0)
        # conflict path exits non-zero without touching files
        self.edit_png(67)
        changed = self.write_config(150)
        before = self.hash(self.font_path())
        blocked = subprocess.run(
            base + ["resume", "--directory", self.chars, "--config", changed],
            env=env,
        )
        self.assertEqual(blocked.returncode, 2)
        self.assertEqual(self.hash(self.font_path()), before)


if __name__ == "__main__":
    unittest.main()
