#!/usr/bin/env python3
"""Build browser-readable LINE Rangers animation metadata in rangerbook_res.

Expected input examples:
  unit/<unit_id>/<unit_id>-body.sam|plist|png
  unit/<unit_id>/<unit_id>-bul.sam|plist|png
  unit/<unit_id>/<unit_id>-bul2.sam|plist|png
  unit/<unit_id>/<unit_id>-bul3.sam|plist|png

Also supports legacy root unit folders:
  <unit_id>/<unit_id>-body.sam|plist|png

Output:
  animation_meta/index.json
  animation_meta/<unit_id>.json

Compatibility notes:
  - Input roots and output paths are unchanged.
  - Existing fields used by the browser viewer are preserved:
    animations, segments, sprites, startup, parts, and index.json.
  - Schema 2 adds the original SAM timeline, repeated label occurrences,
    virtual clip metadata, atlas metadata, and parser validation fields.
"""

from __future__ import annotations

import json
import math
import plistlib
import re
import struct
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT_ROOT = ROOT / "animation_meta"

# Kept as the preferred ordering for backward-compatible standard parts.
# Additional <prefix>-<part>.sam/plist/png triplets are discovered automatically.
PARTS = ("body", "bul", "bul2", "bul3")

STARTUP_SEGMENTS = {
    "normal_attack": ("attack_ready",),
    "skill_1": ("s_attack_ready", "s_action_attack_1"),
    "skill_2": ("s2_attack_ready",),
}

# These are convenience clips for the website. They are not native SAM labels.
COMBO_SEGMENTS = {
    "attack_all": ("attack_ready", "attack"),
    "s_attack_all": ("s_attack_ready", "s_attack"),
    "s_action_attack_all": ("s_action_attack_1", "s_action_attack_2", "s_action_attack_3"),
    "s2_attack_all": ("s2_attack_ready", "s2_attack"),
}

IGNORED_ROOT_DIRS = {
    ".git",
    ".github",
    "animation_meta",
    "scripts",
    "ability_icon",
    "gear_icon",
    "skill_icon",
}

SCHEMA_VERSION = 2


class SAMParser:
    FF_REMOVES = 0x01
    FF_ADDS = 0x02
    FF_MOVES = 0x04
    FF_FRAME_NAME = 0x08

    MF_ROTATE = 0x4000
    MF_COLOR = 0x2000
    MF_MATRIX = 0x1000
    MF_LONGCOORDS = 0x0800

    TWIPS = 20.0
    Q16 = 65536.0
    MAGIC = 0x2E53414D
    SUPPORTED_VERSION = 1

    def __init__(self, path: Path):
        self.path = path
        self.raw = path.read_bytes()
        self.pos = 0
        self.version = 0
        self.anim_rate = 24
        self.x = 0.0
        self.y = 0.0
        self.canvas_w = 0.0
        self.canvas_h = 0.0
        self.images: list[dict[str, Any]] = []

        # animations intentionally keeps the historical behavior of merging
        # repeated labels, because the current viewer reads this mapping.
        self.animations: dict[str, list[list[list[Any]]]] = {}
        self.anim_names: list[str] = []

        # frames and frame_labels preserve the exact original SAM order.
        self.frames: list[list[list[Any]]] = []
        self.frame_labels: list[dict[str, Any]] = []
        self.all_segments: list[dict[str, Any]] = []

        self._parse()

    def _require(self, size: int, label: str) -> None:
        if size < 0 or self.pos + size > len(self.raw):
            raise ValueError(
                f"Unexpected end of SAM while reading {label}: "
                f"path={self.path}, position={self.pos}, "
                f"need={size}, size={len(self.raw)}"
            )

    def _u8(self) -> int:
        self._require(1, "u8")
        value = self.raw[self.pos]
        self.pos += 1
        return value

    def _u16(self) -> int:
        self._require(2, "u16")
        value = struct.unpack_from("<H", self.raw, self.pos)[0]
        self.pos += 2
        return value

    def _i16(self) -> int:
        self._require(2, "i16")
        value = struct.unpack_from("<h", self.raw, self.pos)[0]
        self.pos += 2
        return value

    def _i32(self) -> int:
        self._require(4, "i32")
        value = struct.unpack_from("<i", self.raw, self.pos)[0]
        self.pos += 4
        return value

    def _str(self) -> str:
        length = self._u16()
        self._require(length, "string")
        value = self.raw[self.pos:self.pos + length].decode("ascii", errors="replace")
        self.pos += length
        return value

    @staticmethod
    def _round_matrix(values: tuple[float, float, float, float, float, float]) -> list[float]:
        return [round(value, 6) for value in values]

    def _append_frame_label(self, name: str) -> None:
        frame_index = len(self.frames)
        occurrence = 1 + sum(1 for item in self.frame_labels if item["name"] == name)
        self.frame_labels.append({
            "name": name,
            "occurrence": occurrence,
            "frame": frame_index,
            "seconds": round(frame_index / max(1, self.anim_rate), 6),
        })

    def _build_original_segments(self) -> list[dict[str, Any]]:
        segments: list[dict[str, Any]] = []

        if not self.frames:
            return segments

        if not self.frame_labels:
            return [{
                "name": "_intro",
                "occurrence": 1,
                "start": 0,
                "end": len(self.frames),
                "frame_count": len(self.frames),
                "start_seconds": 0.0,
                "end_seconds": round(len(self.frames) / max(1, self.anim_rate), 6),
            }]

        first_label_frame = self.frame_labels[0]["frame"]
        if first_label_frame > 0:
            segments.append({
                "name": "_intro",
                "occurrence": 1,
                "start": 0,
                "end": first_label_frame,
                "frame_count": first_label_frame,
                "start_seconds": 0.0,
                "end_seconds": round(first_label_frame / max(1, self.anim_rate), 6),
            })

        for index, label in enumerate(self.frame_labels):
            start = int(label["frame"])
            end = (
                int(self.frame_labels[index + 1]["frame"])
                if index + 1 < len(self.frame_labels)
                else len(self.frames)
            )
            segments.append({
                "name": label["name"],
                "occurrence": label["occurrence"],
                "start": start,
                "end": end,
                "frame_count": max(0, end - start),
                "start_seconds": round(start / max(1, self.anim_rate), 6),
                "end_seconds": round(end / max(1, self.anim_rate), 6),
            })

        return segments

    def _parse(self) -> None:
        magic = self._i32()
        if magic != self.MAGIC:
            raise ValueError(f"Bad SAM magic: {self.path}")

        self.version = self._i32()
        if self.version != self.SUPPORTED_VERSION:
            raise ValueError(f"Unsupported SAM version {self.version}: {self.path}")

        self.anim_rate = self._u8()
        self.x = self._i32() / self.TWIPS
        self.y = self._i32() / self.TWIPS
        self.canvas_w = self._i32() / self.TWIPS
        self.canvas_h = self._i32() / self.TWIPS

        image_count = self._i16()
        if image_count < 0:
            raise ValueError(f"Negative SAM image count {image_count}: {self.path}")

        for _ in range(image_count):
            name = self._str()
            width = self._i16()
            height = self._i16()
            matrix = (
                self._i32() / (self.Q16 * self.TWIPS),
                self._i32() / (self.Q16 * self.TWIPS),
                self._i32() / (self.Q16 * self.TWIPS),
                self._i32() / (self.Q16 * self.TWIPS),
                self._i16() / self.TWIPS,
                self._i16() / self.TWIPS,
            )
            self.images.append({
                "name": name,
                "w": width,
                "h": height,
                "m": self._round_matrix(matrix),
            })

        objects: dict[int, dict[str, Any]] = {}
        depth_memory: dict[int, dict[str, Any]] = {}
        current_anim = "_intro"
        self.animations[current_anim] = []
        self.anim_names.append(current_anim)

        frame_count = self._i16()
        if frame_count < 0:
            raise ValueError(f"Negative SAM frame count {frame_count}: {self.path}")

        for _ in range(frame_count):
            flags = self._u8()

            if flags & self.FF_REMOVES:
                for _ in range(self._u8()):
                    obj_id = self._i16()
                    if obj_id in objects:
                        depth_memory[obj_id] = objects[obj_id]
                    objects.pop(obj_id, None)

            if flags & self.FF_ADDS:
                for _ in range(self._u8()):
                    obj_num = self._i16() & 0x07FF
                    res_num = self._u8()
                    old = depth_memory.get(obj_num)
                    objects[obj_num] = {
                        "res": res_num,
                        "m": old["m"] if old else (1.0, 0.0, 0.0, 1.0, 0.0, 0.0),
                        "color": old["color"] if old else (255, 255, 255, 255),
                    }

            if flags & self.FF_MOVES:
                for _ in range(self._u8()):
                    foan = self._u16()
                    obj_num = foan & 0x07FF
                    move_flags = foan & 0xF800
                    obj = objects.setdefault(obj_num, {
                        "res": 0,
                        "m": (1.0, 0.0, 0.0, 1.0, 0.0, 0.0),
                        "color": (255, 255, 255, 255),
                    })

                    m00, m01, m10, m11 = 1.0, 0.0, 0.0, 1.0
                    if move_flags & self.MF_MATRIX:
                        m00 = self._i32() / self.Q16
                        m01 = self._i32() / self.Q16
                        m10 = self._i32() / self.Q16
                        m11 = self._i32() / self.Q16
                    elif move_flags & self.MF_ROTATE:
                        rot = self._i16() / 1000.0
                        cos_v, sin_v = math.cos(rot), math.sin(rot)
                        m00, m01, m10, m11 = cos_v, -sin_v, sin_v, cos_v

                    if move_flags & self.MF_LONGCOORDS:
                        m02 = self._i32() / self.TWIPS
                        m12 = self._i32() / self.TWIPS
                    else:
                        m02 = self._i16() / self.TWIPS
                        m12 = self._i16() / self.TWIPS

                    obj["m"] = (m00, m01, m10, m11, m02, m12)
                    if move_flags & self.MF_COLOR:
                        obj["color"] = (self._u8(), self._u8(), self._u8(), self._u8())
                    depth_memory[obj_num] = obj

            if flags & self.FF_FRAME_NAME:
                name = self._str()
                self._append_frame_label(name)
                if name not in self.animations:
                    self.animations[name] = []
                    self.anim_names.append(name)
                current_anim = name

            snapshot: list[list[Any]] = []
            for obj_num in sorted(objects):
                obj = objects[obj_num]
                snapshot.append([
                    obj_num,
                    obj["res"],
                    self._round_matrix(obj["m"]),
                    list(obj["color"]),
                ])

            self.animations[current_anim].append(snapshot)
            self.frames.append(snapshot)

        if self.pos != len(self.raw):
            raise ValueError(
                f"Unparsed SAM bytes: path={self.path}, position={self.pos}, "
                f"size={len(self.raw)}, remaining={len(self.raw) - self.pos}"
            )

        if not self.animations.get("_intro"):
            self.animations.pop("_intro", None)
            if "_intro" in self.anim_names:
                self.anim_names.remove("_intro")

        # Preserve exact original order. Do not rebuild _all from the animation
        # dictionary, because repeated labels would otherwise be reordered.
        self.animations["_all"] = list(self.frames)
        if "_all" not in self.anim_names:
            self.anim_names.insert(0, "_all")

        self.all_segments = self._build_original_segments()

    def to_json(self) -> dict[str, Any]:
        animations = dict(self.animations)
        virtual_clips: dict[str, Any] = {}

        for combo_name, segment_names in COMBO_SEGMENTS.items():
            combo_frames: list[list[list[Any]]] = []
            included_segments: list[str] = []
            for segment_name in segment_names:
                segment_frames = self.animations.get(segment_name, [])
                if segment_frames:
                    combo_frames.extend(segment_frames)
                    included_segments.append(segment_name)

            if combo_frames:
                # Kept in animations for compatibility with the existing viewer.
                animations[combo_name] = combo_frames
                virtual_clips[combo_name] = {
                    "native": False,
                    "segments": included_segments,
                    "frame_count": len(combo_frames),
                    "seconds": round(len(combo_frames) / max(1, self.anim_rate), 6),
                }

        total_frames = len(self.frames)
        return {
            "schema_version": SCHEMA_VERSION,
            "sam_format": {
                "version": self.version,
                "file_bytes": len(self.raw),
                "parsed_bytes": self.pos,
                "complete": self.pos == len(self.raw),
            },
            "anim_rate": self.anim_rate,
            "canvas": {
                "x": round(self.x, 3),
                "y": round(self.y, 3),
                "w": round(self.canvas_w, 3),
                "h": round(self.canvas_h, 3),
            },
            "images": self.images,
            # Existing field preserved; entries now reflect original occurrences.
            "segments": self.all_segments,
            "timeline": {
                "frame_count": total_frames,
                "seconds": round(total_frames / max(1, self.anim_rate), 6),
                "labels": self.frame_labels,
                "segments": self.all_segments,
            },
            "virtual_clips": virtual_clips,
            "animations": {
                name: {
                    "frame_count": len(frames),
                    "seconds": round(len(frames) / max(1, self.anim_rate), 6),
                    "frames": frames,
                }
                for name, frames in animations.items()
                if frames
            },
        }


def stable_json(data: Any, *, pretty: bool = False) -> str:
    if pretty:
        return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def write_if_changed(path: Path, content: str) -> bool:
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def plist_numbers(value: Any, expected: int) -> list[int]:
    """Read integer coordinates from plist string/list/tuple forms."""
    if isinstance(value, (list, tuple)):
        numbers = [int(item) for item in value]
    else:
        numbers = [int(item) for item in re.findall(r"-?\d+", str(value or ""))]
    return numbers[:expected] if len(numbers) >= expected else [0] * expected


def plist_rect(value: Any) -> list[int]:
    # Kept as a named helper for compatibility with earlier versions/tests.
    return plist_numbers(value, 4)


def load_plist(path: Path) -> dict[str, Any]:
    with path.open("rb") as file:
        data = plistlib.load(file)

    metadata = data.get("metadata", {}) or {}
    frames: dict[str, Any] = {}

    for name, raw_info in (data.get("frames", {}) or {}).items():
        info = raw_info or {}
        frames[name] = {
            # Existing keys preserved.
            "rect": plist_rect(info.get("textureRect", info.get("frame", ""))),
            "rotated": bool(info.get("textureRotated", info.get("rotated", False))),
            # Additional TexturePacker information for exact sprite placement.
            "offset": plist_numbers(info.get("spriteOffset", info.get("offset", "")), 2),
            "sprite_size": plist_numbers(info.get("spriteSize", ""), 2),
            "source_size": plist_numbers(
                info.get("spriteSourceSize", info.get("sourceSize", "")),
                2,
            ),
            "aliases": list(info.get("aliases", []) or []),
        }

    return {
        "frames": frames,
        "metadata": {
            "format": metadata.get("format"),
            "size": plist_numbers(metadata.get("size", ""), 2),
            "premultiply_alpha": metadata.get("premultiplyAlpha"),
            "texture_file_name": metadata.get("textureFileName"),
            "real_texture_file_name": metadata.get("realTextureFileName"),
        },
    }


def root_relative(path: Path) -> str:
    return str(path.relative_to(ROOT)).replace("\\", "/")


def discover_parts(unit_dir: Path, prefix: str) -> list[str]:
    """Discover all complete SAM/plist/png parts without changing input roots."""
    discovered: set[str] = set()
    prefix_with_dash = f"{prefix}-"

    for sam_path in unit_dir.glob(f"{prefix}-*.sam"):
        stem = sam_path.stem
        if not stem.startswith(prefix_with_dash):
            continue

        part = stem[len(prefix_with_dash):]
        if not part:
            continue

        plist_path = unit_dir / f"{stem}.plist"
        png_path = unit_dir / f"{stem}.png"
        if plist_path.exists() and png_path.exists():
            discovered.add(part)

    priority = {name: index for index, name in enumerate(PARTS)}
    return sorted(discovered, key=lambda name: (priority.get(name, len(priority)), name))


def build_part(unit_dir: Path, prefix: str, part: str) -> dict[str, Any] | None:
    stem = f"{prefix}-{part}"
    sam_path = unit_dir / f"{stem}.sam"
    plist_path = unit_dir / f"{stem}.plist"
    png_path = unit_dir / f"{stem}.png"
    if not (sam_path.exists() and plist_path.exists() and png_path.exists()):
        return None

    parser = SAMParser(sam_path)
    atlas = load_plist(plist_path)
    return {
        "sam": root_relative(sam_path),
        "plist": root_relative(plist_path),
        "png": root_relative(png_path),
        # Existing mapping shape preserved for the current JavaScript renderer.
        "sprites": atlas["frames"],
        "atlas": atlas["metadata"],
        **parser.to_json(),
    }


def build_unit(unit_dir: Path) -> dict[str, Any] | None:
    body_candidates = sorted(unit_dir.glob("*-body.sam"))
    if not body_candidates:
        return None
    prefix = body_candidates[0].name[:-len("-body.sam")]

    parts: dict[str, Any] = {}
    for part in discover_parts(unit_dir, prefix):
        built = build_part(unit_dir, prefix, part)
        if built:
            parts[part] = built

    body = parts.get("body")
    if not body:
        return None

    # Preserved for backward compatibility. "seconds" is the duration of the
    # selected startup segment, not a guaranteed projectile spawn timestamp.
    startup: dict[str, Any] = {}
    for label, segments in STARTUP_SEGMENTS.items():
        picked_segment = segments[0]
        frame_count = 0
        for segment in segments:
            candidate_count = body["animations"].get(segment, {}).get("frame_count", 0)
            if candidate_count:
                picked_segment = segment
                frame_count = candidate_count
                break
        startup[label] = {
            "segment": picked_segment,
            "frames": frame_count,
            "seconds": round(frame_count / max(1, body["anim_rate"]), 4),
            "meaning": "segment_duration",
            "projectile_spawn_time": False,
        }

    thumb = unit_dir / f"{prefix}-thum.png"
    return {
        "schema_version": SCHEMA_VERSION,
        "unit_id": unit_dir.name,
        "prefix": prefix,
        "resource_dir": root_relative(unit_dir),
        "thumbnail": root_relative(thumb) if thumb.exists() else "",
        "startup": startup,
        "parts": parts,
    }


def iter_unit_dirs() -> list[Path]:
    candidates: list[Path] = []
    for root in (ROOT / "unit", ROOT):
        if not root.exists():
            continue
        for path in sorted(item for item in root.iterdir() if item.is_dir()):
            if root == ROOT and path.name in IGNORED_ROOT_DIRS:
                continue
            if list(path.glob("*-body.sam")):
                candidates.append(path)
    return sorted(set(candidates))


def main() -> int:
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    units: dict[str, Any] = {}
    errors: list[dict[str, str]] = []
    changed_files = 0

    current_unit_ids: set[str] = set()
    for unit_dir in iter_unit_dirs():
        try:
            data = build_unit(unit_dir)
            if not data:
                continue
            current_unit_ids.add(unit_dir.name)
            out_path = OUT_ROOT / f"{unit_dir.name}.json"
            if write_if_changed(out_path, stable_json(data)):
                changed_files += 1
            units[unit_dir.name] = {
                "unit_id": unit_dir.name,
                "prefix": data["prefix"],
                "resource_dir": data["resource_dir"],
                "thumbnail": data["thumbnail"],
                "meta": f"animation_meta/{unit_dir.name}.json",
                "parts": sorted(data["parts"].keys()),
                "startup": data["startup"],
            }
        except Exception as exc:
            errors.append({"unit_id": unit_dir.name, "error": str(exc)})

    for old_meta in OUT_ROOT.glob("*.json"):
        if old_meta.name == "index.json":
            continue
        if old_meta.stem not in current_unit_ids:
            old_meta.unlink()
            changed_files += 1

    index = {
        "schema_version": SCHEMA_VERSION,
        "resource_root": ".",
        "count": len(units),
        "units": units,
        "errors": errors,
    }
    if write_if_changed(OUT_ROOT / "index.json", stable_json(index, pretty=True)):
        changed_files += 1

    print(
        f"Built animation metadata for {len(units)} unit(s); "
        f"errors={len(errors)}; changed_files={changed_files}"
    )
    if errors:
        for item in errors[:20]:
            print(f"ERROR {item['unit_id']}: {item['error']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
