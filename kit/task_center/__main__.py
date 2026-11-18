"""UTF-8 JSON CLI; no model, plugin, network, or Cindy session is launched here."""
import argparse
import json
import sys
from pathlib import Path

from .core import Center

MAX_INPUT_BYTES = 1024 * 1024


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        parser = Parser(description="Local task center (trusted single-user prototype)")
        parser.add_argument("--db", required=True, help="Absolute path to the one authoritative database")
        parser.add_argument("operation")
        parser.add_argument("--request-id", help="Unique command id; reuse only when retrying identical parameters")
        parser.add_argument("--input", default="-", help="UTF-8 JSON file, or - for standard input")
        args = parser.parse_args()
        if not Path(args.db).is_absolute():
            raise ValueError("--db must be an absolute path")
        if args.input == "-":
            raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        else:
            with Path(args.input).open("rb") as source:
                raw = source.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            raise ValueError("JSON input exceeds 1 MiB")
        payload = json.loads(raw.decode("utf-8-sig")) if raw.strip() else {}
        response = Center(args.db).execute(args.operation, payload, args.request_id)
    except (ValueError, OSError, UnicodeError, RecursionError):
        response = {"ok": False, "error": {"code": "INVALID_INPUT", "message": "Invalid arguments, JSON input, or inaccessible input file"}}
    print(json.dumps(response, ensure_ascii=False, allow_nan=False))
    return 0 if response["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
