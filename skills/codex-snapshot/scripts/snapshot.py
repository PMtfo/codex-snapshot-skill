#!/usr/bin/env python3
"""snapshot.py — 创建 codex 快照。

用法:
  python3 snapshot.py <version> [--force] [--prune-logs <days>] [--internal]

行为:
  1. 校验版本号、目录、Codex.app 状态。
  2. 写到 ~/.codex-snapshots/<version>.partial/,失败可重入。
  3. 遍历 ~/.codex/,按 exclude.txt 过滤;sqlite 走 backup API,其他用 copy2。
  4. 写 manifest.json,打包成 payload.tar.gz,生成 payload.sha256。
  5. 原子 rename 成 <version>/,更新 INDEX.json。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    CODEX_ROOT, SNAP_ROOT, KNOWN_IGNORED_DIRS,
    codex_app_version, ensure_codex_root, err, info, warn,
    is_codex_running, is_sqlite, load_exclude_patterns,
    memories_git_head, sha256_file, sqlite_safe_copy, ts_compact, ts_iso,
    upsert_index_entry, validate_version, walk_codex_files,
)


def prune_logs_in_temp_sqlite(staging_db: Path, days: int) -> None:
    """对 staging 副本 sqlite 做 days 之前的日志裁剪 + VACUUM。

    只对 logs_*.sqlite 类做;表结构不确定时跳过。
    """
    if not staging_db.name.startswith("logs"):
        return
    try:
        cutoff_ms = int((time.time() - days * 86400) * 1000)
        with sqlite3.connect(staging_db, timeout=30) as c:
            cur = c.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
            tables = [r[0] for r in cur.fetchall()]
            for t in tables:
                cur.execute(f"PRAGMA table_info('{t}')")
                cols = [r[1].lower() for r in cur.fetchall()]
                ts_col = None
                for cand in ("timestamp", "ts", "created_at", "time"):
                    if cand in cols:
                        ts_col = cand
                        break
                if ts_col is None:
                    continue
                # 判断列是 ms 还是 s,简单按数量级猜
                cur.execute(f'SELECT MAX("{ts_col}") FROM "{t}"')
                mx = cur.fetchone()[0]
                if mx is None:
                    continue
                if mx > 10**12:
                    cutoff = cutoff_ms
                elif mx > 10**9:
                    cutoff = int(time.time() - days * 86400)
                else:
                    continue
                cur.execute(f'DELETE FROM "{t}" WHERE "{ts_col}" < ?', (cutoff,))
                info(f"prune-logs: {staging_db.name}.{t} 删 {cur.rowcount} 行")
            cur.execute("VACUUM")
    except sqlite3.Error as e:
        warn(f"prune-logs 跳过 {staging_db.name}:{e}")


def collect_snapshot(staging_dir: Path, prune_logs: int | None) -> dict:
    patterns = load_exclude_patterns()
    files_meta: dict[str, dict] = {}
    total_bytes = 0
    file_count = 0

    for src_abs, rel in walk_codex_files(patterns):
        dst = staging_dir / "tree" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            if is_sqlite(rel):
                sqlite_safe_copy(src_abs, dst)
                if prune_logs and prune_logs > 0:
                    prune_logs_in_temp_sqlite(dst, prune_logs)
            else:
                shutil.copy2(src_abs, dst)
        except Exception as e:
            warn(f"复制失败,跳过 {rel}: {e}")
            continue

        try:
            st_src = src_abs.stat()
        except FileNotFoundError:
            continue
        try:
            sha = sha256_file(dst)
        except Exception as e:
            warn(f"sha256 失败 {rel}: {e}")
            sha = ""
        size = dst.stat().st_size
        files_meta[rel] = {
            "sha256": sha,
            "size": size,
            "src_mtime_ns": st_src.st_mtime_ns,
            "is_sqlite": is_sqlite(rel),
        }
        total_bytes += size
        file_count += 1
        if file_count % 200 == 0:
            info(f"...已快照 {file_count} 个文件,累计 {total_bytes // (1024*1024)} MB")

    info(f"共快照 {file_count} 个文件,源数据合计 {total_bytes // (1024*1024)} MB(压缩前)")
    return {"files": files_meta, "file_count": file_count, "total_bytes": total_bytes}


def make_tarball(staging_dir: Path) -> tuple[Path, str, int]:
    """打包 staging/tree → staging/payload.tar.gz,返回 (path, sha256, size)。"""
    payload = staging_dir / "payload.tar.gz"
    tree = staging_dir / "tree"
    info("打包 tar.gz(gzip 压缩,可能需要 1-3 分钟)...")
    with tarfile.open(payload, "w:gz", compresslevel=6) as tf:
        tf.add(tree, arcname="tree", recursive=True)
    sha = sha256_file(payload)
    (staging_dir / "payload.sha256").write_text(sha + "  payload.tar.gz\n", encoding="utf-8")
    return payload, sha, payload.stat().st_size


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("version")
    ap.add_argument("--force", action="store_true", help="覆盖已存在版本(自动备份成 <version>.bak-<ts>)")
    ap.add_argument("--prune-logs", type=int, default=None, help="仅保留最近 N 天的对话日志(对 logs_*.sqlite 副本做)")
    ap.add_argument("--internal", action="store_true", help="内部调用(pre-restore 等),不打印用户提示")
    ap.add_argument(
        "--allow-while-running",
        action="store_true",
        help="允许在 Codex.app 运行中快照(SQLite 走 backup API)。默认禁止,要求先退出 Codex 拿到一个'真.干净'的快照。"
    )
    args = ap.parse_args()

    version = validate_version(args.version)
    ensure_codex_root()
    SNAP_ROOT.mkdir(parents=True, exist_ok=True)

    target = SNAP_ROOT / version
    if target.exists():
        if not args.force:
            err(f"版本 '{version}' 已存在:{target}。加 --force 才覆盖(会先备份成 .bak-<ts>)")
            return 1
        bak = target.with_name(f"{version}.bak-{ts_compact()}")
        info(f"--force:已存在版本备份为 {bak.name}")
        target.rename(bak)

    running = is_codex_running()
    app_ver = codex_app_version()

    # 默认强约束:Codex.app 运行中拒绝快照,提醒用户退出后重试。
    # pre-restore 等内部调用、用户显式 --allow-while-running 时跳过。
    if running and not args.internal and not args.allow_while_running:
        err("==================================================")
        err("Codex.app 当前正在运行,默认拒绝快照。")
        err("")
        err("建议先彻底退出 Codex.app(⌘Q 或菜单 Codex → 退出 Codex),")
        err("等 sqlite WAL 都合并后再重新跑本命令,拿到一个真正干净的快照。")
        err("")
        err("如果你确认 sqlite 在线 backup 足够安全,可加 --allow-while-running 强制继续:")
        err(f"  python3 {Path(__file__).name} {version} --allow-while-running")
        err("==================================================")
        return 2

    staging = SNAP_ROOT / f"{version}.partial-{os.getpid()}"
    if staging.exists():
        shutil.rmtree(staging)
    (staging / "tree").mkdir(parents=True)

    if not args.internal:
        info(f"开始快照 → {target.name}")
        if running:
            warn("Codex.app 正在运行,--allow-while-running 已传,走 SQLite backup API。")
        else:
            info(f"Codex.app: 未运行(干净状态),应用版本 {app_ver}")

    summary = collect_snapshot(staging, args.prune_logs)

    payload_path, payload_sha, payload_size = make_tarball(staging)

    manifest = {
        "version": version,
        "created_at": ts_iso(),
        "codex_app_version": app_ver,
        "codex_running_at_snapshot": running,
        "file_count": summary["file_count"],
        "uncompressed_bytes": summary["total_bytes"],
        "compressed_bytes": payload_size,
        "payload_sha256": payload_sha,
        "memories_git_head": memories_git_head(),
        "prune_logs_days": args.prune_logs,
        "known_ignored_dirs": [str(p) for p in KNOWN_IGNORED_DIRS],
        "files": summary["files"],
    }
    (staging / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    # 原子重命名
    staging.rename(target)

    # 清理 tree(只留 payload + manifest + sha)
    tree_dir = target / "tree"
    if tree_dir.exists():
        shutil.rmtree(tree_dir)

    upsert_index_entry(version, {
        "path": str(target),
        "created_at": manifest["created_at"],
        "compressed_bytes": payload_size,
        "uncompressed_bytes": summary["total_bytes"],
        "file_count": summary["file_count"],
        "codex_app_version": app_ver,
    })

    if not args.internal:
        info(f"✓ 快照完成:{target}")
        info(f"  文件数 {summary['file_count']},压缩后 {payload_size // (1024*1024)} MB,源合计 {summary['total_bytes'] // (1024*1024)} MB")
        info(f"  manifest: {target}/manifest.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
