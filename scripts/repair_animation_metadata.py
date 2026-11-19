#!/usr/bin/env python3
"""Repair existing RangerBook sprite metadata using the authoritative .plist files.

This deliberately does not reinterpret SAM animation, timings, effects or transforms.
Only atlas rectangles and missing sprite aliases are repaired in place.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
from pathlib import Path
import plistlib
import re
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
NUMBERS = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)")


def pixel_numbers(value: Any, count: int) -> list[int] | None:
    if isinstance(value, (tuple, list)):
        raw = value
    else:
        raw = NUMBERS.findall(str(value or ""))
    if len(raw) < count:
        return None
    try:
        numbers = [float(number) for number in raw[:count]]
        if not all(math.isfinite(number) for number in numbers):
            return None
        # Match Math.round for pixel coordinates; do not split 98.0 into [98, 0].
        return [math.floor(number + 0.5) for number in numbers]
    except (TypeError, ValueError, OverflowError):
        return None


def repair_part(part: dict[str, Any], frames: dict[str, Any]) -> tuple[int, int]:
    sprites = part.get("sprites")
    if not isinstance(sprites, dict):
        return 0, 0
    fixed_rects = 0
    fixed_aliases = 0
    image_names = {
        image.get("name") for image in part.get("images", [])
        if isinstance(image, dict) and isinstance(image.get("name"), str)
    }

    # Fix the canonical atlas entries before mapping aliases to them.
    for name, info in frames.items():
        if not isinstance(info, dict):
            continue
        rect = pixel_numbers(info.get("textureRect", info.get("frame", "")), 4)
        if rect is None or rect[2] <= 0 or rect[3] <= 0:
            continue
        existing = sprites.get(name)
        if existing is None and name in image_names:
            existing = {}
            sprites[name] = existing
        if not isinstance(existing, dict):
            continue
        if existing.get("rect") != rect:
            existing["rect"] = rect
            fixed_rects += 1

    # Explicit plist frames always win over aliases, regardless of iteration order.
    for name, info in frames.items():
        if not isinstance(info, dict):
            continue
        canonical = sprites.get(name)
        if not isinstance(canonical, dict):
            continue
        rect = canonical.get("rect", [])
        if len(rect) != 4 or rect[2] <= 0 or rect[3] <= 0:
            continue
        aliases = info.get("aliases", [])
        if not isinstance(aliases, (list, tuple)):
            continue
        for alias in aliases:
            if (
                not isinstance(alias, str)
                or not alias
                or alias in frames
                or alias in sprites
            ):
                continue
            sprites[alias] = copy.deepcopy(canonical)
            sprites[alias]["aliases"] = []
            fixed_aliases += 1
    return fixed_rects, fixed_aliases


def repair_metadata(metadata: dict[str, Any], root: Path) -> tuple[int, int]:
    rects = aliases = 0
    root = root.resolve()
    for part in metadata.get("parts", {}).values():
        if not isinstance(part, dict):
            continue
        raw_path = part.get("plist")
        if not isinstance(raw_path, str):
            continue
        plist_path = (root / raw_path).resolve()
        if not plist_path.is_relative_to(root) or plist_path.suffix != ".plist" or not plist_path.is_file():
            continue
        with plist_path.open("rb") as file:
            frames = (plistlib.load(file) or {}).get("frames", {})
        if not isinstance(frames, dict):
            continue
        fixed_rects, fixed_aliases = repair_part(part, frames)
        rects += fixed_rects
        aliases += fixed_aliases
    return rects, aliases


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--unit", action="append", default=[], help="Limit to unit ID (repeatable)")
    args = parser.parse_args()
    root = args.root.resolve()
    output = root / "animation_meta"
    files = (
        [output / f"{unit}.json" for unit in args.unit]
        if args.unit else sorted(output.glob("u*.json"))
    )
    updated = rects = aliases = 0
    errors: list[str] = []
    for path in files:
        if not path.is_file():
            errors.append(f"Missing metadata: {path.name}")
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            fixed_rects, fixed_aliases = repair_metadata(data, root)
            if fixed_rects or fixed_aliases:
                if not args.dry_run:
                    content = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                    path.write_text(content, encoding="utf-8")
                print(f"{path.stem}: rects={fixed_rects} aliases={fixed_aliases}")
                updated += 1
                rects += fixed_rects
                aliases += fixed_aliases
        except (OSError, ValueError, TypeError, plistlib.InvalidFileException) as exc:
            errors.append(f"{path.name}: {exc}")
    print(f"files_checked={len(files)} files_changed={updated} fixed_rects={rects} fixed_aliases={aliases} dry_run={args.dry_run} errors={len(errors)}")
    for error in errors[:20]:
        print("ERROR:", error)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
