#!/usr/bin/env python3
"""Sync selected LINE Rangers resource ZIPs into rangerbook_res.

The 10-minute DB updater already fetches the official resource manifest. This
script consumes that same manifest and only downloads a small, explicit asset
whitelist:

- unit resource folders: /u.../hd_u..._<timestamp>.zip
- /ability_icon/hd_ability_icon_<timestamp>.zip
- /gear_icon/hd_gear_icon_<timestamp>.zip
- /skill_icon/hd_skill_icon_<timestamp>.zip

Everything else in the manifest remains record-only. DB/NDB download behavior
is intentionally outside this script and is not changed here.

ZIP payloads are extracted into the repository. Existing files are overwritten
when the official bundle changed, new files are added, and files missing from a
new bundle are NOT deleted automatically.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from urllib.request import Request, urlopen
from zipfile import ZipFile

DEFAULT_CDN_BASE = "https://game.line-scdn.net/inhouse/lgrangers/resources/"
ICON_DIRS = {"ability_icon", "gear_icon", "skill_icon"}
UNIT_DIR_RE = re.compile(r"^u[0-9][A-Za-z0-9_-]*$")
HEX_MD5_RE = re.compile(r"^[0-9a-fA-F]{32}$")


@dataclass(frozen=True)
class Target:
    resource_id: str
    resource_path: str
    destination_dir: str
    size: int | None
    signature: str


class SyncError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync selected LINE Rangers manifest resources into rangerbook_res"
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--repo-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--cdn-base", default=DEFAULT_CDN_BASE)
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--push", action="store_true")
    parser.add_argument("--branch", default=None)
    parser.add_argument(
        "--commit-message",
        default="Sync LINE Rangers resource assets",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def _resource_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]

    if not isinstance(payload, dict):
        raise SyncError("manifest root must be an object or array")

    result = payload.get("result")
    if isinstance(result, list):
        return [row for row in result if isinstance(row, dict)]
    if isinstance(result, dict):
        resources = result.get("resources", [])
        if isinstance(resources, list):
            return [row for row in resources if isinstance(row, dict)]

    # Also accept recorder batch / index shapes for recovery or manual replay.
    items = payload.get("items")
    if isinstance(items, list):
        return [row for row in items if isinstance(row, dict)]

    batches = payload.get("batches")
    if isinstance(batches, list):
        rows: list[dict[str, Any]] = []
        for batch in batches:
            if isinstance(batch, dict) and isinstance(batch.get("items"), list):
                rows.extend(
                    row for row in batch["items"] if isinstance(row, dict)
                )
        return rows

    raise SyncError("manifest does not contain result/resources/items")


def _normalize_path(row: dict[str, Any]) -> str:
    value = row.get("resourcePath") or row.get("path") or ""
    return str(value).strip().replace("\\", "/").lstrip("/")


def _to_size(value: Any) -> int | None:
    try:
        size = int(value)
    except (TypeError, ValueError):
        return None
    return size if size >= 0 else None


def choose_targets(rows: Iterable[dict[str, Any]]) -> list[Target]:
    selected: dict[tuple[str, str], Target] = {}

    for row in rows:
        if row.get("deleted") is True:
            continue

        resource_path = _normalize_path(row)
        if not resource_path.lower().endswith(".zip"):
            continue

        parts = PurePosixPath(resource_path).parts
        if len(parts) < 2:
            continue

        top = parts[0]
        filename = parts[-1]

        destination_dir = ""
        if top in ICON_DIRS:
            if not filename.startswith(f"hd_{top}_"):
                continue
            destination_dir = top
        elif UNIT_DIR_RE.fullmatch(top):
            if not filename.startswith(f"hd_{top}_"):
                continue
            destination_dir = top
        else:
            continue

        resource_id = str(row.get("resourceId") or top).strip() or top
        signature = str(row.get("signature") or "").strip()
        target = Target(
            resource_id=resource_id,
            resource_path=resource_path,
            destination_dir=destination_dir,
            size=_to_size(row.get("size")),
            signature=signature,
        )
        selected[(resource_path, destination_dir)] = target

    return sorted(selected.values(), key=lambda item: item.resource_path)


def _md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def download_target(target: Target, cdn_base: str, output: Path) -> None:
    url = cdn_base.rstrip("/") + "/" + target.resource_path.lstrip("/")
    request = Request(url, headers={"User-Agent": "RangerBookResourceSync/1.0"})
    with urlopen(request, timeout=90) as response, output.open("wb") as handle:
        shutil.copyfileobj(response, handle, length=1024 * 1024)

    actual_size = output.stat().st_size
    if target.size is not None and actual_size != target.size:
        raise SyncError(
            f"size mismatch for {target.resource_path}: "
            f"expected={target.size} actual={actual_size}"
        )

    if HEX_MD5_RE.fullmatch(target.signature):
        actual_md5 = _md5(output)
        if actual_md5.lower() != target.signature.lower():
            raise SyncError(
                f"MD5 mismatch for {target.resource_path}: "
                f"expected={target.signature} actual={actual_md5}"
            )


def _safe_member_path(name: str) -> PurePosixPath:
    member = PurePosixPath(name.replace("\\", "/"))
    if member.is_absolute() or not member.parts or ".." in member.parts:
        raise SyncError(f"unsafe ZIP member: {name!r}")
    return member


def extract_zip(zip_path: Path, destination: Path) -> list[Path]:
    destination.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    with tempfile.TemporaryDirectory(prefix="rangerbook-res-extract-") as tmp_name:
        tmp_root = Path(tmp_name)

        with ZipFile(zip_path) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue

                member = _safe_member_path(info.filename)
                temp_path = tmp_root.joinpath(*member.parts)
                temp_path.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, temp_path.open("wb") as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)

        for source in sorted(path for path in tmp_root.rglob("*") if path.is_file()):
            relative = source.relative_to(tmp_root)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)

            # Avoid touching mtime/content when the payload is byte-identical.
            if target.is_file() and source.stat().st_size == target.stat().st_size:
                if _md5(source) == _md5(target):
                    continue

            shutil.copy2(source, target)
            written.append(target)

    return written


def run_git(repo_dir: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def ensure_sparse_paths(repo_dir: Path, paths: list[str]) -> None:
    enabled = run_git(
        repo_dir,
        "config",
        "--bool",
        "core.sparseCheckout",
        check=False,
    )
    if enabled.returncode != 0 or enabled.stdout.strip().lower() != "true":
        return

    result = run_git(
        repo_dir,
        "sparse-checkout",
        "add",
        "--skip-checks",
        *paths,
        check=False,
    )
    if result.returncode != 0:
        raise SyncError(
            "failed to extend sparse checkout: "
            + (result.stderr.strip() or result.stdout.strip())
        )


def git_changed_paths(repo_dir: Path, paths: list[str]) -> list[str]:
    result = run_git(repo_dir, "status", "--porcelain", "--", *paths)
    changed: list[str] = []
    for line in result.stdout.splitlines():
        if len(line) >= 4:
            changed.append(line[3:])
    return changed


def commit_and_maybe_push(
    repo_dir: Path,
    paths: list[str],
    message: str,
    push: bool,
    branch: str | None,
) -> str | None:
    add = run_git(
        repo_dir,
        "add",
        "--all",
        "--sparse",
        "--",
        *paths,
        check=False,
    )
    if add.returncode != 0:
        raise SyncError(add.stderr.strip() or add.stdout.strip() or "git add failed")

    staged = run_git(repo_dir, "diff", "--cached", "--quiet", check=False)
    if staged.returncode == 0:
        return None
    if staged.returncode != 1:
        raise SyncError(staged.stderr.strip() or "git diff --cached failed")

    commit = run_git(repo_dir, "commit", "-m", message)
    commit_sha = run_git(repo_dir, "rev-parse", "HEAD").stdout.strip()

    if push:
        effective_branch = branch
        if not effective_branch:
            effective_branch = run_git(
                repo_dir,
                "rev-parse",
                "--abbrev-ref",
                "HEAD",
            ).stdout.strip()
        if not effective_branch or effective_branch == "HEAD":
            raise SyncError("--push requires a branch name")

        pull = run_git(
            repo_dir,
            "pull",
            "--rebase",
            "origin",
            effective_branch,
            check=False,
        )
        if pull.returncode != 0:
            raise SyncError(
                "git pull --rebase failed: "
                + (pull.stderr.strip() or pull.stdout.strip())
            )

        pushed = run_git(
            repo_dir,
            "push",
            "origin",
            f"HEAD:{effective_branch}",
            check=False,
        )
        if pushed.returncode != 0:
            raise SyncError(
                "git push failed: "
                + (pushed.stderr.strip() or pushed.stdout.strip())
            )

    # Keep the command output consumed so lint tools do not treat it as unused.
    _ = commit.stdout
    return commit_sha


def main() -> int:
    args = parse_args()
    repo_dir = args.repo_dir.resolve()
    manifest = args.manifest.resolve()

    if not manifest.is_file():
        raise SyncError(f"manifest not found: {manifest}")
    if not repo_dir.is_dir():
        raise SyncError(f"repo dir not found: {repo_dir}")

    payload = json.loads(manifest.read_text(encoding="utf-8"))
    targets = choose_targets(_resource_rows(payload))

    if not targets:
        print("[RES SYNC] no eligible unit/icon resources in this manifest")
        return 0

    target_dirs = sorted({target.destination_dir for target in targets})
    print(
        f"[RES SYNC] eligible resources={len(targets)} "
        f"destinations={len(target_dirs)}"
    )
    for target in targets:
        print(f"[RES SYNC] target {target.resource_path} -> {target.destination_dir}/")

    if args.dry_run:
        return 0

    ensure_sparse_paths(repo_dir, target_dirs)

    downloaded = 0
    written_files: list[Path] = []
    with tempfile.TemporaryDirectory(prefix="rangerbook-res-download-") as tmp_name:
        tmp_root = Path(tmp_name)

        for index, target in enumerate(targets, start=1):
            zip_path = tmp_root / f"{index:04d}.zip"
            download_target(target, args.cdn_base, zip_path)
            downloaded += zip_path.stat().st_size
            written_files.extend(
                extract_zip(zip_path, repo_dir / target.destination_dir)
            )

    changed = git_changed_paths(repo_dir, target_dirs)
    print(
        f"[RES SYNC] downloaded_bytes={downloaded} "
        f"written_files={len(written_files)} git_changes={len(changed)}"
    )
    for path in changed:
        print(f"[RES SYNC] changed {path}")

    if args.commit:
        sha = commit_and_maybe_push(
            repo_dir=repo_dir,
            paths=target_dirs,
            message=args.commit_message,
            push=args.push,
            branch=args.branch,
        )
        if sha:
            print(f"[RES SYNC] commit={sha}")
        else:
            print("[RES SYNC] no Git changes to commit")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
