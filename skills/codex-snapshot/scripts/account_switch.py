#!/usr/bin/env python3
"""account_switch.py - 换号后让旧账号产生的对话在新账号下重新可见。

用法:
  python3 account_switch.py prepare [--provider openai]
  python3 account_switch.py finish  [--title-from <version>]

背景:
  Codex.app 换 OpenAI 账号后, sidebar 默认会过滤掉跟当前 model_provider 不
  匹配的 thread, 加上 backfill 重建索引时不会保留 LLM 总结的 title 字段。
  这个脚本是治这个问题的官方机制 + 经验组合方案。

机制(分两阶段):
  prepare(改文件,要求重启 Codex 之前跑):
    1. 自动 pre-account-switch-<ts> 全量快照保险(走 snapshot.py)
    2. 批量改 sessions/ + archived_sessions/ 所有 jsonl 第一行 session_meta:
       model_provider -> 用户指定 provider (默认 openai)
    3. 将 state_5.sqlite 三件套 + cache/ + session_index.jsonl 移动到
       ~/.codex-snapshots/.account-switch-trash/<ts>/ (不真删,30 天可还原)
    4. 提示用户 ⌘Q 退出 Codex 后重新打开, 等 20 秒让 backfill 跑完,
       再回头跑 finish

  finish(收尾,要求重启 Codex 之后跑):
    1. 校验 backfill 已 complete (state_5.sqlite 存在 + threads 表 >0)
    2. UPDATE threads SET thread_source='user' WHERE thread_source IS NULL
       (sidebar UI 二次过滤会隐藏 thread_source=NULL 的 thread)
    3. UPDATE threads SET model_provider=<provider> WHERE
       model_provider != <provider> (清扫 backfill 残留)
    4. 从最近一份 pre-account-switch 快照 merge title 列 (backfill 不会生
       成 LLM 总结的 title, 重建后 title 都会变成 first_user_message,
       从备份 merge 回 LLM 总结的版本)

强约束:
  - Codex.app 运行中拒绝跑 prepare(避免 sqlite 锁冲突)
  - Codex.app 运行中拒绝跑 finish(避免改 sqlite 跟 Codex 写入冲突)
  - prepare 跑完必须用户重启 Codex.app, 否则 finish 找不到 sqlite
  - 任何破坏性动作前先备份
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    CODEX_ROOT, SNAP_ROOT,
    confirm_yes, ensure_codex_root, err, info, warn,
    is_codex_running, ts_compact,
)
from snapshot import main as snapshot_main  # noqa: E402

ACCOUNT_SWITCH_TRASH = SNAP_ROOT / ".account-switch-trash"

# 这几个文件/目录在 prepare 阶段会被 mv 到 trash, 让 Codex 启动时重建
FILES_TO_MOVE_TO_TRASH = [
    "state_5.sqlite",
    "state_5.sqlite-shm",
    "state_5.sqlite-wal",
    "session_index.jsonl",
]
DIRS_TO_MOVE_TO_TRASH = [
    "cache",
]


# ---------- 工具 ----------
def ensure_codex_not_running(stage: str) -> None:
    if is_codex_running():
        err(f"Codex.app 正在运行, 拒绝跑 {stage}")
        err("请 ⌘Q 彻底退出 Codex.app 后重试")
        sys.exit(2)


def snapshot_main_with_args(argv: list[str]) -> int:
    saved = sys.argv[:]
    try:
        sys.argv = ["snapshot.py"] + argv
        return snapshot_main()
    finally:
        sys.argv = saved


# ---------- prepare ----------
def cmd_prepare(provider: str) -> int:
    ensure_codex_root()
    ensure_codex_not_running("account-switch prepare")

    info("==================================================")
    info("阶段 1: prepare")
    info(f"目标 model_provider: {provider}")
    info("即将执行的破坏性操作:")
    info(f"  [1] 自动 pre-account-switch-<ts> 全量快照(走 snapshot.py)")
    info(f"  [2] 批量改 sessions/ + archived_sessions/ 所有 jsonl")
    info(f"      session_meta.model_provider -> '{provider}'")
    info(f"  [3] 将 state_5.sqlite 三件套 + cache/ + session_index.jsonl")
    info(f"      移动到 {ACCOUNT_SWITCH_TRASH} 下(不真删)")
    info("==================================================")
    if not confirm_yes("是否继续? 严格输入 yes 继续: "):
        err("已取消, 未做任何修改")
        return 1

    # 1. pre-account-switch 全量快照
    pre_ver = f"pre-account-switch-{ts_compact()}"
    info(f"\n[1/3] 自动全量快照 -> {pre_ver}")
    rc = snapshot_main_with_args([pre_ver, "--allow-while-running"])
    if rc != 0:
        err("pre-account-switch 快照失败, 中止 account-switch 以保安全")
        return 1
    info(f"      ✓ 已落地 {SNAP_ROOT / pre_ver}")

    # 2. 批量改 jsonl model_provider
    info(f"\n[2/3] 批量改 jsonl model_provider -> '{provider}'")
    roots = [CODEX_ROOT / "sessions", CODEX_ROOT / "archived_sessions"]
    total = changed = skipped = errors = 0
    prov_seen: dict[str, int] = {}
    for root in roots:
        if not root.exists():
            continue
        for p in sorted(root.rglob("rollout-*.jsonl")):
            total += 1
            try:
                with open(p, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                if not lines:
                    skipped += 1
                    continue
                first = json.loads(lines[0])
                payload = first.get("payload")
                if not isinstance(payload, dict):
                    skipped += 1
                    continue
                old = payload.get("model_provider")
                prov_seen[str(old)] = prov_seen.get(str(old), 0) + 1
                if old == provider:
                    continue
                if old is None:
                    continue
                payload["model_provider"] = provider
                lines[0] = json.dumps(first, ensure_ascii=False) + "\n"
                tmp = str(p) + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.writelines(lines)
                os.replace(tmp, p)
                changed += 1
                if changed % 100 == 0:
                    info(f"      ...已改 {changed} 个")
            except Exception as e:
                errors += 1
                warn(f"      ERR {p}: {e}")
    info(f"      ✓ 扫描 {total}, 改写 {changed}, 跳过 {skipped}, 错误 {errors}")
    info(f"      改前 model_provider 分布:")
    for k, v in sorted(prov_seen.items(), key=lambda x: -x[1]):
        info(f"        {k!r}: {v}")

    # 3. mv state_5 三件套 + cache/ + session_index.jsonl 到 trash
    ts = ts_compact()
    trash_dir = ACCOUNT_SWITCH_TRASH / ts
    trash_dir.mkdir(parents=True, exist_ok=True)
    info(f"\n[3/3] 移动旧索引到 trash: {trash_dir}")
    moved = []
    for fname in FILES_TO_MOVE_TO_TRASH:
        src = CODEX_ROOT / fname
        if src.exists():
            dst = trash_dir / fname
            shutil.move(str(src), str(dst))
            moved.append(fname)
            info(f"      mv {fname}")
    for dname in DIRS_TO_MOVE_TO_TRASH:
        src = CODEX_ROOT / dname
        if src.exists():
            dst = trash_dir / dname
            shutil.move(str(src), str(dst))
            moved.append(dname + "/")
            info(f"      mv {dname}/")
    info(f"      ✓ 共移动 {len(moved)} 项")

    # 写下 finish 阶段需要的 hint
    hint = {
        "pre_account_switch_snapshot": pre_ver,
        "provider": provider,
        "trash_dir": str(trash_dir),
        "prepared_at": ts,
        "jsonl_changed": changed,
    }
    (trash_dir / "account-switch-hint.json").write_text(
        json.dumps(hint, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    info("\n==================================================")
    info("✓ prepare 完成")
    info("==================================================")
    info("\n请按以下步骤操作:")
    info("  1. ⌘Q 打开 Codex.app(或者直接打开 Codex.app)")
    info("  2. 等待 10-30 秒, Codex 会自动 backfill 扫描 sessions/ 重建索引")
    info("  3. (此时 sidebar 可能短暂只显示几条对话)")
    info("  4. 跑下一阶段命令完成收尾:")
    info(f"     python3 {Path(__file__).name} finish")
    info(f"\n保险快照: {SNAP_ROOT / pre_ver}")
    info(f"旧索引备份: {trash_dir}")
    return 0


# ---------- finish ----------
def cmd_finish(title_from: str | None, provider_override: str | None = None) -> int:
    """收尾清扫。可重复运行(用户 Codex 用一段时间发现还有 thread 漏过滤,可再跑)。

    优先级:
      1. 命令行 --provider 显式传入
      2. 最近一次 prepare 写的 hint.json
      3. fallback 'openai'
    """
    ensure_codex_root()
    ensure_codex_not_running("account-switch finish")

    info("==================================================")
    info("阶段 2: finish (可重复运行)")
    info("==================================================")

    db = CODEX_ROOT / "state_5.sqlite"
    if not db.exists():
        err(f"state_5.sqlite 不存在: {db}")
        err("说明 Codex.app 还没启动过, 请先打开 Codex.app 等 backfill 跑完, 再回来跑 finish")
        return 2

    # 取最近一份 account-switch hint(可能不存在 - 用户跳过 prepare 直接跑 finish 修残留)
    latest_hint_dir = None
    hint = {}
    if ACCOUNT_SWITCH_TRASH.exists():
        cands = sorted(
            [d for d in ACCOUNT_SWITCH_TRASH.iterdir() if (d / "account-switch-hint.json").exists()],
            key=lambda d: d.name,
            reverse=True,
        )
        if cands:
            latest_hint_dir = cands[0]
            hint = json.loads((latest_hint_dir / "account-switch-hint.json").read_text(encoding="utf-8"))

    # 优先级: --provider > hint > 'openai'
    if provider_override:
        provider = provider_override
        info(f"使用命令行指定 provider: {provider}")
    elif hint.get("provider"):
        provider = hint["provider"]
        info(f"使用最近一次 prepare 的 provider: {provider}")
    else:
        provider = "openai"
        info(f"未找到 hint, 使用默认 provider: {provider}")

    pre_snapshot = hint.get("pre_account_switch_snapshot")
    title_from = title_from or pre_snapshot
    if pre_snapshot:
        info(f"  pre_account_switch 快照: {pre_snapshot}")
    if title_from:
        info(f"  title 来源: {title_from}")

    # backup current sqlite
    bak = db.with_name(f"state_5.sqlite.before-account-switch-finish-{ts_compact()}")
    shutil.copy2(db, bak)
    info(f"备份当前 sqlite: {bak.name}")

    # 清 wal/shm
    for sfx in ("-shm", "-wal"):
        p = CODEX_ROOT / f"state_5.sqlite{sfx}"
        if p.exists():
            p.unlink()

    # 校验
    with sqlite3.connect(db) as con:
        total = con.execute("SELECT count(*) FROM threads").fetchone()[0]
    info(f"backfill 后 threads 表共 {total} 条 thread")
    if total == 0:
        err("threads 表为空, 说明 backfill 没跑出任何对话")
        err("建议重新打开 Codex.app 等 60 秒, 再跑 finish")
        return 2

    # UPDATE thread_source NULL -> 'user'
    info("\n[1/3] UPDATE thread_source NULL -> 'user' (sidebar 不显示 NULL 的)")
    with sqlite3.connect(db) as con:
        cur = con.execute(
            "UPDATE threads SET thread_source = 'user' WHERE thread_source IS NULL OR thread_source = ''"
        )
        info(f"      ✓ 改了 {cur.rowcount} 条")
        con.commit()

    # UPDATE model_provider 残留
    info(f"\n[2/3] UPDATE model_provider != '{provider}' -> '{provider}' (清扫 backfill 残留)")
    with sqlite3.connect(db) as con:
        cur = con.execute(
            "UPDATE threads SET model_provider = ? WHERE model_provider != ? AND thread_source != 'subagent'",
            (provider, provider),
        )
        info(f"      ✓ 改了 {cur.rowcount} 条")
        con.commit()

    # merge title 列 from pre-snapshot
    if title_from is None:
        info(f"\n[3/3] 跳过 title merge (--title-from 未指定 且 没有 pre-snapshot hint)")
        info(f"      (重复跑 finish 时正常, 现有 sqlite 中的 title 不变)")
        snap_db = None
    else:
        info(f"\n[3/3] 从 pre-snapshot '{title_from}' merge title 列")
    snap_dir = SNAP_ROOT / title_from if title_from else None
    snap_db = _extract_sqlite_from_snapshot(snap_dir) if snap_dir else None
    if title_from and snap_db is None:
        warn(f"      ✗ 找不到 {title_from} 中的 state_5.sqlite, 跳过 title merge")
        warn(f"        (说明:v2 之后 snapshot 不再打包 sqlite, title 无法从快照恢复)")
        warn(f"        (用户对话 title 会变成 first_user_message 的开头,后续 Codex 会逐步生成新 title)")
    elif snap_db:
        with sqlite3.connect(db) as con:
            con.execute(f"ATTACH DATABASE '{snap_db}' AS bak")
            cur = con.execute("""
                UPDATE threads AS cur
                SET title = (SELECT bak_t.title FROM bak.threads AS bak_t WHERE bak_t.id = cur.id)
                WHERE EXISTS (
                    SELECT 1 FROM bak.threads AS bak_t
                    WHERE bak_t.id = cur.id AND bak_t.title IS NOT NULL AND bak_t.title <> ''
                )
            """)
            info(f"      ✓ merge 了 {cur.rowcount} 条 title")
            con.execute("DETACH DATABASE bak")
            con.commit()

    # integrity
    with sqlite3.connect(db) as con:
        r = con.execute("PRAGMA integrity_check(1)").fetchone()
    info(f"\nintegrity_check: {r[0] if r else 'unknown'}")

    info("\n==================================================")
    info("✓ finish 完成")
    info("==================================================")
    info(f"请打开 Codex.app 验证 sidebar 是否完整显示所有对话")
    info(f"\n保险快照: {SNAP_ROOT / pre_snapshot}")
    info(f"旧索引备份: {latest_hint_dir}")
    info(f"finish 前 sqlite 备份: {bak}")
    return 0


def _extract_sqlite_from_snapshot(snap_dir: Path) -> Path | None:
    """从快照 payload.tar.gz 里提取 state_5.sqlite 到临时文件。

    v1 snapshot 含 sqlite, v2 不含, 这里兼容两种情况。
    """
    if not snap_dir.exists():
        return None
    payload = snap_dir / "payload.tar.gz"
    if not payload.exists():
        return None
    import tarfile
    target = "tree/state_5.sqlite"
    out = SNAP_ROOT / f".tmp-extracted-state_5-{os.getpid()}.sqlite"
    try:
        with tarfile.open(payload, "r:gz") as tf:
            try:
                m = tf.getmember(target)
            except KeyError:
                return None
            ext_dir = SNAP_ROOT / f".tmp-extract-{os.getpid()}"
            try:
                tf.extract(m, ext_dir, filter="data")
            except TypeError:
                tf.extract(m, ext_dir)
            src = ext_dir / target
            if not src.exists():
                return None
            shutil.move(str(src), str(out))
            shutil.rmtree(ext_dir, ignore_errors=True)
        return out
    except Exception:
        return None


# ---------- main ----------
def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_prep = sub.add_parser("prepare", help="阶段 1: 改 jsonl + 移动旧索引到 trash")
    p_prep.add_argument("--provider", default="openai", help="目标 model_provider, 默认 openai")
    p_fin = sub.add_parser("finish", help="阶段 2: 重启 Codex 后跑这个收尾清扫 (可重复运行)")
    p_fin.add_argument("--title-from", default=None, help="从哪个快照 merge title 列, 默认最近一份 pre-account-switch-*")
    p_fin.add_argument("--provider", default=None, help="目标 model_provider, 不指定时读最近 prepare 的 hint, 都没有则用 'openai'")

    args = ap.parse_args()
    if args.cmd == "prepare":
        return cmd_prepare(args.provider)
    elif args.cmd == "finish":
        return cmd_finish(args.title_from, args.provider)
    else:
        ap.print_help()
        return 2


if __name__ == "__main__":
    sys.exit(main())
