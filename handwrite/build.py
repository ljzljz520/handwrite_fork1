"""Build orchestration: one-shot full builds and incremental resume builds.

Stage dependency chain per codepoint::

    sheet scan + threshold  ->  <cp>/<cp>.png  ->  <cp>/<cp>.bmp  ->  <cp>/<cp>.svg  ->  font
                                  (editable)                       (derivative)        (editable)

A resume run replans every codepoint from content hashes stored in the
manifest (see :mod:`handwrite.state`):

* a changed source scan only replaces crops for the affected codepoints;
* a modified PNG rebuilds that codepoint's BMP/SVG;
* a modified SVG is taken as the effective outline as-is;
* any situation where a rebuild would silently discard a manual edit is a
  hard conflict (overridable only with an explicit ``--force``, which backs
  the edit up first);
* the font is regenerated only when the effective outlines, config or
  metadata actually changed (or the previous run did not complete).
"""

import json
import os
import uuid
from datetime import datetime, timezone

import cv2

from handwrite.sheettopng import SHEETtoPNG, ALL_CHARS
from handwrite.pngtosvg import PNGtoSVG, BMP_SIZE, BMP_THRESHOLD
from handwrite.svgtottf import SVGtoTTF
from handwrite.state import (
    BuildState,
    BuildStateError,
    BuildConflictError,
    ORIGIN_SHEET,
    ORIGIN_MANUAL,
    ORIGIN_POTRACE,
    BUILD_VERSION,
    sha256_bytes,
    sha256_file,
    atomic_write_bytes,
    atomic_copy,
)

COLS = 8
ROWS = 10


# ------------------------------------------------------------- small helpers


def _normalize_metadata(metadata):
    """Drop CLI metadata keys that were not provided (``None`` values)."""
    if not metadata:
        return {}
    return {k: v for k, v in metadata.items() if v is not None}


def _read_config(config_path):
    with open(config_path, "rb") as f:
        raw = f.read()
    parsed = json.loads(raw.decode("utf-8"))
    threshold = parsed.get("threshold_value", 200)
    return raw, parsed, threshold


def _constants():
    return {
        "cols": COLS,
        "rows": ROWS,
        "bmp_size": BMP_SIZE,
        "bmp_threshold": BMP_THRESHOLD,
    }


def _font_filename(metadata, config_parsed):
    return str(
        metadata.get("filename", None) or config_parsed["props"].get("filename", None)
    )


def _ttf_path(outdir, filename):
    return os.path.join(
        outdir, filename + ".ttf" if not filename.endswith(".ttf") else filename
    )


def _predict_dedup_output(outdir, filename):
    """Path the legacy one-shot font generation would write (dedup copies)."""
    outfile = _ttf_path(outdir, filename)
    while os.path.exists(outfile):
        outfile = os.path.splitext(outfile)[0] + " (1).ttf"
    return outfile


def _font_input_hash(config_raw, sheet_hash, metadata, constants, effective):
    payload = {
        "build_version": BUILD_VERSION,
        "config_sha256": sha256_bytes(config_raw),
        "sheet_sha256": sheet_hash,
        "metadata": metadata,
        "constants": constants,
        "glyphs": [
            {"codepoint": cp, "svg_sha256": svg_hash} for cp, svg_hash in effective
        ],
    }
    return sha256_bytes(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def _cleanup_temps(directory):
    """Remove leftover per-stage temporary files from interrupted runs."""
    for root, _dirs, files in os.walk(directory):
        for name in files:
            if name.startswith(".tmp-"):
                try:
                    os.remove(os.path.join(root, name))
                except OSError:
                    pass


def _cleanup_font_temps(outdir, font_name):
    """Remove hidden ``.<font_name>.<token>.ttf`` files from interrupted runs."""
    if not os.path.isdir(outdir):
        return
    prefix = "." + font_name + "."
    for name in os.listdir(outdir):
        if name.startswith(prefix) and name.endswith(".ttf"):
            try:
                os.remove(os.path.join(outdir, name))
            except OSError:
                pass


def _font_base_name(filename):
    return filename[:-4] if filename.endswith(".ttf") else filename


# =====================================================================
# Full (one-shot) build
# =====================================================================


def build_full(
    sheet,
    output_directory,
    characters_dir,
    config,
    metadata,
    persistent,
    force=False,
):
    """Run the complete sheet -> png -> bmp -> svg -> ttf pipeline.

    Parameters
    ----------
    persistent : bool
        True when the user passed ``--directory``: intermediate files are kept
        and a build manifest is created so that ``resume`` can continue the
        build. False reproduces the original throwaway temp-directory run.
    force : bool, default=False
        Explicitly discard manual edits found in an existing persistent build
        directory (the edits are backed up first).
    """
    normalized = _normalize_metadata(metadata)

    if not persistent:
        _run_chain(sheet, output_directory, characters_dir, config, normalized)
        return None

    state = _try_open_state(characters_dir)
    if state is not None:
        edits = state.manual_edits()
        if edits and not force:
            conflicts = [
                "{} for codepoint {} ({!r}) in {} has been hand-edited; a full "
                "rebuild would overwrite it. Use 'handwrite resume ...' to keep "
                "the edit, or pass --force to rebuild from scratch.".format(
                    kind.upper(),
                    cp,
                    chr(cp),
                    state.working_path(cp, "." + kind),
                )
                for kind, cp in edits
            ]
            raise BuildConflictError(conflicts)
        run_id = uuid.uuid4().hex
        for kind, cp in edits:
            src = state.working_path(cp, "." + kind)
            suffix = "." + kind
            dst = state.backup_copy(run_id, kind, cp, suffix)
            atomic_copy(src, dst)

    raw_cfg, parsed, threshold = _read_config(config)
    sheet_hash = sha256_file(sheet)
    filename = _font_filename(normalized, parsed)
    # Predict the name *before* generation: the chooser only renames when the
    # candidate already exists, so asking afterwards always picks the wrong
    # duplicate name.
    output_path = _predict_dedup_output(output_directory, filename)

    # Regenerate every stage (original one-shot behaviour).
    _run_chain(sheet, output_directory, characters_dir, config, normalized)

    output_hash = sha256_file(output_path)

    state = BuildState.create(
        characters_dir,
        sheet={"path": sheet, "sha256": sheet_hash},
        config={
            "path": config,
            "sha256": sha256_bytes(raw_cfg),
            "threshold_value": threshold,
        },
        metadata=normalized,
        constants=_constants(),
    )

    for cp in ALL_CHARS:
        png_path = state.working_path(cp, ".png")
        bmp_path = state.working_path(cp, ".bmp")
        svg_path = state.working_path(cp, ".svg")
        if not os.path.exists(png_path):
            continue
        png_h = sha256_file(png_path)
        bmp_h = sha256_file(bmp_path)
        svg_h = sha256_file(svg_path)
        # A fresh full build: working files are tool products by definition.
        atomic_copy(png_path, state.original_png(cp))
        if svg_h is not None:
            atomic_copy(svg_path, state.generated_svg(cp))
        rec = state.record(cp)
        rec["png"] = {
            "sha256": png_h,
            "original_sha256": png_h,
            "origin": ORIGIN_SHEET,
        }
        rec["bmp"] = {"sha256": bmp_h, "from_png": png_h}
        rec["svg"] = {
            "sha256": svg_h,
            "generated_sha256": svg_h,
            "from_bmp": bmp_h,
            "origin": ORIGIN_POTRACE,
        }

    _finalize_font_record(
        state,
        raw_cfg,
        sheet_hash,
        normalized,
        output_directory,
        output_path,
        output_hash,
        run_id=uuid.uuid4().hex,
        completed=True,
    )
    state.save()
    return state


def _run_chain(sheet, output_directory, characters_dir, config, metadata):
    SHEETtoPNG().convert(sheet, characters_dir, config)
    PNGtoSVG().convert(directory=characters_dir)
    SVGtoTTF().convert(characters_dir, output_directory, config, metadata)


def _try_open_state(characters_dir):
    try:
        return BuildState.open(characters_dir)
    except BuildStateError:
        return None


def _finalize_font_record(
    state,
    raw_cfg,
    sheet_hash,
    metadata,
    output_directory,
    output_path,
    output_hash,
    run_id,
    completed,
):
    manifest = state.manifest
    effective = [(cp, state.record(cp)["svg"]["sha256"]) for cp in state.codepoints()]
    input_hash = _font_input_hash(
        raw_cfg, sheet_hash, metadata, manifest["constants"], effective
    )
    manifest["font"] = {
        "input_hash": input_hash,
        "output_directory": os.path.abspath(output_directory),
        "output": os.path.abspath(output_path) if output_path else None,
        "sha256": output_hash,
        "run_id": run_id,
    }
    manifest["run"]["completed"] = completed
    if completed:
        manifest["run"]["finished_at"] = datetime.now(timezone.utc).isoformat()
    return input_hash


# =====================================================================
# Incremental resume build
# =====================================================================


class ResumeBuilder:
    """Continue a persistent build, redoing only the stages that changed."""

    def __init__(
        self,
        characters_dir,
        output_directory=None,
        sheet=None,
        config=None,
        metadata=None,
        force=False,
    ):
        self.state = BuildState.open(characters_dir)
        manifest = self.state.manifest
        self.sheet = os.path.abspath(sheet or manifest["sheet"]["path"])
        self.config = os.path.abspath(config or manifest["config"]["path"])
        recorded_outdir = manifest["font"].get("output_directory") or os.path.dirname(
            manifest["font"].get("output") or ""
        )
        self.output_directory = os.path.abspath(output_directory or recorded_outdir)
        cli_metadata = _normalize_metadata(metadata)
        self.metadata = {**manifest.get("metadata", {}), **cli_metadata}
        self.force = force
        self.png_converter = PNGtoSVG()
        self.report = {
            "crops_replaced": [],
            "png_edits": [],
            "pngs_restored": [],
            "bmps_rebuilt": [],
            "svg_edits": [],
            "svgs_rebuilt": [],
            "conflicts": [],
            "font_rebuilt": False,
            "output": None,
        }

    def run(self):
        state = self.state
        manifest = state.manifest

        self._validate_inputs()
        raw_cfg, parsed, threshold = _read_config(self.config)
        sheet_hash = sha256_file(self.sheet)

        previous_completed = bool(manifest["run"].get("completed"))
        run_id = uuid.uuid4().hex
        self.run_id = run_id
        run_started = datetime.now(timezone.utc)
        manifest["run"] = {
            "id": run_id,
            "completed": False,
            "started_at": run_started.isoformat(),
            "finished_at": None,
        }
        state.save()

        os.makedirs(self.output_directory, exist_ok=True)
        _cleanup_temps(state.characters_dir)
        font_base = _font_base_name(_font_filename(self.metadata, parsed))
        _cleanup_font_temps(self.output_directory, font_base)

        # ---- plan -----------------------------------------------------
        crops = self._detect_changed_crops(sheet_hash, threshold)
        plan = {}
        conflicts = []
        for cp in state.codepoints():
            action, cp_conflicts = self._plan_codepoint(cp, crops)
            plan[cp] = action
            conflicts.extend(cp_conflicts)

        self.report["conflicts"] = conflicts
        if conflicts and not self.force:
            raise BuildConflictError(conflicts)

        # Inputs are accepted: pin the new fingerprints before mutating files.
        manifest["sheet"] = {"path": self.sheet, "sha256": sheet_hash}
        manifest["config"] = {
            "path": self.config,
            "sha256": sha256_bytes(raw_cfg),
            "threshold_value": threshold,
        }
        manifest["metadata"] = dict(self.metadata)
        state.save()

        # ---- execute per-codepoint stages -----------------------------
        for cp in state.codepoints():
            self._execute_codepoint(cp, plan[cp], run_id)
            state.save()

        # ---- font stage ----------------------------------------------
        effective = [
            (cp, state.record(cp)["svg"]["sha256"]) for cp in state.codepoints()
        ]
        input_hash = _font_input_hash(
            raw_cfg,
            sheet_hash,
            self.metadata,
            manifest["constants"],
            effective,
        )
        font_rec = manifest["font"]
        output_path = _ttf_path(
            self.output_directory, _font_filename(self.metadata, parsed)
        )
        font_needed = (
            not previous_completed
            or font_rec.get("input_hash") != input_hash
            or not os.path.isfile(output_path)
            or sha256_file(output_path) != font_rec.get("sha256")
        )

        if font_needed:
            SVGtoTTF().convert(
                state.characters_dir,
                self.output_directory,
                self.config,
                self.metadata,
                overwrite=True,
                token=run_id,
            )
            if (
                not os.path.isfile(output_path)
                or os.path.getsize(output_path) == 0
                or datetime.fromtimestamp(os.path.getmtime(output_path), timezone.utc)
                < run_started
            ):
                raise RuntimeError(
                    "Font generation did not produce {}; keeping the previous "
                    "font (if any) untouched.".format(output_path)
                )
            output_hash = sha256_file(output_path)
            manifest["font"] = {
                "input_hash": input_hash,
                "output_directory": self.output_directory,
                "output": output_path,
                "sha256": output_hash,
                "run_id": run_id,
            }
            self.report["font_rebuilt"] = True
        else:
            output_path = font_rec.get("output") or output_path

        manifest["run"] = {
            "id": run_id,
            "completed": True,
            "started_at": run_started.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        state.save()
        self.report["output"] = output_path
        return self.report

    # ------------------------------------------------------------- planning

    def _validate_inputs(self):
        if os.path.isdir(self.sheet):
            raise IsADirectoryError("Sheet parameter should not be a directory.")
        if not os.path.isfile(self.sheet):
            raise FileNotFoundError(
                "Sheet image not found: {}. Pass the current sheet path "
                "explicitly.".format(self.sheet)
            )
        if os.path.isdir(self.config):
            raise IsADirectoryError("Config parameter should not be a directory.")
        if not os.path.isfile(self.config):
            raise FileNotFoundError(
                "Config file not found: {}. Pass the current config path "
                "explicitly.".format(self.config)
            )
        if not self.output_directory:
            raise BuildStateError(
                "No output directory recorded; pass --output-directory."
            )

    def _detect_changed_crops(self, sheet_hash, threshold):
        """Return ``{codepoint: (png_bytes, sha256)}`` for crops that differ
        from the recorded sheet crop."""
        state = self.state
        manifest = state.manifest
        sheet_changed = sheet_hash != manifest["sheet"]["sha256"]
        threshold_changed = threshold != manifest["config"].get("threshold_value")
        if not sheet_changed and not threshold_changed:
            return {}

        constants = manifest["constants"]
        images = SHEETtoPNG().detect_characters(
            self.sheet,
            threshold,
            cols=constants["cols"],
            rows=constants["rows"],
        )
        crops = {}
        for cp, entry in zip(ALL_CHARS, images):
            ok, buffer = cv2.imencode(".png", entry[0])
            if not ok:
                raise RuntimeError("Failed to encode crop for codepoint {}".format(cp))
            data = buffer.tobytes()
            crop_hash = sha256_bytes(data)
            if crop_hash != state.record(cp)["png"].get("original_sha256"):
                crops[cp] = (data, crop_hash)
        return crops

    def _plan_codepoint(self, cp, crops):
        state = self.state
        rec = state.record(cp)
        conflicts = []

        png_path = state.working_path(cp, ".png")
        bmp_path = state.working_path(cp, ".bmp")
        svg_path = state.working_path(cp, ".svg")

        png_h = sha256_file(png_path)
        bmp_h = sha256_file(bmp_path)
        svg_h = sha256_file(svg_path)
        orig_h = rec["png"].get("original_sha256")
        gen_h = rec["svg"].get("generated_sha256")

        new_crop = crops.get(cp)
        crop_replace = new_crop is not None
        png_restore = False
        png_manual = False
        svg_manual = svg_h is not None and gen_h is not None and svg_h != gen_h

        if crop_replace:
            if png_h is not None and png_h != orig_h:
                conflicts.append(
                    "codepoint {} ({!r}): the source scan produces a different "
                    "crop, but {} has been hand-edited.".format(cp, chr(cp), png_path)
                )
            target_png_h = new_crop[1]
        else:
            if png_h is None:
                if not os.path.isfile(state.original_png(cp)):
                    conflicts.append(
                        "codepoint {} ({!r}): {} is missing and no original crop "
                        "is recorded.".format(cp, chr(cp), png_path)
                    )
                    target_png_h = orig_h
                else:
                    png_restore = True
                    target_png_h = orig_h
            elif png_h != orig_h:
                png_manual = True
                target_png_h = png_h
            else:
                target_png_h = png_h

        # BMP stage: a missing/corrupt file must be rebuilt, but the SVG only
        # depends on the *content* the BMP will have -- rendering it now makes
        # the SVG decision independent of PNG re-saves and deleted temp files.
        bmp_recorded_h = rec["bmp"].get("sha256")
        bmp_file_ok = (
            bmp_h is not None
            and bmp_h == bmp_recorded_h
            and rec["bmp"].get("from_png") == target_png_h
        )
        bmp_rebuild = not bmp_file_ok
        prospective_bmp_h = None
        png_source = new_crop[0] if crop_replace else png_path
        if isinstance(png_source, bytes) or os.path.isfile(png_source):
            prospective_bmp_h = sha256_bytes(
                self.png_converter.render_bmp_bytes(png_source)
            )
        bmp_content_changed = (
            prospective_bmp_h is not None and prospective_bmp_h != bmp_recorded_h
        )

        svg_missing = svg_h is None
        svg_regen = False
        if svg_missing:
            if rec["svg"].get("origin") == ORIGIN_MANUAL:
                conflicts.append(
                    "codepoint {} ({!r}): {} was hand-edited but the file is "
                    "missing; the edit cannot be reproduced.".format(
                        cp, chr(cp), svg_path
                    )
                )
            svg_regen = True
        elif svg_manual and bmp_content_changed:
            conflicts.append(
                "codepoint {} ({!r}): the rasterised PNG changed but {} has "
                "been hand-edited; regenerating the SVG would discard that "
                "edit.".format(cp, chr(cp), svg_path)
            )
            # With an explicit --force the backed-up edit is replaced by a
            # freshly traced outline; otherwise the conflict aborts.
            if self.force:
                svg_regen = True
        elif not svg_manual and bmp_content_changed:
            svg_regen = True

        action = {
            "new_crop": new_crop,
            "crop_replace": crop_replace,
            "png_restore": png_restore,
            "png_manual": png_manual,
            "png_hash": png_h,
            "bmp_hash": bmp_h,
            "bmp_rebuild": bmp_rebuild,
            "bmp_content_changed": bmp_content_changed,
            "target_png_hash": target_png_h,
            "svg_hash": svg_h,
            "svg_manual": svg_manual,
            "svg_regen": svg_regen,
        }
        return action, conflicts

    # ------------------------------------------------------------ execution

    def _execute_codepoint(self, cp, action, run_id):
        state = self.state
        rec = state.record(cp)
        report = self.report

        png_path = state.working_path(cp, ".png")
        bmp_path = state.working_path(cp, ".bmp")
        svg_path = state.working_path(cp, ".svg")

        # ---- PNG stage ---------------------------------------------------
        if action["crop_replace"]:
            data, crop_hash = action["new_crop"]
            if os.path.isfile(png_path) and action["png_hash"] != rec["png"].get(
                "original_sha256"
            ):
                atomic_copy(png_path, state.backup_copy(run_id, "png", cp, ".png"))
            atomic_write_bytes(png_path, data)
            atomic_write_bytes(state.original_png(cp), data)
            rec["png"] = {
                "sha256": crop_hash,
                "original_sha256": crop_hash,
                "origin": ORIGIN_SHEET,
            }
            report["crops_replaced"].append(cp)
        elif action["png_restore"]:
            atomic_copy(state.original_png(cp), png_path)
            rec["png"]["sha256"] = rec["png"]["original_sha256"]
            rec["png"]["origin"] = ORIGIN_SHEET
            report["pngs_restored"].append(cp)
        elif action["png_manual"]:
            rec["png"]["sha256"] = action["png_hash"]
            rec["png"]["origin"] = ORIGIN_MANUAL
            report["png_edits"].append(cp)

        target_png_h = action["target_png_hash"]

        # ---- BMP stage (pure derivative) --------------------------------
        if action["bmp_rebuild"]:
            self.png_converter.pngToBmp(png_path, atomic=True)
            bmp_h = sha256_file(bmp_path)
            rec["bmp"] = {"sha256": bmp_h, "from_png": target_png_h}
            report["bmps_rebuilt"].append(cp)
        else:
            bmp_h = action["bmp_hash"]

        # ---- SVG stage (effective outline) ------------------------------
        if action["svg_regen"]:
            if os.path.isfile(svg_path) and action["svg_manual"]:
                atomic_copy(svg_path, state.backup_copy(run_id, "svg", cp, ".svg"))
            self.png_converter.bmpToSvg(bmp_path, atomic=True)
            svg_h = sha256_file(svg_path)
            atomic_copy(svg_path, state.generated_svg(cp))
            rec["svg"] = {
                "sha256": svg_h,
                "generated_sha256": svg_h,
                "from_bmp": bmp_h,
                "origin": ORIGIN_POTRACE,
            }
            report["svgs_rebuilt"].append(cp)
        elif action["svg_manual"]:
            rec["svg"]["sha256"] = action["svg_hash"]
            rec["svg"]["origin"] = ORIGIN_MANUAL
            report["svg_edits"].append(cp)
        # Else the tool-generated SVG is still in sync: leave it untouched.
