"""Turn the game's own collision shapes into a bundled table.

Mojang's ``reports/blocks.json`` lists every block state's id and properties but
not its collision boxes: those are computed by block code at runtime, so a
generated table has to approximate every solid block as a full cube.  For beds,
slabs, stairs, fences, panes, carpets and snow that approximation is wrong by up
to half a block, and a movement prediction measures the difference.

The shapes can be read out of the game itself.  ``artifacts/shapes/ShapeDump``
walks the real state registry inside the server jar and prints one line per
state::

    <state id>\t<namespace:path[prop=value,...]>\t<x1,y1,z1,x2,y2,z2>[;...]

This tool folds that dump into the compact table the library loads next to
``blocks-<release>.json.gz``::

    {"release": "26.3", "boxes": [[...], ...], "states": {"<id>": [box index]}}

Boxes are deduplicated because the overwhelming majority of states are the same
full cube, which keeps the file to a few hundred kilobytes.

Usage::

    python tools/gen_block_shapes.py artifacts/shapes/shapes.tsv \\
        --release 26.3 --output protobot/data/shapes-26.3.json.gz
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from pathlib import Path

DEFAULT_INPUT = Path("artifacts/shapes/shapes.tsv")
DEFAULT_OUTPUT = Path("protobot/data/shapes-26.3.json.gz")


def parse_box(text: str) -> tuple[float, float, float, float, float, float]:
    values = [float(part) for part in text.split(",")]
    if len(values) != 6:
        raise ValueError(f"expected six numbers in a box, got {text!r}")
    return (values[0], values[1], values[2], values[3], values[4], values[5])


def read_dump(path: Path) -> tuple[dict[str, list[list[float]]], int, int]:
    states: dict[str, list[list[float]]] = {}
    skipped = 0
    unknown = 0
    raw = path.read_bytes()
    # The dump usually comes out of a shell redirect, and PowerShell 5.1 writes
    # UTF-16LE with a BOM for ``>``.  Accept both encodings rather than making
    # the operator remember an encoding flag.
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        text = raw.decode("utf-16")
    else:
        text = raw.decode("utf-8-sig")
    for line in text.splitlines():
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) != 3:
            skipped += 1
            continue
        raw_id, _name, raw_boxes = parts
        try:
            state_id = int(raw_id)
        except ValueError:
            skipped += 1
            continue
        if raw_boxes.strip() == "?":
            # A shape that needs a live level; keep the generated fallback.
            unknown += 1
            continue
        boxes: list[list[float]] = []
        if raw_boxes.strip():
            for chunk in raw_boxes.split(";"):
                if not chunk:
                    continue
                boxes.append(list(parse_box(chunk)))
        states[str(state_id)] = boxes
    return states, skipped, unknown


def build_table(
    states: dict[str, list[list[float]]], release: str
) -> dict[str, object]:
    box_index: dict[tuple[float, ...], int] = {}
    boxes: list[list[float]] = []
    packed: dict[str, list[int]] = {}
    for state_id, state_boxes in states.items():
        indices: list[int] = []
        for box in state_boxes:
            key = tuple(box)
            index = box_index.get(key)
            if index is None:
                index = len(boxes)
                box_index[key] = index
                boxes.append(box)
            # A shape can legitimately use the same box twice (a torch's two
            # pieces), so the index is appended per occurrence instead of
            # deduplicating inside one state.
            indices.append(index)
        packed[state_id] = indices
    return {
        "release": release,
        "box_count": len(boxes),
        "state_count": len(packed),
        "boxes": boxes,
        "states": packed,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", nargs="?", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--release", default="26.3")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)

    if not args.input.exists():
        print(f"missing shape dump: {args.input}", file=sys.stderr)
        print(
            "run artifacts/shapes/ShapeDump against the server jar first",
            file=sys.stderr,
        )
        return 2

    states, skipped, unknown = read_dump(args.input)
    table = build_table(states, args.release)
    payload = json.dumps(table, separators=(",", ":"), sort_keys=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(args.output, "wt", encoding="utf-8") as stream:
        stream.write(payload + "\n")
    print(
        f"{args.output}: {table['state_count']} states, "
        f"{table['box_count']} distinct boxes, {len(payload) // 1024} KiB "
        f"(skipped {skipped}, unknown {unknown})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
