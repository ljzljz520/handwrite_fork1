#!/usr/bin/env python3
"""Minimal stand-in for the ``fontforge`` executable used by the resume tests.

Real fontforge is a heavy dependency that is unavailable on some developer
machines; the CI pipeline installs and runs the real binary. This stub honours
the same invocation contract relied on by :mod:`handwrite.svgtottf`:

    fontforge -script SCRIPT CONFIG DIRECTORY OUTDIR METADATA_JSON

plus the ``HANDWRITE_TTF_OPTIONS`` environment variable (``overwrite`` and
``token``). The produced "font" embeds a hash of every codepoint SVG together
with the config and metadata, which lets tests assert which outlines were
actually used.
"""

import hashlib
import json
import os
import sys

_ = sys.argv[1]  # "-script"
_ = sys.argv[2]  # script path
config_path = sys.argv[3]
directory = sys.argv[4]
outdir = sys.argv[5]
metadata_arg = sys.argv[6]

with open(config_path) as f:
    config = json.load(f)
try:
    metadata = (
        json.loads(metadata_arg) if metadata_arg and metadata_arg != "null" else {}
    )
except ValueError:
    metadata = {}

filename = metadata.get("filename") or config["props"]["filename"]

digest = hashlib.sha256()
for entry in sorted(os.listdir(directory)):
    if not entry.isdigit():
        continue
    svg_path = os.path.join(directory, entry, entry + ".svg")
    if os.path.isfile(svg_path):
        digest.update(entry.encode("utf-8"))
        with open(svg_path, "rb") as f:
            digest.update(f.read())
digest.update(json.dumps(config, sort_keys=True).encode("utf-8"))
digest.update(json.dumps(metadata, sort_keys=True).encode("utf-8"))
payload = b"FAKETTF:" + digest.hexdigest().encode("ascii") + b"\n"

os.makedirs(outdir, exist_ok=True)
final_path = os.path.join(outdir, filename + ".ttf")
options = json.loads(os.environ.get("HANDWRITE_TTF_OPTIONS", "{}"))
if options.get("overwrite"):
    tmp_path = os.path.join(
        outdir,
        "." + filename + "." + str(options.get("token") or "tmp") + ".ttf",
    )
    with open(tmp_path, "wb") as f:
        f.write(payload)
    os.replace(tmp_path, final_path)
else:
    while os.path.exists(final_path):
        base, _ext = os.path.splitext(final_path)
        final_path = base + " (1).ttf"
    with open(final_path, "wb") as f:
        f.write(payload)
