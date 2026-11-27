#!/usr/bin/env python3
"""Pin a published ``flutter`` deb checksum into the repo.

Single direction: published artifact -> repository pins.

Run after a ``build.yml`` release publishes, so installers never keep a
fail-closed placeholder after the asset exists. ``--check`` reports drift
without writing.

Usage:
    python scripts/ci/pin_release_sha.py --sha256 <64-hex> [--size BYTES]
    python scripts/ci/pin_release_sha.py --deb path/to/flutter_*.deb
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHA_RE = re.compile(r"^[0-9a-f]{64}$")

INSTALLER_FILES = [
    "scripts/install/versions_common.sh",
    "scripts/install/lib_common.sh",
    "scripts/test/gh_e2e_test.sh",
]
# EXPECTED_SHA256="${FLUTTER_DEB_SHA256:-${EXPECTED_SHA256:-<hash>}}" (quoted or not)
INSTALLER_PIN_RE = re.compile(
    r"(EXPECTED_SHA256=\"?\$\{FLUTTER_DEB_SHA256:-\$\{EXPECTED_SHA256:-)([^}\n]*)(\}\}\"?)"
)
SHA_DOC_RE = re.compile(r"^(\| SHA256 \| `?)([^`|\n]*)(`? \|)$", re.M)
SIZE_DOC_RE = re.compile(
    r"^(\| (?:Package size|Size|deb size) \| `?)([^`|\n]*)(`? \|)$", re.M
)
EXPECTED_DOC_RE = re.compile(r"^(# Expected: ).*$", re.M)
SMOKE_DOC_RE = re.compile(r"^(expected_sha256: ).*$", re.M)


def fail(msg: str) -> int:
    print(f"pin_release_sha: {msg}", file=sys.stderr)
    return 1


def format_size(size: int) -> str:
    return f"{size} bytes (~{round(size / (1024 * 1024))} MiB)"


def set_flutter_value(text: str, key: str, value: str) -> str:
    """Replace ``[flutter] <key>``, inserting the key when it is absent."""
    lines = text.splitlines(keepends=True)
    in_flutter = False
    pattern = re.compile(rf"^{re.escape(key)}\s*=")
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("["):
            in_flutter = stripped == "[flutter]"
            continue
        if in_flutter and pattern.match(stripped) and not stripped.startswith("#"):
            newline = "\r\n" if line.endswith("\r\n") else "\n"
            indent = line[: len(line) - len(line.lstrip())]
            lines[i] = f"{indent}{key} = '{value}'{newline}"
            return "".join(lines)

    # No such key yet: insert after devtools_version, else right after [flutter].
    insert_at = None
    in_flutter = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "[flutter]":
            in_flutter = True
            insert_at = i + 1
            continue
        if in_flutter and stripped.startswith("["):
            break
        if in_flutter and stripped.startswith("devtools_version"):
            insert_at = i + 1
    if insert_at is None:
        raise ValueError("build.toml has no [flutter] section")
    lines.insert(insert_at, f"{key} = '{value}'\n")
    return "".join(lines)


def derive_asset_name(root: Path) -> str:
    """Compute the deb asset name from build.toml, same math as build.py."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import tomllib

    import version_lib

    with open(root / "build.toml", "rb") as f:
        cfg = tomllib.load(f)
    flutter = cfg.get("flutter", {})
    version = version_lib.deb_version(
        str(flutter.get("tag", "")),
        str(cfg.get("package", {}).get("pkg_rel", "") or ""),
        version_lib.snapshot_stamp(
            str(flutter.get("framework_commit_date", "") or ""),
            str(flutter.get("framework_revision", "") or ""),
        ),
        str(flutter.get("framework_version", "") or ""),
    )
    return f"flutter_{version}_aarch64.deb"


def rewrite_installer(text: str, sha: str) -> str:
    return INSTALLER_PIN_RE.sub(lambda m: f"{m.group(1)}{sha}{m.group(3)}", text)


def rewrite_doc(text: str, sha: str, size: int | None) -> str:
    text = SHA_DOC_RE.sub(lambda m: f"{m.group(1)}{sha}{m.group(3)}", text)
    text = EXPECTED_DOC_RE.sub(lambda m: f"{m.group(1)}{sha}", text)
    text = SMOKE_DOC_RE.sub(lambda m: f"{m.group(1)}{sha}", text)
    if size:
        text = SIZE_DOC_RE.sub(
            lambda m: f"{m.group(1)}{format_size(size)}{m.group(3)}", text
        )
    return text


def targets(root: Path) -> list[Path]:
    files = [root / "build.toml", root / "README.md"]
    files += [root / rel for rel in INSTALLER_FILES]
    docs = root / "docs"
    if docs.is_dir():
        files += sorted(docs.rglob("*.md"))
    return [p for p in files if p.is_file()]


def apply(
    root: Path, sha: str, size: int | None, check_only: bool
) -> tuple[list[str], list[str]]:
    """Return (changed, missing) paths."""
    changed: list[str] = []
    missing: list[str] = []
    asset = derive_asset_name(root)
    for path in targets(root):
        rel = path.relative_to(root).as_posix()
        original = path.read_text(encoding="utf-8")
        if rel == "build.toml":
            updated = set_flutter_value(original, "sha256_asset", asset)
            updated = set_flutter_value(updated, "sha256", sha)
        elif rel in INSTALLER_FILES:
            updated = rewrite_installer(original, sha)
        else:
            updated = rewrite_doc(original, sha, size)
        if rel in INSTALLER_FILES and not INSTALLER_PIN_RE.search(original):
            missing.append(rel)
        if updated != original:
            changed.append(rel)
            if not check_only:
                path.write_text(updated, encoding="utf-8")
    return changed, missing


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--sha256", help="published deb SHA256 (64 lowercase hex)")
    group.add_argument("--deb", help="local deb file to hash")
    ap.add_argument("--size", type=int, help="published deb size in bytes")
    ap.add_argument("--root", default=str(ROOT), help="repository root (default: repo)")
    ap.add_argument("--check", action="store_true", help="report drift, write nothing")
    args = ap.parse_args()

    if args.deb:
        deb = Path(args.deb)
        if not deb.is_file():
            return fail(f"deb not found: {deb}")
        digest = hashlib.sha256()
        with deb.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
        sha = digest.hexdigest()
        size = args.size or deb.stat().st_size
    else:
        sha = (args.sha256 or "").strip().lower()
        size = args.size

    if not SHA_RE.fullmatch(sha):
        return fail(f"not a 64-hex sha256: {sha!r}")

    root = Path(args.root)
    asset = derive_asset_name(root)
    if args.deb and Path(args.deb).name != asset:
        return fail(f"deb {Path(args.deb).name!r} is not the configured asset {asset!r}")

    changed, missing = apply(root, sha, size, args.check)

    for rel in missing:
        print(f"pin_release_sha: no EXPECTED_SHA256 pin found in {rel}", file=sys.stderr)
    if missing:
        return 1
    if args.check:
        if changed:
            print("pins are stale: " + ", ".join(changed), file=sys.stderr)
            return 1
        print(f"release pins are current ({sha[:12]}...).")
        return 0
    if changed:
        print(f"pinned {sha[:12]}... in: " + ", ".join(changed))
    else:
        print(f"release pins already current ({sha[:12]}...).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
