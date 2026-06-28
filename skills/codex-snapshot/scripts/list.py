#!/usr/bin/env python3
"""list.py — 列出所有 codex 快照,按创建时间倒序。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import SNAP_ROOT, load_index, info, gc_trash  # noqa: E402


def fmt_bytes(n: int) -> str:
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.2f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


def main() -> int:
    gc_trash(30)
    idx = load_index()
    versions = idx.get("versions", {})

    # 与磁盘对账:磁盘上有但 INDEX.json 中没有的版本也列出(标记 [no-index])
    on_disk = set()
    if SNAP_ROOT.exists():
        for p in SNAP_ROOT.iterdir():
            if not p.is_dir() or p.name.startswith(".") or p.name.endswith(".partial") or "partial-" in p.name:
                continue
            on_disk.add(p.name)

    all_versions: dict[str, dict] = {}
    for v, entry in versions.items():
        all_versions[v] = {**entry, "_source": "index"}
    for v in on_disk - set(versions.keys()):
        path = SNAP_ROOT / v
        try:
            st = path.stat()
            all_versions[v] = {
                "path": str(path),
                "created_at": "",
                "compressed_bytes": st.st_size,
                "_source": "disk-only",
            }
        except FileNotFoundError:
            continue

    if not all_versions:
        info("(无快照)")
        info(f"快照仓库目录:{SNAP_ROOT}")
        return 0

    def sort_key(item: tuple[str, dict]):
        return item[1].get("created_at", ""), item[0]

    items = sorted(all_versions.items(), key=sort_key, reverse=True)

    print(f"\n{'版本':40} {'压缩大小':>12} {'文件数':>7}  {'Codex.app':>10}  创建时间")
    print("-" * 110)
    for v, e in items:
        comp = fmt_bytes(int(e.get("compressed_bytes", 0)))
        fc = e.get("file_count", "-")
        cv = e.get("codex_app_version", "-")
        ts = e.get("created_at", "-")
        suffix = "" if e.get("_source") == "index" else "  [no-index]"
        print(f"{v:40} {comp:>12} {str(fc):>7}  {str(cv):>10}  {ts}{suffix}")
    print()
    info(f"快照仓库:{SNAP_ROOT}  (共 {len(items)} 个)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
