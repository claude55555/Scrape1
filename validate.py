#!/usr/bin/env python3
"""Validate meeting JSON files against the Open Civic Agenda schema.

Usage:
    python validate.py examples/millfield-city-council-2025-03-15.json
    python validate.py output/           # validate all .json files in a directory
    python validate.py --schema schema/v0.1/meeting.json examples/*.json
"""

import argparse
import json
import sys
from pathlib import Path

try:
    import jsonschema
except ImportError:
    print("Install jsonschema first:  pip install jsonschema", file=sys.stderr)
    sys.exit(1)

DEFAULT_SCHEMA = Path(__file__).resolve().parent / "schema" / "v0.1" / "meeting.json"


def validate_file(filepath: Path, schema: dict) -> list[str]:
    data = json.loads(filepath.read_text())
    validator = jsonschema.Draft202012Validator(schema)
    return [f"  {e.json_path}: {e.message}" for e in validator.iter_errors(data)]


def main():
    parser = argparse.ArgumentParser(description="Validate meeting JSON files.")
    parser.add_argument("paths", nargs="+", help="JSON files or directories to validate")
    parser.add_argument("--schema", default=str(DEFAULT_SCHEMA), help="Path to the JSON Schema file")
    args = parser.parse_args()

    schema = json.loads(Path(args.schema).read_text())

    files = []
    for p in args.paths:
        path = Path(p)
        if path.is_dir():
            files.extend(sorted(path.glob("**/*.json")))
        else:
            files.append(path)

    if not files:
        print("No files found.")
        sys.exit(1)

    total_errors = 0
    for f in files:
        errors = validate_file(f, schema)
        if errors:
            print(f"FAIL  {f}")
            for e in errors:
                print(e)
            total_errors += len(errors)
        else:
            print(f"OK    {f}")

    print(f"\n{len(files)} file(s) checked, {total_errors} error(s).")
    sys.exit(1 if total_errors else 0)


if __name__ == "__main__":
    main()
