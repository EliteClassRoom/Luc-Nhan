"""Build curated release archive for Luc Nhan IDA plugin (HCLI flat-ZIP layout).

Chỉ include runtime files cần để install và chạy plugin trong IDA:
- lucnhan_plugin.py  (entry point)
- lucnhan/           (Python package, loại __pycache__)
- install.sh, install_ida.sh, install.ps1, install_ida.bat
- requirements.txt
- ida-plugin.json
- LICENSE
- README.md

Không include: tests/, docs/, AGENTS.md, ARCHITECTURE.md, DEVELOPMENT.md,
llms.txt, .github/, assets/, chat_examples/, pyproject.toml,
uv.lock, ci-local.sh, .git/, .venv/, .*_cache/, __pycache__/.

HCLI layout (per https://hcli.docs.hex-rays.com/reference/plugin-packaging-and-format/):
ida-plugin.json và entryPoint phải ở GỐC zip, không có subfolder bao quanh.

Usage:
    python scripts/build_release.py --version 1.2.3 --out-dir dist

Output:
    dist/lucnhan-v1.2.3.zip
    dist/SHA256SUMS
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import zipfile
from pathlib import Path

# Tên file/dir cần include (paths tương đối so với source root).
INCLUDE_PATHS: list[str] = [
    "lucnhan_plugin.py",
    "lucnhan",  # toàn bộ package
    "install.sh",
    "install_ida.sh",
    "install.ps1",
    "install_ida.bat",
    "requirements.txt",
    "ida-plugin.json",
    "LICENSE",
    "README.md",
]

# File/dir KHÔNG được include dù nằm trong INCLUDE_PATHS (match bất kỳ path part nào).
EXCLUDE_NAMES: set[str] = {
    "__pycache__",
    ".git",
    ".venv",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    ".desloppify",
    ".codegraph",
    ".reasonix",
    ".claude",
    "node_modules",
    "docs",
    "tests",
}

# File suffix KHÔNG được include.
EXCLUDE_SUFFIXES: tuple[str, ...] = (".pyc", ".pyo", ".pyd")


def should_skip(path: Path) -> bool:
    """Return True nếu path nên bị skip (exclude rule match)."""
    if any(part in EXCLUDE_NAMES for part in path.parts):
        return True
    if path.suffix in EXCLUDE_SUFFIXES:
        return True
    return False


def collect(source_root: Path) -> list[Path]:
    """Collect tất cả files trong INCLUDE_PATHS, áp dụng exclude rules.

    Returns:
        Sorted list of absolute paths to files (sorted by relative
        POSIX path so order is deterministic on Windows and matches
        the Linux CI).
    """
    collected: list[Path] = []
    for spec in INCLUDE_PATHS:
        src = source_root / spec
        if src.is_file():
            if not should_skip(src):
                collected.append(src)
        elif src.is_dir():
            for p in src.rglob("*"):
                if p.is_file() and not should_skip(p):
                    collected.append(p)
        # Nếu spec không tồn tại → silently skip
    return sorted(collected, key=lambda p: p.relative_to(source_root).as_posix())


def build_zip(files: list[Path], out_path: Path, source_root: Path) -> None:
    """Build FLAT zip archive (HCLI layout) — no wrapping subfolder.

    Each file's path inside the archive is its path relative to
    ``source_root`` (forward slashes via ``as_posix()``). ``ida-plugin.json``
    and the entryPoint file end up at the ZIP root.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for src in files:
            arcname = src.relative_to(source_root).as_posix()
            zf.write(src, arcname)


# ── SHA256 + CLI ───────────────────────────────────────────────────────


def sha256_file(path: Path) -> str:
    """Compute SHA256 hex digest của file (streamed, 1 MB chunks)."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_sha256sums(paths: list[Path], out_path: Path) -> None:
    """Write SHA256SUMS file (GNU coreutils format: hex + 2 spaces + name).

    Written in binary mode with explicit ``\n`` line endings so the file
    matches the GNU coreutils format on every platform (works with
    ``sha256sum -c`` on Linux CI regardless of the build host's
    default newline convention).
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("wb") as f:
        for p in paths:
            line = f"{sha256_file(p)}  {p.name}\n".encode("utf-8")
            f.write(line)


def main() -> int:
    """CLI entry point. See module docstring.

    Returns:
        0 on success, 1 if no files were collected.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, help="Version vd: 1.2.3")
    parser.add_argument("--out-dir", type=Path, default=Path("dist"))
    parser.add_argument("--source-root", type=Path, default=Path("."))
    args = parser.parse_args()

    archive_name = f"lucnhan-v{args.version}.zip"
    args.out_dir.mkdir(parents=True, exist_ok=True)
    files = collect(args.source_root)
    if not files:
        print("ERROR: no files collected (source root empty?)", file=sys.stderr)
        return 1

    zip_path = args.out_dir / archive_name
    build_zip(files, zip_path, args.source_root)

    sums_path = args.out_dir / "SHA256SUMS"
    write_sha256sums([zip_path], sums_path)

    print(f"OK: {zip_path} ({zip_path.stat().st_size} bytes, {len(files)} files)")
    print(f"OK: {sums_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
