"""Command-line entry point: ``documentai`` / ``python -m documentai``."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import __version__
from .converters import DEFAULT_TIMEOUT, find_soffice
from .formats import supported_extensions
from .parsers import OUTPUT_FORMATS
from .pipeline import DocumentPipeline, collect_inputs, write_manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="documentai",
        description="Convert documents to PDF and extract text, Markdown and JSON.",
        epilog="supported inputs: " + " ".join(supported_extensions()),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("inputs", nargs="+", metavar="INPUT",
                        help="files or directories to process")
    parser.add_argument("-o", "--output", default="output", metavar="DIR",
                        help="directory for the extracted files")
    parser.add_argument("-f", "--formats", nargs="+", default=["text", "markdown", "json"],
                        metavar="FMT",
                        help=f"any of: {', '.join(OUTPUT_FORMATS)} (aliases: txt, md)")
    parser.add_argument("-r", "--recursive", action="store_true",
                        help="descend into subdirectories of directory inputs")
    parser.add_argument("--keep-pdf", action="store_true",
                        help="keep the intermediate PDF under OUTPUT/pdf/")
    parser.add_argument("--images", action="store_true",
                        help="write embedded images under OUTPUT/images/ and link them "
                             "from the Markdown")
    parser.add_argument("--no-spans", action="store_true",
                        help="drop the per-span font detail from the JSON output")
    parser.add_argument("--manifest", nargs="?", const="manifest.json", metavar="PATH",
                        help="write a JSON run summary (default name: manifest.json, "
                             "relative to OUTPUT)")
    parser.add_argument("--soffice", metavar="PATH",
                        help="path to the LibreOffice executable (Office inputs only)")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, metavar="SEC",
                        help="per-document LibreOffice conversion timeout")
    parser.add_argument("--no-overwrite", action="store_true",
                        help="fail instead of replacing existing output files")
    parser.add_argument("-q", "--quiet", action="store_true", help="only report errors")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every stage")
    parser.add_argument("--version", action="version", version=f"documentai {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    level = logging.WARNING if args.quiet else (logging.INFO if args.verbose else logging.WARNING)
    logging.basicConfig(level=level, format="%(levelname)s %(message)s")

    inputs = collect_inputs(args.inputs, recursive=args.recursive)
    if not inputs:
        print("no supported input files found", file=sys.stderr)
        return 2

    try:
        pipeline = DocumentPipeline(
            args.output,
            formats=args.formats,
            keep_pdf=args.keep_pdf,
            extract_images=args.images,
            spans=not args.no_spans,
            soffice=args.soffice,
            timeout=args.timeout,
            overwrite=not args.no_overwrite,
        )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    results = pipeline.run_many(inputs)

    for result in results:
        if result.ok:
            if not args.quiet:
                produced = ", ".join(p.name for p in result.outputs.values())
                note = "" if result.converted else " (already PDF)"
                print(f"OK   {result.source.name}{note} -> {produced} "
                      f"[{result.page_count}p, {result.duration:.1f}s]")
        else:
            print(f"FAIL {result.source.name}: {result.error}", file=sys.stderr)

    if args.manifest:
        manifest = Path(args.manifest)
        if not manifest.is_absolute():
            manifest = Path(args.output) / manifest
        manifest = write_manifest(results, manifest)
        if not args.quiet:
            print(f"manifest: {manifest}")

    failed = sum(1 for r in results if not r.ok)
    if not args.quiet:
        print(f"\n{len(results) - failed}/{len(results)} document(s) processed "
              f"-> {Path(args.output).resolve()}")
    if failed and any("LibreOffice" in r.error for r in results) and find_soffice() is None:
        print("hint: install LibreOffice to convert Office formats", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
