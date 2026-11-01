"""Persistent build state for incremental (resume) font builds.

Every retained build directory (``--directory``) gets a hidden
``.handwrite/`` folder containing:

* ``manifest.json`` -- per-codepoint record of the original sheet crop, any
  manual modification and the outline the font was last built from;
* ``originals/png/<codepoint>.png`` -- pristine crop produced from the sheet,
  kept separately so a hand-edited working PNG can be detected and never
  silently overwritten;
* ``generated/<codepoint>.svg`` -- the last outline produced by potrace, so a
  hand-edited working SVG is detectable;
* ``backup/<run-id>/...`` -- copies of manual edits before an explicit
  ``--force`` run replaces them.

The manifest is the only source of truth for "what belongs to the current
build". Files are always written via temporary files + ``os.replace`` and the
manifest is saved after every codepoint, so an interrupted run can never leave
a state where a partial derivative is treated as a valid result.
"""

import hashlib
import json
import os
import tempfile

STATE_DIRNAME = ".handwrite"
MANIFEST_NAME = "manifest.json"

# Bump when the recorded schema or the pipeline stages change.
BUILD_VERSION = 1

ORIGIN_SHEET = "sheet"
ORIGIN_MANUAL = "manual"
ORIGIN_POTRACE = "potrace"


class BuildStateError(Exception):
    """Raised when the build state directory is missing or incompatible."""


class BuildConflictError(Exception):
    """Raised when a rebuild would silently discard a manual edit.

    The exception lists every conflict so the caller can report them all at
    once instead of failing on the first codepoint.
    """

    def __init__(self, conflicts):
        self.conflicts = list(conflicts)
        message = "Refusing to overwrite manual edits:\n  - " + "\n  - ".join(
            self.conflicts
        )
        super().__init__(message)


def sha256_file(path):
    """Return the hex sha256 of a file, or ``None`` if it does not exist."""
    if not os.path.isfile(path):
        return None
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def atomic_write_bytes(path, data):
    """Write bytes to ``path`` atomically (temp file + ``os.replace``)."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix="." + os.path.basename(path) + ".", suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def atomic_copy(src, dst):
    with open(src, "rb") as f:
        atomic_write_bytes(dst, f.read())


class BuildState:
    """Load, create and persist the build manifest for a characters directory."""

    def __init__(self, characters_dir, manifest):
        self.characters_dir = os.path.abspath(characters_dir)
        self.state_dir = os.path.join(self.characters_dir, STATE_DIRNAME)
        self.manifest_path = os.path.join(self.state_dir, MANIFEST_NAME)
        self.manifest = manifest

    # ------------------------------------------------------------------ open

    @classmethod
    def open(cls, characters_dir):
        """Open an existing build state, raising if none/incompatible exists."""
        state_dir = os.path.join(os.path.abspath(characters_dir), STATE_DIRNAME)
        manifest_path = os.path.join(state_dir, MANIFEST_NAME)
        if not os.path.isfile(manifest_path):
            raise BuildStateError(
                "No previous build state found in {}. Run a full build with "
                "'handwrite SHEET OUTPUT_DIRECTORY --directory {}' first.".format(
                    characters_dir, characters_dir
                )
            )
        with open(manifest_path) as f:
            manifest = json.load(f)
        if manifest.get("build_version") != BUILD_VERSION:
            raise BuildStateError(
                "Build state in {} was written by an incompatible handwrite "
                "version (found build_version {}, expected {}).".format(
                    characters_dir,
                    manifest.get("build_version"),
                    BUILD_VERSION,
                )
            )
        return cls(characters_dir, manifest)

    @classmethod
    def create(cls, characters_dir, sheet, config, metadata, constants):
        """Create a fresh empty manifest for a new full (retained) build."""
        manifest = {
            "build_version": BUILD_VERSION,
            "sheet": {
                "path": os.path.abspath(sheet["path"]),
                "sha256": sheet["sha256"],
            },
            "config": {
                "path": os.path.abspath(config["path"]),
                "sha256": config["sha256"],
                "threshold_value": config["threshold_value"],
            },
            "metadata": dict(metadata),
            "constants": dict(constants),
            "run": {
                "id": None,
                "completed": False,
                "started_at": None,
                "finished_at": None,
            },
            "codepoints": {},
            "font": {
                "input_hash": None,
                "output_directory": None,
                "output": None,
                "sha256": None,
                "run_id": None,
            },
        }
        state = cls(characters_dir, manifest)
        state.save()
        return state

    # ------------------------------------------------------------- persistence

    def save(self):
        atomic_write_bytes(
            self.manifest_path,
            (json.dumps(self.manifest, indent=2, sort_keys=True) + "\n").encode(
                "utf-8"
            ),
        )

    # --------------------------------------------------------------- locations

    def original_png(self, codepoint):
        return os.path.join(
            self.state_dir, "originals", "png", "{}.png".format(codepoint)
        )

    def generated_svg(self, codepoint):
        return os.path.join(self.state_dir, "generated", "{}.svg".format(codepoint))

    def backup_dir(self, run_id):
        return os.path.join(self.state_dir, "backup", run_id)

    def backup_copy(self, run_id, kind, codepoint, suffix):
        return os.path.join(
            self.backup_dir(run_id), kind, "{}{}".format(codepoint, suffix)
        )

    # --------------------------------------------------------------- codepaths

    def codepoint_dir(self, codepoint):
        return os.path.join(self.characters_dir, str(codepoint))

    def working_path(self, codepoint, suffix):
        return os.path.join(
            self.codepoint_dir(codepoint), "{}{}".format(codepoint, suffix)
        )

    def codepoints(self):
        return sorted(int(cp) for cp in self.manifest["codepoints"])

    def record(self, codepoint):
        return self.manifest["codepoints"].setdefault(
            str(codepoint),
            {
                "png": {
                    "sha256": None,
                    "original_sha256": None,
                    "origin": ORIGIN_SHEET,
                },
                "bmp": {"sha256": None, "from_png": None},
                "svg": {
                    "sha256": None,
                    "generated_sha256": None,
                    "from_bmp": None,
                    "origin": ORIGIN_POTRACE,
                },
            },
        )

    # ------------------------------------------------------- status inspection

    def png_is_manual(self, codepoint):
        """True if the working PNG differs from the recorded sheet crop."""
        rec = self.record(codepoint)["png"]
        working = sha256_file(self.working_path(codepoint, ".png"))
        return (
            working is not None
            and rec.get("original_sha256") is not None
            and working != rec["original_sha256"]
        )

    def svg_is_manual(self, codepoint):
        """True if the working SVG differs from the last potrace output."""
        rec = self.record(codepoint)["svg"]
        working = sha256_file(self.working_path(codepoint, ".svg"))
        return (
            working is not None
            and rec.get("generated_sha256") is not None
            and working != rec["generated_sha256"]
        )

    def manual_edits(self):
        """Return ``[(kind, codepoint), ...]`` for every hand-edited file."""
        edits = []
        for codepoint in self.codepoints():
            if self.png_is_manual(codepoint):
                edits.append(("png", codepoint))
            if self.svg_is_manual(codepoint):
                edits.append(("svg", codepoint))
        return edits
