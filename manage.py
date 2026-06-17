#!/usr/bin/env python3
"""Music World management CLI.

Bulk operations that are awkward through the web UI. Uses only the stdlib and
the app's own data layer (no Flask needed), so it runs anywhere the app does.

  # Import genres from a JSON file (array, or {"genres": [...]}):
  python3 manage.py import-genres genres.json

  # Read the same JSON from stdin:
  cat genres.json | python3 manage.py import-genres -

  # Dump current genres as JSON (a ready-made import template):
  python3 manage.py export-genres            # to stdout
  python3 manage.py export-genres -o out.json

  # List genre names:
  python3 manage.py list-genres

Genre JSON object shape (all fields optional except name):
  {
    "name": "Shoegaze",
    "description": "Dense, guitar-washed dream rock.",
    "descriptors": ["hazy", "wall of sound", "reverb-drenched"],
    "typical_instruments": ["distorted guitar", "bass", "drums"],
    "tempo_range": "100-140 bpm",
    "common_regions": ["UK"],
    "base_style_tags": ["shoegaze", "wall of guitar", "ethereal vocals"]
  }
List fields also accept a comma/semicolon-separated string.
"""

import argparse
import json
import sys

import database


def _load_text(path):
    if path in ("-", "/dev/stdin", None):
        return sys.stdin.read()
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def cmd_import_genres(args):
    database.init_db()
    try:
        items = database.parse_genres_payload(_load_text(args.file))
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    res = database.import_genres(items)
    print(f"genres: +{res['added']} added, {res['updated']} updated, "
          f"{res['skipped']} skipped")
    for warning in res["errors"]:
        print(f"  warn: {warning}", file=sys.stderr)
    return 0


def cmd_export_genres(args):
    database.init_db()
    text = json.dumps(database.export_genres(), indent=2, ensure_ascii=False)
    if args.output and args.output != "-":
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print(f"wrote {len(database.export_genres())} genres to {args.output}")
    else:
        print(text)
    return 0


def cmd_list_genres(args):
    database.init_db()
    for r in database.query("SELECT name FROM genre ORDER BY name"):
        print(r["name"])
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="manage.py",
                                     description="Music World management CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_imp = sub.add_parser("import-genres",
                           help="bulk import/update genres from a JSON file (or - for stdin)")
    p_imp.add_argument("file", help="path to a JSON file, or - for stdin")
    p_imp.set_defaults(func=cmd_import_genres)

    p_exp = sub.add_parser("export-genres", help="dump all genres as JSON")
    p_exp.add_argument("-o", "--output", help="write to this file instead of stdout")
    p_exp.set_defaults(func=cmd_export_genres)

    p_list = sub.add_parser("list-genres", help="print genre names, one per line")
    p_list.set_defaults(func=cmd_list_genres)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
