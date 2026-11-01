import os
import sys
import shutil
import argparse
import tempfile

from handwrite import SHEETtoPNG
from handwrite import PNGtoSVG
from handwrite import SVGtoTTF
from handwrite.build import build_full, ResumeBuilder
from handwrite.state import BuildStateError, BuildConflictError

RESUME_COMMAND = "resume"


def run(sheet, output_directory, characters_dir, config, metadata):
    SHEETtoPNG().convert(sheet, characters_dir, config)
    PNGtoSVG().convert(directory=characters_dir)
    SVGtoTTF().convert(characters_dir, output_directory, config, metadata)


def converters(
    sheet,
    output_directory,
    directory=None,
    config=None,
    metadata=None,
    force=False,
):
    if not directory:
        directory = tempfile.mkdtemp()
        isTempdir = True
    else:
        directory = os.path.abspath(directory)
        isTempdir = False

    if config is None:
        config = os.path.join(
            os.path.dirname(os.path.realpath(__file__)), "default.json"
        )
    if os.path.isdir(config):
        raise IsADirectoryError("Config parameter should not be a directory.")

    if os.path.isdir(sheet):
        raise IsADirectoryError("Sheet parameter should not be a directory.")
    else:
        build_full(
            sheet,
            output_directory,
            directory,
            config,
            metadata,
            persistent=not isTempdir,
            force=force,
        )

    if isTempdir:
        shutil.rmtree(directory)


def _print_resume_report(report):
    def fmt(cps):
        return ", ".join("{} ({!r})".format(cp, chr(cp)) for cp in cps) or "none"

    print("Crops replaced from sheet:   " + fmt(report["crops_replaced"]))
    print("Manual PNG edits adopted:    " + fmt(report["png_edits"]))
    print("Missing PNGs restored:       " + fmt(report["pngs_restored"]))
    print("BMPs rebuilt:                " + fmt(report["bmps_rebuilt"]))
    print("Manual SVG edits kept:       " + fmt(report["svg_edits"]))
    print("SVGs rebuilt from PNGs:      " + fmt(report["svgs_rebuilt"]))
    if report["font_rebuilt"]:
        print("Font regenerated:            {}".format(report["output"]))
    else:
        print("Font up to date; not rebuilt.")


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]

    if argv and argv[0] == RESUME_COMMAND:
        _main_resume(argv[1:])
        return

    parser = argparse.ArgumentParser()
    parser.add_argument("input_path", help="Path to sample sheet")
    parser.add_argument("output_directory", help="Directory Path to save font output")
    parser.add_argument(
        "--directory",
        help="Generate additional files to this path (Temp by default). "
        "The directory keeps a build state for 'handwrite resume'.",
        default=None,
    )
    parser.add_argument("--config", help="Use custom configuration file", default=None)
    parser.add_argument("--filename", help="Font File name", default=None)
    parser.add_argument("--family", help="Font Family name", default=None)
    parser.add_argument("--style", help="Font Style name", default=None)
    parser.add_argument(
        "--force",
        action="store_true",
        help="With --directory: discard hand-edited PNG/SVG files when a full "
        "rebuild would overwrite them (edits are backed up first)",
    )

    args = parser.parse_args(argv)
    metadata = {"filename": args.filename, "family": args.family, "style": args.style}
    try:
        converters(
            args.input_path,
            args.output_directory,
            args.directory,
            args.config,
            metadata,
            force=args.force,
        )
    except BuildConflictError as error:
        sys.stderr.write("\n" + str(error) + "\n")
        sys.exit(2)
    except BuildStateError as error:
        sys.stderr.write(str(error) + "\n")
        sys.exit(2)


def _main_resume(argv):
    parser = argparse.ArgumentParser(
        prog="handwrite resume",
        description="Continue a build in a retained --directory: re-crop and "
        "re-trace only the codepoints whose sources or hand-edited PNG/SVG "
        "files changed, then regenerate the font when needed.",
    )
    parser.add_argument(
        "--directory",
        required=True,
        help="Retained characters directory from a previous run with --directory",
    )
    parser.add_argument(
        "--output-directory",
        default=None,
        help="Directory for the font output (defaults to the recorded one)",
    )
    parser.add_argument(
        "--sheet",
        default=None,
        help="Current sample sheet path (defaults to the recorded one)",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Current config file path (defaults to the recorded one)",
    )
    parser.add_argument("--filename", help="Font File name", default=None)
    parser.add_argument("--family", help="Font Family name", default=None)
    parser.add_argument("--style", help="Font Style name", default=None)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Accept the new source crop/config and regenerate derivatives even "
        "when this overwrites hand-edited PNG/SVG files (edits are backed up "
        "under the directory's .handwrite/backup/ folder first)",
    )

    args = parser.parse_args(argv)
    metadata = {"filename": args.filename, "family": args.family, "style": args.style}
    try:
        report = ResumeBuilder(
            os.path.abspath(args.directory),
            output_directory=(
                os.path.abspath(args.output_directory)
                if args.output_directory
                else None
            ),
            sheet=os.path.abspath(args.sheet) if args.sheet else None,
            config=os.path.abspath(args.config) if args.config else None,
            metadata=metadata,
            force=args.force,
        ).run()
    except BuildConflictError as error:
        sys.stderr.write(
            "\nBuild conflicts detected; no files were changed. Resolve them or "
            "rerun with --force to proceed (manual edits are backed up first):\n"
        )
        for conflict in error.conflicts:
            sys.stderr.write("  - " + conflict + "\n")
        sys.exit(2)
    except BuildStateError as error:
        sys.stderr.write(str(error) + "\n")
        sys.exit(2)

    _print_resume_report(report)


if __name__ == "__main__":
    main()
