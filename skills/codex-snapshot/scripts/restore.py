#!/usr/bin/env python3
"""restore.py — 恢复 codex 快照。

用法:
  python3 restore.py <version> [--force-while-running] [--yes-i-checked-memories-git]

行为(强约束):
  1. 校验目标版本存在、payload sha256 一致。
  2. 解包到临时 staging 目录。
  3. 先对当前 ~/.codex/ 做 pre-restore-<ts> 自动快照(不可关)。
  4. diff manifest:对 manifest 每个文件比 (size, mtime_ns) → sha256 确证;sqlite 用指纹比较。
  5. 有差异打印前 50 个变动文件 + 总数,要求输入 'yes' 才覆盖。
  6. memories git HEAD 变更 → 额外二次确认(除非 --yes-i-checked-memories-git)。
  7. Codex.app 运行中默认拒绝写 sqlite,需 --force-while-running 才允许。
  8. 按 manifest 文件集做白名单同步(只写 manifest 中的文件,不删除快照外的本地文件)。
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    CODEX_ROOT, SNAP_ROOT,
    confirm_yes, ensure_codex_root, err, info, warn,
    is_codex_running, is_sqlite, memories_git_head,
    sha256_file, sqlite_fingerprint, ts_compact,
)
from snapshot import main as snapshot_main  # noqa: E402


def load_manifest(version: str) -> tuple[Path, dict]:
    target = SNAP_ROOT / version
    mpath = target / "manifest.json"
    if not target.exists() or not mpath.exists():
        err(f"版本 '{version}' 不存在或缺 manifest.json:{target}")
        sys.exit(1)
    return target, json.loads(mpath.read_text(encoding="utf-8"))


def verify_payload(target: Path, manifest: dict) -> None:
    payload = target / "payload.tar.gz"
    if not payload.exists():
        err(f"payload.tar.gz 缺失:{payload}")
        sys.exit(1)
    sha = sha256_file(payload)
    if sha != manifest["payload_sha256"]:
        err(f"payload sha256 不一致! 期望 {manifest['payload_sha256']}, 实际 {sha}")
        sys.exit(1)
    info(f"payload sha256 校验通过 ({manifest['compressed_bytes'] // (1024*1024)} MB)")


def extract_payload(target: Path, staging: Path) -> Path:
    payload = target / "payload.tar.gz"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    info(f"解包到 {staging} ...")
    with tarfile.open(payload, "r:gz") as tf:
        # Python 3.12+ 需 filter='data'
        try:
            tf.extractall(staging, filter="data")
        except TypeError:
            tf.extractall(staging)
    return staging / "tree"


def diff_against_current(manifest: dict, snap_tree: Path) -> tuple[list[str], dict]:
    """返回 (变动文件 rel 列表, 元数据字典)。

    元数据:{sqlite_changes:[], missing_in_current:[], added_outside_snapshot:[]}
    """
    changes: list[str] = []
    sqlite_changes: list[str] = []
    missing_in_current: list[str] = []
    files = manifest.get("files", {})
    for rel, meta in files.items():
        cur = CODEX_ROOT / rel
        if not cur.exists():
            missing_in_current.append(rel)
            changes.append(rel)
            continue
        if is_sqlite(rel):
            # 走指纹比对,sqlite 文件直接 sha256 不稳
            fp_cur = sqlite_fingerprint(cur)
            fp_snap = sqlite_fingerprint(snap_tree / rel)
            if fp_cur != fp_snap:
                sqlite_changes.append(rel)
                changes.append(rel)
            continue
        # 普通文件:size + mtime 快筛 → sha256 确证
        try:
            st = cur.stat()
        except FileNotFoundError:
            missing_in_current.append(rel)
            changes.append(rel)
            continue
        if st.st_size == meta["size"] and st.st_mtime_ns == meta["src_mtime_ns"]:
            continue
        sha_cur = sha256_file(cur)
        if sha_cur != meta["sha256"]:
            changes.append(rel)

    return changes, {
        "sqlite_changes": sqlite_changes,
        "missing_in_current": missing_in_current,
    }


def make_pre_restore_snapshot() -> str:
    pre_ver = f"pre-restore-{ts_compact()}"
    info(f"先做 pre-restore 自动快照 → {pre_ver}")
    # --internal 让 pre-restore 即使 Codex 在运行也照常进行(白名单同步阶段不写 sqlite,
    # 但我们仍要把所有非 sqlite 的当前状态留底);--allow-while-running 显式声明在线 backup
    rc = snapshot_main_with_args([pre_ver, "--internal", "--allow-while-running"])
    if rc != 0:
        err("pre-restore 自动快照失败,中止恢复以保安全")
        sys.exit(1)
    return pre_ver


def snapshot_main_with_args(argv: list[str]) -> int:
    """以 argv 调 snapshot.main()。"""
    saved = sys.argv[:]
    try:
        sys.argv = ["snapshot.py"] + argv
        return snapshot_main()
    finally:
        sys.argv = saved


# v2 起,这些文件即使在旧快照里也永远不写回 ~/.codex/
# Codex 启动会自动从 sessions/*.jsonl 重建 state_5.sqlite/cache/session_index
# 强行覆盖会让 sqlite 跟当前账号 metadata 不匹配,导致 sidebar 隐藏对话
RESTORE_BLACKLIST = {
    "state_5.sqlite",
    "state_5.sqlite-shm",
    "state_5.sqlite-wal",
    "session_index.jsonl",
    "auth.json",  # 登录态,永远不动
}
RESTORE_BLACKLIST_PREFIXES = (
    "cache/",
    "sqlite/",
)


def _is_blacklisted(rel: str) -> bool:
    if rel in RESTORE_BLACKLIST:
        return True
    for pre in RESTORE_BLACKLIST_PREFIXES:
        if rel.startswith(pre):
            return True
    return False


def apply_restore(snap_tree: Path, manifest: dict, force_while_running: bool) -> None:
    """按 manifest 文件集白名单同步到 ~/.codex/。

    - 仅写 manifest 中的文件;不在 manifest 中的本地文件保留(包括 auth.json)。
    - **永远跳过 RESTORE_BLACKLIST**(state_5.sqlite/auth.json/cache/),
      这些由 Codex 启动时自动重建,强行恢复会跟当前账号不匹配。
    - sqlite 文件在 Codex.app 运行中默认拒绝写,加 --force-while-running 才允许。
    """
    running = is_codex_running()
    files = manifest.get("files", {})
    skipped_blacklist = 0
    for rel, meta in files.items():
        if _is_blacklisted(rel):
            skipped_blacklist += 1
            continue
        src = snap_tree / rel
        dst = CODEX_ROOT / rel
        if not src.exists():
            warn(f"snap tree 中缺 {rel},跳过")
            continue
        if is_sqlite(rel) and running and not force_while_running:
            warn(f"Codex.app 运行中,跳过写 {rel}(加 --force-while-running 才覆盖)")
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            # 目标可能是 git pack 中的 0444 只读文件,先放权再写
            if dst.exists():
                try:
                    import os as _os, stat as _stat
                    _os.chmod(dst, dst.stat().st_mode | _stat.S_IWUSR)
                except Exception:
                    pass
            shutil.copy2(src, dst)
        except Exception as e:
            warn(f"写入失败 {rel}: {e}")
    if skipped_blacklist > 0:
        info(f"已跳过 {skipped_blacklist} 个黑名单文件(state_5.sqlite/auth.json/cache/ 等)")
        info("  → 这些会由 Codex 启动自动重建,如果重建后 sidebar 缺对话,请跑 account_switch.py prepare/finish")


def memories_head_check(manifest: dict, skip: bool) -> None:
    snap_head = manifest.get("memories_git_head")
    cur_head = memories_git_head()
    if snap_head and cur_head and snap_head != cur_head:
        info("==================================================")
        info(f"⚠️  memories/.git HEAD 将变化:")
        info(f"    当前: {cur_head}")
        info(f"    恢复后: {snap_head}")
        info("==================================================")
        if skip:
            warn("--yes-i-checked-memories-git 已传,跳过 memories git HEAD 确认")
            return
        if not confirm_yes("memories/.git HEAD 将被覆盖,是否继续? 输入 yes 继续:"):
            err("已取消(memories git HEAD 二次确认未通过)")
            sys.exit(1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("version")
    ap.add_argument("--force-while-running", action="store_true",
                    help="Codex.app 运行中仍然覆盖 sqlite(警告:可能 corruption)")
    ap.add_argument("--yes-i-checked-memories-git", action="store_true",
                    help="跳过 memories git HEAD 变更的二次确认")
    args = ap.parse_args()

    ensure_codex_root()
    version = args.version.strip()
    target, manifest = load_manifest(version)

    info(f"准备恢复版本 {version} (创建于 {manifest['created_at']}, Codex.app {manifest.get('codex_app_version','?')})")
    if is_codex_running():
        warn("Codex.app 当前正在运行")
        if not args.force_while_running:
            warn("默认会跳过对 sqlite 的写入(非 sqlite 文件正常恢复)。建议退出 Codex 后重跑;或加 --force-while-running 强制。")

    verify_payload(target, manifest)

    # 解包到 staging
    staging = SNAP_ROOT / f".staging-restore-{ts_compact()}"
    snap_tree = extract_payload(target, staging)

    try:
        # 强制 pre-restore 快照
        pre_ver = make_pre_restore_snapshot()
        info(f"pre-restore 快照已落地:{SNAP_ROOT / pre_ver}")

        # 变动检测
        info("检测当前 ~/.codex/ 相对快照的变动...")
        changes, extra = diff_against_current(manifest, snap_tree)
        if not changes:
            info("✓ 无变动,直接恢复(按 manifest 同步,保持 sha256 一致)")
        else:
            info("==================================================")
            info(f"检测到 {len(changes)} 个文件变动:")
            for rel in changes[:50]:
                info(f"  • {rel}")
            if len(changes) > 50:
                info(f"  ...还有 {len(changes) - 50} 个未列出")
            if extra["sqlite_changes"]:
                info(f"其中 sqlite 指纹差异:{len(extra['sqlite_changes'])} 个")
            if extra["missing_in_current"]:
                info(f"当前缺失(快照有):{len(extra['missing_in_current'])} 个")
            info("==================================================")
            info("注意:已为你做了 pre-restore 快照,本次覆盖可随时反向恢复。")
            if not confirm_yes("是否覆盖? 严格输入 yes 继续(其他任意输入将取消):"):
                err("已取消(用户未确认)。当前 ~/.codex/ 未做任何修改。")
                return 1

        # memories git HEAD 二次确认
        memories_head_check(manifest, args.yes_i_checked_memories_git)

        # 应用恢复
        info("开始白名单同步(仅写 manifest 中的文件,不删除快照外的本地文件)...")
        apply_restore(snap_tree, manifest, args.force_while_running)
        info(f"✓ 恢复完成 → {CODEX_ROOT}")
        info(f"  如需反向恢复,运行:python3 restore.py {pre_ver}")
        info("提示:auth.json 不在快照中。请用账号 B(或新账号)在 Codex.app 内重新登录。")
        return 0
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
