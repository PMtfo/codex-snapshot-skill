#!/usr/bin/env python3
"""delete.py — 软删除某个快照到 ~/.codex-snapshots/.trash/<version>-<ts>/。

30 天内可手动从 trash 移回;30 天后由 list/snapshot 触发的 gc_trash() 真清。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import SNAP_ROOT, TRASH_ROOT, err, info, remove_index_entry, ts_compact  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("version")
    args = ap.parse_args()
    target = SNAP_ROOT / args.version
    if not target.exists():
        err(f"版本 '{args.version}' 不存在:{target}")
        return 1
    TRASH_ROOT.mkdir(parents=True, exist_ok=True)
    dest = TRASH_ROOT / f"{args.version}-{ts_compact()}"
    target.rename(dest)
    remove_index_entry(args.version)
    info(f"✓ 已软删除 → {dest}")
    info("  30 天内可手动 mv 回 ~/.codex-snapshots/;30 天后会自动清理。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
