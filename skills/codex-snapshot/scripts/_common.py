"""codex-snapshot 共用工具。

路径常量、排除清单、sqlite 在线 backup、sha256、manifest 读写、Codex.app 状态探测。
所有脚本都从这里导入,避免重复实现。
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import plistlib
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Iterable

# ---------- 路径常量 ----------
HOME = Path.home()
CODEX_ROOT = HOME / ".codex"
SNAP_ROOT = HOME / ".codex-snapshots"
TRASH_ROOT = SNAP_ROOT / ".trash"
INDEX_PATH = SNAP_ROOT / "INDEX.json"
SKILL_ROOT = Path(__file__).resolve().parent.parent
EXCLUDE_FILE = SKILL_ROOT / "scripts" / "exclude.txt"
CODEX_APP_PLIST = Path("/Applications/Codex.app/Contents/Info.plist")

# 已知忽略路径(只记录在 manifest.known_ignored 中,作提示用,不快照)
KNOWN_IGNORED_DIRS = [
    HOME / "Library" / "Application Support" / "com.openai.codex",
    HOME / "Library" / "Caches" / "com.openai.codex",
    HOME / "Library" / "Logs" / "com.openai.codex",
    HOME / "Library" / "HTTPStorages" / "com.openai.codex",
]

VERSION_RE = re.compile(r"^[\w.-]+$")

# ---------- 时间 ----------
def now_local() -> datetime:
    return datetime.now().astimezone()

def ts_compact() -> str:
    # 精度到毫秒,避免同一秒内多次调用 pre-restore-<ts> 撞名
    t = now_local()
    return t.strftime("%Y%m%d-%H%M%S") + f"-{t.microsecond // 1000:03d}"

def ts_iso() -> str:
    return now_local().isoformat(timespec="seconds")

# ---------- 日志 ----------
def info(msg: str) -> None:
    print(f"[codex-snapshot] {msg}", flush=True)

def warn(msg: str) -> None:
    print(f"[codex-snapshot][WARN] {msg}", file=sys.stderr, flush=True)

def err(msg: str) -> None:
    print(f"[codex-snapshot][ERR] {msg}", file=sys.stderr, flush=True)

# ---------- 版本号校验 ----------
def validate_version(v: str) -> str:
    v = v.strip()
    if not v:
        err("版本号不能为空")
        sys.exit(2)
    if not VERSION_RE.match(v):
        err(f"版本号 '{v}' 含非法字符,只允许字母数字 . _ -")
        sys.exit(2)
    if v.startswith(".") or v.startswith("pre-restore-") and "internal" not in sys.argv:
        # pre-restore- 前缀保留给内部自动快照,用户不要手动用
        pass
    return v

# ---------- 排除清单 ----------
def load_exclude_patterns() -> list[str]:
    patterns: list[str] = []
    if not EXCLUDE_FILE.exists():
        warn(f"exclude.txt 不存在:{EXCLUDE_FILE},默认空清单")
        return patterns
    for line in EXCLUDE_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(line)
    return patterns

def is_excluded(rel_path: str, patterns: list[str]) -> bool:
    """rel_path 是相对 CODEX_ROOT 的 posix 路径,如 'auth.json' 或 'plugins/foo.bin'。"""
    parts = rel_path.split("/")
    for pat in patterns:
        pat_norm = pat.rstrip("/")
        # 完整路径匹配
        if fnmatch.fnmatch(rel_path, pat_norm):
            return True
        # 任一段匹配(用于目录排除)
        for seg in parts:
            if fnmatch.fnmatch(seg, pat_norm):
                return True
    return False

# ---------- Codex.app 状态 ----------
def is_codex_running() -> bool:
    try:
        r = subprocess.run(
            ["pgrep", "-f", "Codex.app"],
            capture_output=True, text=True, timeout=5
        )
        return r.returncode == 0 and bool(r.stdout.strip())
    except Exception:
        return False

def codex_app_version() -> str:
    if not CODEX_APP_PLIST.exists():
        return "unknown"
    try:
        with CODEX_APP_PLIST.open("rb") as f:
            data = plistlib.load(f)
        return data.get("CFBundleShortVersionString", "unknown")
    except Exception:
        return "unknown"

# ---------- SHA256 ----------
def sha256_file(p: Path, bufsize: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        while True:
            chunk = f.read(bufsize)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()

# ---------- SQLite 在线快照 ----------
def sqlite_safe_copy(src: Path, dst: Path, retries: int = 3, wait_s: float = 2.0) -> None:
    """用 sqlite3.backup API 拷贝 sqlite 文件,WAL 模式下对在线写入安全。

    失败重试 retries 次,每次间隔 wait_s 秒。仍失败抛 RuntimeError。
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30) as src_conn, \
                 sqlite3.connect(dst) as dst_conn:
                src_conn.backup(dst_conn)
            return
        except sqlite3.Error as e:
            last_err = e
            warn(f"sqlite backup {src.name} 第 {attempt}/{retries} 次失败:{e}")
            time.sleep(wait_s)
    raise RuntimeError(f"sqlite backup 失败 {src}: {last_err}")

def sqlite_fingerprint(p: Path) -> dict:
    """对在线 sqlite 计算指纹,避免 WAL 干扰直接 sha256 不稳。

    返回 dict: {tables: {name: rowcount}, integrity: 'ok'/...}
    """
    fp = {"tables": {}, "integrity": "skipped"}
    if not p.exists():
        return fp
    try:
        with sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=10) as c:
            cur = c.cursor()
            cur.execute("PRAGMA integrity_check(1)")
            r = cur.fetchone()
            fp["integrity"] = r[0] if r else "unknown"
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
            tables = [row[0] for row in cur.fetchall()]
            for t in tables:
                try:
                    cur.execute(f'SELECT count(*) FROM "{t}"')
                    cnt = cur.fetchone()[0]
                except sqlite3.Error:
                    cnt = -1
                fp["tables"][t] = cnt
    except sqlite3.Error as e:
        fp["error"] = str(e)
    return fp

# ---------- INDEX.json ----------
def load_index() -> dict:
    if not INDEX_PATH.exists():
        return {"versions": {}}
    try:
        return json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        warn(f"INDEX.json 解析失败,重建:{e}")
        return {"versions": {}}

def save_index(idx: dict) -> None:
    SNAP_ROOT.mkdir(parents=True, exist_ok=True)
    tmp = INDEX_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(INDEX_PATH)

def upsert_index_entry(version: str, entry: dict) -> None:
    idx = load_index()
    idx.setdefault("versions", {})[version] = entry
    save_index(idx)

def remove_index_entry(version: str) -> None:
    idx = load_index()
    idx.setdefault("versions", {}).pop(version, None)
    save_index(idx)

# ---------- 遍历 + 收集 ----------
SQLITE_SUFFIXES = (".sqlite", ".db")

def walk_codex_files(patterns: list[str]) -> Iterable[tuple[Path, str]]:
    """生成 (绝对路径, 相对 CODEX_ROOT 的 posix 路径)。已过滤 exclude 与 SNAP_ROOT 自身。"""
    if not CODEX_ROOT.exists():
        return
    for root, dirs, files in os.walk(CODEX_ROOT, followlinks=False):
        rootp = Path(root)
        rel_root = rootp.relative_to(CODEX_ROOT).as_posix()
        if rel_root == ".":
            rel_root = ""
        # 目录排除(in-place 修改 dirs 跳过子树)
        keep_dirs = []
        for d in dirs:
            rel_d = f"{rel_root}/{d}" if rel_root else d
            if is_excluded(rel_d, patterns):
                continue
            keep_dirs.append(d)
        dirs[:] = keep_dirs
        for fn in files:
            rel_f = f"{rel_root}/{fn}" if rel_root else fn
            if is_excluded(rel_f, patterns):
                continue
            yield rootp / fn, rel_f

def is_sqlite(rel_path: str) -> bool:
    return rel_path.endswith(SQLITE_SUFFIXES)

# ---------- 软删除 30 天清理 ----------
def gc_trash(days: int = 30) -> None:
    if not TRASH_ROOT.exists():
        return
    cutoff = time.time() - days * 86400
    for child in TRASH_ROOT.iterdir():
        try:
            if child.stat().st_mtime < cutoff:
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
                info(f"已清理超过 {days} 天的 trash 项:{child.name}")
        except Exception as e:
            warn(f"清理 trash 失败 {child}: {e}")

# ---------- memories git ----------
def memories_git_head() -> str | None:
    g = CODEX_ROOT / "memories" / ".git"
    if not g.exists():
        return None
    try:
        r = subprocess.run(
            ["git", "-C", str(CODEX_ROOT / "memories"), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return None

# ---------- 输入确认 ----------
def confirm_yes(prompt: str) -> bool:
    """严格等于 'yes' 才返回 True,其他一律 False。"""
    try:
        ans = input(prompt)
    except EOFError:
        return False
    return ans.strip() == "yes"

# ---------- 自检 ----------
def ensure_codex_root() -> None:
    if not CODEX_ROOT.exists():
        err(f"~/.codex 不存在,无 Codex 数据可处理:{CODEX_ROOT}")
        sys.exit(1)
