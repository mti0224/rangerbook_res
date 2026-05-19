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
PARTS = ("body", "bul", "bul2", "bul3")
STARTUP_SEGMENTS = {
    "normal_attack": "attack_ready",
    "skill_1": "s_attack_ready",
    "skill_2": "s2_attack_ready",
}
COMBO_SEGMENTS = {
    "attack_all": ("attack_ready", "attack"),
    "s_attack_all": ("s_attack_ready", "s_attack"),
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

    def __init__(self, path: Path):
        self.path = path
        self.raw = path.read_bytes()
        self.pos = 0
        self.anim_rate = 24
        self.x = 0.0
        self.y = 0.0
        self.canvas_w = 0.0
        self.canvas_h = 0.0
        self.images: list[dict[str, Any]] = []
        self.animations: dict[str, list[list[list[Any]]]] = {}
        self.anim_names: list[str] = []
        self.frames: list[list[list[Any]]] = []
        self._parse()

    def _u8(self) -> int:
        value = self.raw[self.pos]
        self.pos += 1
        return value

    def _u16(self) -> int:
        value = struct.unpack_from("<H", self.raw, self.pos)[0]
        self.pos += 2
        return value

    def _i16(self) -> int:
        value = struct.unpack_from("<h", self.raw, self.pos)[0]
        self.pos += 2
        return value

    def _i32(self) -> int:
        value = struct.unpack_from("<i", self.raw, self.pos)[0]
        self.pos += 4
        return value

    def _str(self) -> str:
        length = self._u16()
        value = self.raw[self.pos:self.pos + length].decode("ascii", errors="replace")
        self.pos += length
        return value

    @staticmethod
    def _round_matrix(values: tuple[float, float, float, float, float, float]) -> list[float]:
        return [round(value, 6) for value in values]

    def _parse(self) -> None:
        magic = self._i32()
        if magic != 0x2E53414D:
            raise ValueError(f"Bad SAM magic: {self.path}")
        version = self._i32()
        if version != 1:
            raise ValueError(f"Unsupported SAM version {version}: {self.path}")

        self.anim_rate = self._u8()
        self.x = self._i32() / self.TWIPS
        self.y = self._i32() / self.TWIPS
        self.canvas_w = self._i32() / self.TWIPS
        self.canvas_h = self._i32() / self.TWIPS

        for _ in range(self._i16()):
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
            self.images.append({"name": name, "w": width, "h": height, "m": self._round_matrix(matrix)})

        objects: dict[int, dict[str, Any]] = {}
        depth_memory: dict[int, dict[str, Any]] = {}
        current_anim = "_intro"
        self.animations[current_anim] = []
        self.anim_names.append(current_anim)

        for _ in range(self._i16()):
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
                if name not in self.animations:
                    self.animations[name] = []
                    self.anim_names.append(name)
                current_anim = name

            snapshot: list[list[Any]] = []
            for obj_num in sorted(objects.keys()):
                obj = objects[obj_num]
                snapshot.append([
                    obj_num,
                    obj["res"],
                    self._round_matrix(obj["m"]),
                    list(obj["color"]),
                ])
            self.animations[current_anim].append(snapshot)
            self.frames.append(snapshot)

        if not self.animations.get("_intro"):
            self.animations.pop("_intro", None)
            if "_intro" in self.anim_names:
                self.anim_names.remove("_intro")

        all_frames: list[list[list[Any]]] = []
        self.all_segments: list[dict[str, Any]] = []
        for name in list(self.anim_names):
            start = len(all_frames)
            all_frames.extend(self.animations[name])
            self.all_segments.append({"name": name, "start": start, "end": len(all_frames)})
        self.animations["_all"] = all_frames
        if "_all" not in self.anim_names:
            self.anim_names.insert(0, "_all")

    def to_json(self) -> dict[str, Any]:
        animations = dict(self.animations)
        for combo_name, segment_names in COMBO_SEGMENTS.items():
            combo_frames: list[list[list[Any]]] = []
            for segment_name in segment_names:
                combo_frames.extend(self.animations.get(segment_name, []))
            if combo_frames:
                animations[combo_name] = combo_frames

        return {
            "anim_rate": self.anim_rate,
            "canvas": {
                "x": round(self.x, 3),
                "y": round(self.y, 3),
                "w": round(self.canvas_w, 3),
                "h": round(self.canvas_h, 3),
            },
            "images": self.images,
            "segments": self.all_segments,
            "animations": {
                name: {"frame_count": len(frames), "frames": frames}
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


def plist_rect(value: str) -> list[int]:
    numbers = [int(item) for item in re.findall(r"-?\d+", value or "")]
    return numbers[:4] if len(numbers) >= 4 else [0, 0, 0, 0]


def load_plist(path: Path) -> dict[str, Any]:
    with path.open("rb") as file:
        data = plistlib.load(file)
    result: dict[str, Any] = {}
    for name, info in data.get("frames", {}).items():
        result[name] = {
            "rect": plist_rect(info.get("textureRect", "")),
            "rotated": bool(info.get("textureRotated", False)),
        }
    return result


def root_relative(path: Path) -> str:
    return str(path.relative_to(ROOT)).replace("\\", "/")


def build_part(unit_dir: Path, prefix: str, part: str) -> dict[str, Any] | None:
    stem = f"{prefix}-{part}"
    sam_path = unit_dir / f"{stem}.sam"
    plist_path = unit_dir / f"{stem}.plist"
    png_path = unit_dir / f"{stem}.png"
    if not (sam_path.exists() and plist_path.exists() and png_path.exists()):
        return None

    parser = SAMParser(sam_path)
    return {
        "sam": root_relative(sam_path),
        "plist": root_relative(plist_path),
        "png": root_relative(png_path),
        "sprites": load_plist(plist_path),
        **parser.to_json(),
    }


def build_unit(unit_dir: Path) -> dict[str, Any] | None:
    body_candidates = sorted(unit_dir.glob("*-body.sam"))
    if not body_candidates:
        return None
    prefix = body_candidates[0].name[:-len("-body.sam")]

    parts: dict[str, Any] = {}
    for part in PARTS:
        built = build_part(unit_dir, prefix, part)
        if built:
            parts[part] = built

    body = parts.get("body")
    if not body:
        return None

    startup: dict[str, Any] = {}
    for label, segment in STARTUP_SEGMENTS.items():
        frame_count = body["animations"].get(segment, {}).get("frame_count", 0)
        startup[label] = {
            "segment": segment,
            "frames": frame_count,
            "seconds": round(frame_count / max(1, body["anim_rate"]), 4),
        }

    thumb = unit_dir / f"{prefix}-thum.png"
    return {
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

    current_unit_ids = set()
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
        "resource_root": ".",
        "count": len(units),
        "units": units,
        "errors": errors,
    }
    if write_if_changed(OUT_ROOT / "index.json", stable_json(index, pretty=True)):
        changed_files += 1

    print(f"Built animation metadata for {len(units)} unit(s); errors={len(errors)}; changed_files={changed_files}")
    if errors:
        for item in errors[:20]:
            print(f"ERROR {item['unit_id']}: {item['error']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
