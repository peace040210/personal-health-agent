"""Project the public Google PHA summary onto fields used in the browser."""

import argparse
import csv
import json
from pathlib import Path


def export(source: Path, output: Path) -> None:
    with source.open(encoding="utf-8", newline="") as handle:
        rows = []
        for row in csv.DictReader(handle):
            rows.append({
                "date": row["datetime"][:10],
                "bedtime": row["bed_time"][:10] or None,
                "bedAt": row["bed_time"] or None,
                "wakeAt": row["wake_up_time"] or None,
                **{field: int(float(row[column])) if row[column] else None for field, column in (
                    ("steps", "steps"), ("sleep", "sleep_minutes"), ("deep", "deep_sleep_minutes"),
                )},
            })
    rows.sort(key=lambda row: row["date"])
    output.write_text(json.dumps({
        "last": rows[-1]["date"],
        "source": "Google PHA public synthetic sample (Apache-2.0)",
        "rows": rows,
    }, separators=(",", ":")) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    export(args.source, args.output)
