"""Dry-run CLI. No GPIO, radio, USB discovery or HTTP side effects."""
import argparse
import json
from pathlib import Path
from transfer import MAX_BYTES, decode, encode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path, help="explicit local .txt input")
    parser.add_argument("--dry-run", action="store_true", required=True,
                        help="required: real radio is not configured")
    args = parser.parse_args()
    if args.file.suffix.lower() != ".txt":
        parser.error("input must be a .txt file")
    try:
        with args.file.open("rb") as source:
            data = source.read(MAX_BYTES + 1)
        frames = encode(data)
        if decode(frames) != data:
            raise ValueError("local reconstruction mismatch")
    except (OSError, ValueError) as error:
        parser.exit(1, f"Failed: {error}\n")
    print(json.dumps({"mode": "host-dry-run", "bytes": len(data),
                      "data_frames": len(frames) - 1,
                      "radio_transmitted": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
