#!/usr/bin/env python3
"""show.py — 显示某个快照的详细信息(不解压)。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import SNAP_ROOT, err, info  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("version")
    args = ap.parse_args()
    target = SNAP_ROOT / args.version
    mpath = target / "manifest.json"
    if not mpath.exists():
        err(f"版本 '{args.version}' 不存在或无 manifest:{mpath}")
        return 1
    m = json.loads(mpath.read_text(encoding="utf-8"))

    files = m.get("files", {})
    agents = [k for k in files if k.startswith("agents/")]
    sqlite_files = [k for k in files if m["files"][k].get("is_sqlite")]
    sessions = [k for k in files if k.startswith("sessions/") or k.startswith("archived_sessions/")]

    print()
    print(f"版本号:        {m['version']}")
    print(f"创建时间:      {m['created_at']}")
    print(f"Codex.app 版本: {m.get('codex_app_version','?')}")
    print(f"快照时 Codex 状态: {'运行中' if m.get('codex_running_at_snapshot') else '未运行'}")
    print(f"文件数:        {m.get('file_count',0)}")
    print(f"未压缩合计:    {m.get('uncompressed_bytes',0) // (1024*1024)} MB")
    print(f"压缩后:        {m.get('compressed_bytes',0) // (1024*1024)} MB")
    print(f"payload sha256: {m.get('payload_sha256','')}")
    print(f"memories git HEAD: {m.get('memories_git_head','-')}")
    print(f"prune-logs days: {m.get('prune_logs_days', '-')}")
    print(f"agents 数:     {len(agents)}")
    print(f"sqlite 文件:   {len(sqlite_files)}({', '.join(sorted(sqlite_files))[:200]})")
    print(f"会话相关文件:  {len(sessions)}")
    print(f"已知忽略目录:")
    for d in m.get("known_ignored_dirs", []):
        print(f"  - {d}")
    print(f"\n快照路径:      {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
