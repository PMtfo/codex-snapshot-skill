#!/usr/bin/env python3
"""Final Codex import orchestrator for ccc-syn-skill.

Official path only: build Claude-format sessions, then call Codex's
externalAgentConfig/import RPC so conversations appear in the Codex sidebar.

Supported sources:
- cursor: Cursor transcripts converted to Claude JSONL first.
- claude: Native Claude Code JSONL sessions.
- both: Import both sources into the selected Codex project cwd.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

HOME = Path.home()
ROOT = Path(__file__).resolve().parents[1]
CODEX_IMPORT_DIR = ROOT / "codex-import"
CODEX = Path(os.environ.get("CODEX_HOME", HOME / ".codex"))
IMPORTS = CODEX / "external_agent_session_imports.json"
STATE_DB = CODEX / "state_5.sqlite"
CURSOR_SIDECAR = CODEX / "cursor-claude-bridge-map.json"
IMPORT_MAP = CODEX / "ccc-syn-import-map.json"
CONVERTER = CODEX_IMPORT_DIR / "codex-planB-claude-bridge.py"
RPC = CODEX_IMPORT_DIR / "codex-appserver-rpc.py"
CLAUDE_PROJECTS = HOME / ".claude" / "projects"
STAMP = time.strftime("%Y%m%d-%H%M%S")
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
NOISE_TITLE_RE = re.compile(
    r"^(?:\d+|ok|收到|你好\d*|hello\d*|hi\d*|test\d*|测试\d*|ping\d*|"
    r"请回复\s*ok|回复\s*ok|只回复\s*ok|只输出\s*ok|回复一个字[:：]?\s*好|"
    r"测网速|测速|网络测试|连接性测试|连通性测试|测试连接|测试网络|测试一下|试一下)$",
    re.I,
)
NOISE_KEYWORD_RE = re.compile(r"(测网速|测速|网络测试|连接性测试|连通性测试|测试连接|测试网络|ping\s*测试)", re.I)
NOISE_CONTEXT_RE = re.compile(r"(synctest|只回复\s*ok|只输出\s*ok|回复一个字|aws_bedrock_ok|^insights\s*/insights$)", re.I)

sys.path.insert(0, str(HOME / ".cursor" / "agent-shared" / "skills" / "ccc-syn-skill" / "scripts"))
import codex_import as ci  # noqa: E402

CODEX_CWD = ci._codex_project_cwd()
TARGET_DIR = HOME / ".claude" / "projects" / ("-" + CODEX_CWD.strip("/").replace("/", "-"))


@dataclass(frozen=True)
class ImportSession:
    source: str
    source_key: str
    path: Path
    title: str
    cwd: str
    updated_ms: int


def configure_codex_cwd(cwd: str | None) -> None:
    """Set the target Codex workspace cwd for this process and child converter."""
    global CODEX_CWD, TARGET_DIR
    if cwd:
        CODEX_CWD = str(Path(cwd).expanduser())
        os.environ["CCC_CODEX_PROJECT_CWD"] = CODEX_CWD
    else:
        CODEX_CWD = ci._codex_project_cwd()
    TARGET_DIR = HOME / ".claude" / "projects" / ("-" + CODEX_CWD.strip("/").replace("/", "-"))


def configure_cursor_bridge_paths(*, target_dir: Path, sidecar: Path) -> None:
    """Point Cursor conversion at an explicit bridge location."""
    global TARGET_DIR, CURSOR_SIDECAR
    TARGET_DIR = target_dir
    CURSOR_SIDECAR = sidecar


def default_cursor_bridge_target_dir() -> Path:
    return HOME / ".claude" / "projects" / ("-" + CODEX_CWD.strip("/").replace("/", "-"))


def ensure_quit() -> None:
    r = subprocess.run(["pgrep", "-f", "Codex.app/Contents/MacOS/Codex"], capture_output=True, text=True)
    r2 = subprocess.run(["pgrep", "-f", "Resources/codex app-server"], capture_output=True, text=True)
    if r.stdout.strip() or r2.stdout.strip():
        print("ABORT: Codex still running. Cmd+Q first.")
        sys.exit(2)


def backup() -> None:
    if STATE_DB.exists():
        with sqlite3.connect(STATE_DB) as s, sqlite3.connect(STATE_DB.with_name(STATE_DB.name + f".bak-runB-{STAMP}")) as d:
            s.backup(d)
    if IMPORTS.exists():
        shutil.copy2(IMPORTS, IMPORTS.with_name(IMPORTS.name + f".bak-runB-{STAMP}"))
    if IMPORT_MAP.exists():
        shutil.copy2(IMPORT_MAP, IMPORT_MAP.with_name(IMPORT_MAP.name + f".bak-runB-{STAMP}"))


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_iso_ms(value: str | None, fallback_ns: int = 0) -> int:
    text = str(value or "").strip()
    if text:
        try:
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            return int(datetime.fromisoformat(text).timestamp() * 1000)
        except ValueError:
            pass
    if fallback_ns:
        return int(fallback_ns / 1_000_000)
    return int(time.time() * 1000)


def parse_since(value: str | None) -> int | None:
    if not value:
        return None
    text = value.strip().lower()
    now = datetime.now(timezone.utc)
    m = re.fullmatch(r"(\d+)([dhwm])", text)
    if m:
        amount = int(m.group(1))
        unit = m.group(2)
        if unit == "d":
            delta = timedelta(days=amount)
        elif unit == "h":
            delta = timedelta(hours=amount)
        elif unit == "w":
            delta = timedelta(weeks=amount)
        else:
            delta = timedelta(days=30 * amount)
        return int((now - delta).timestamp() * 1000)
    return parse_date_ms(value)


def parse_date_ms(value: str | None) -> int | None:
    if not value:
        return None
    text = value.strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            dt = datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
        else:
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except ValueError:
        raise SystemExit(f"Invalid date/since value: {value}")


def imported_source_paths() -> set[str]:
    data = load_json(IMPORTS, {"records": []})
    paths: set[str] = set()
    for rec in data.get("records", []):
        if isinstance(rec, dict) and rec.get("source_path"):
            paths.add(str(rec["source_path"]))
    return paths


def import_map_records() -> list[dict[str, Any]]:
    data = load_json(IMPORT_MAP, {"records": []})
    records = data.get("records", [])
    return records if isinstance(records, list) else []


def reset(source: str) -> None:
    ensure_quit()
    backup()

    data = load_json(IMPORTS, {"records": []})
    recs = data.get("records", []) if isinstance(data.get("records"), list) else []
    tracked = import_map_records()
    tracked_paths = {str(r.get("source_path")) for r in tracked if r.get("source_path")}
    tracked_thread_ids = {str(r.get("imported_thread_id")) for r in tracked if r.get("imported_thread_id")}

    def match_rec(rec: dict[str, Any]) -> bool:
        path = str(rec.get("source_path", ""))
        if source in {"cursor", "both"} and path.startswith(str(TARGET_DIR)):
            return True
        if source in {"claude", "both"} and path in tracked_paths:
            return True
        return False

    bridge_recs = [r for r in recs if isinstance(r, dict) and match_rec(r)]
    bridge_thread_ids = {str(r.get("imported_thread_id")) for r in bridge_recs if r.get("imported_thread_id")}
    bridge_thread_ids |= tracked_thread_ids

    rollout_paths: list[str] = []
    removed_rows = 0
    if STATE_DB.exists() and bridge_thread_ids:
        with sqlite3.connect(STATE_DB) as con:
            for tid in bridge_thread_ids:
                row = con.execute("select rollout_path from threads where id=?", (tid,)).fetchone()
                if row and row[0]:
                    rollout_paths.append(row[0])
            before = con.total_changes
            con.executemany("delete from threads where id=?", [(tid,) for tid in bridge_thread_ids])
            con.commit()
            con.execute("pragma wal_checkpoint(full)")
            removed_rows = con.total_changes - before
    for rp in rollout_paths:
        try:
            Path(rp).unlink()
        except OSError:
            pass

    data["records"] = [r for r in recs if not (isinstance(r, dict) and match_rec(r))]
    write_json(IMPORTS, data)

    sidx = CODEX / "session_index.jsonl"
    if sidx.exists() and bridge_thread_ids:
        keep: list[str] = []
        for line in sidx.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                if str(json.loads(line).get("id")) in bridge_thread_ids:
                    continue
            except json.JSONDecodeError:
                pass
            keep.append(line)
        sidx.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")

    if source in {"cursor", "both"}:
        if TARGET_DIR.exists():
            shutil.rmtree(TARGET_DIR)
        if CURSOR_SIDECAR.exists():
            CURSOR_SIDECAR.unlink()

    if IMPORT_MAP.exists():
        remaining = [r for r in tracked if str(r.get("imported_thread_id")) not in bridge_thread_ids]
        write_json(IMPORT_MAP, {"records": remaining})

    print(f"reset: source={source} records={len(bridge_recs)} removed_thread_rows={removed_rows} removed_rollouts={len(rollout_paths)}")


def convert_cursor() -> None:
    env = os.environ.copy()
    env["CCC_CURSOR_BRIDGE_TARGET_DIR"] = str(TARGET_DIR)
    env["CCC_CURSOR_BRIDGE_SIDECAR"] = str(CURSOR_SIDECAR)
    subprocess.run([sys.executable, str(CONVERTER)], check=True, env=env)


def read_jsonl(path: Path, max_rows: int | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle):
                if max_rows is not None and index >= max_rows:
                    break
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    rows.append(item)
    except OSError:
        return []
    return rows


def extract_text_parts(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") == "text" and isinstance(item.get("text"), str):
                    parts.append(item["text"])
        return parts
    if isinstance(content, dict):
        return extract_text_parts(content.get("content"))
    return []


def clean_one_line(text: str, max_chars: int = 120) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars].rstrip() or "(untitled)"


def is_noise_session(session: ImportSession) -> bool:
    compact = re.sub(r"\s+", "", session.title or "").strip()
    normalized = re.sub(r"[\s，。；;:：,.!！?？、\"'`]+", "", session.title or "").strip()
    if not compact:
        return True
    if NOISE_TITLE_RE.fullmatch(compact):
        return True
    if NOISE_TITLE_RE.fullmatch(normalized):
        return True
    if NOISE_KEYWORD_RE.search(session.title or ""):
        return True
    if NOISE_CONTEXT_RE.search(session.title or ""):
        return True
    return False


def claude_title(path: Path, rows: list[dict[str, Any]]) -> str:
    for row in rows:
        if row.get("type") == "ai-title" and row.get("aiTitle"):
            return clean_one_line(str(row["aiTitle"]))
    for row in rows:
        if row.get("type") != "user":
            continue
        msg = row.get("message")
        if isinstance(msg, dict):
            text = "\n".join(extract_text_parts(msg.get("content"))).strip()
            if text:
                return clean_one_line(text)
    return path.stem


def claude_updated_ms(path: Path, rows: list[dict[str, Any]]) -> int:
    timestamps = [str(row.get("timestamp")) for row in rows if row.get("timestamp")]
    if timestamps:
        return parse_iso_ms(timestamps[-1], path.stat().st_mtime_ns)
    return int(path.stat().st_mtime_ns / 1_000_000)


def iter_claude_sessions() -> Iterable[ImportSession]:
    if not CLAUDE_PROJECTS.exists():
        return []
    sessions: list[ImportSession] = []
    cursor_bridge_dirs = {TARGET_DIR, default_cursor_bridge_target_dir()}
    for path in sorted(CLAUDE_PROJECTS.glob("*/*.jsonl")):
        if any(bridge_dir in path.parents for bridge_dir in cursor_bridge_dirs):
            continue
        if not UUID_RE.match(path.stem):
            continue
        rows = read_jsonl(path, max_rows=120)
        if not rows:
            continue
        sessions.append(
            ImportSession(
                source="claude",
                source_key=str(path),
                path=path,
                title=claude_title(path, rows),
                cwd=CODEX_CWD,
                updated_ms=claude_updated_ms(path, rows),
            )
        )
    return sorted(sessions, key=lambda item: item.updated_ms)


def cursor_sessions() -> list[ImportSession]:
    bridge = load_json(CURSOR_SIDECAR, {})
    sessions: list[ImportSession] = []
    ordered = sorted(bridge.values(), key=lambda item: int(item.get("source_modified_ns") or 0))
    for info in ordered:
        p = Path(info.get("claude_file", ""))
        if not p.exists():
            continue
        title = str(info.get("source_title") or "").strip() or "(untitled)"
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                d = json.loads(line)
                if d.get("type") == "ai-title" and d.get("aiTitle"):
                    title = str(d["aiTitle"])
            except json.JSONDecodeError:
                continue
        sessions.append(
            ImportSession(
                source="cursor",
                source_key=str(info.get("source_uuid") or info.get("source_path") or p),
                path=p,
                title=title[:120],
                cwd=CODEX_CWD,
                updated_ms=int(info.get("source_modified_ns") or 0) // 1_000_000,
            )
        )
    return sessions


def source_sessions(source: str) -> list[ImportSession]:
    sessions: list[ImportSession] = []
    if source in {"cursor", "both"}:
        sessions.extend(cursor_sessions())
    if source in {"claude", "both"}:
        sessions.extend(iter_claude_sessions())
    return sorted(sessions, key=lambda item: item.updated_ms)


def apply_filters(
    sessions: list[ImportSession],
    *,
    incremental: bool,
    since_ms: int | None,
    from_ms: int | None,
    to_ms: int | None,
    title_query: str | None,
) -> tuple[list[ImportSession], dict[str, int]]:
    imported = imported_source_paths()
    title_query_cf = title_query.casefold().strip() if title_query else ""
    stats = {"seen": len(sessions), "skipped_imported": 0, "skipped_time": 0, "skipped_title": 0, "skipped_noise": 0}
    output: list[ImportSession] = []
    lower_bound = since_ms if since_ms is not None else from_ms
    for session in sessions:
        if is_noise_session(session):
            stats["skipped_noise"] += 1
            continue
        if incremental and str(session.path) in imported:
            stats["skipped_imported"] += 1
            continue
        if lower_bound is not None and session.updated_ms < lower_bound:
            stats["skipped_time"] += 1
            continue
        if to_ms is not None and session.updated_ms > to_ms:
            stats["skipped_time"] += 1
            continue
        if title_query_cf and title_query_cf not in session.title.casefold():
            stats["skipped_title"] += 1
            continue
        output.append(session)
    return output, stats


def build_items(sessions: list[ImportSession]) -> list[dict[str, Any]]:
    return [{"path": str(s.path), "cwd": s.cwd, "title": s.title[:120]} for s in sessions]


def write_import_map(sessions: list[ImportSession]) -> None:
    existing = import_map_records()
    by_path = {str(r.get("source_path")): r for r in existing if r.get("source_path")}
    imports = load_json(IMPORTS, {"records": []})
    records = imports.get("records", []) if isinstance(imports.get("records"), list) else []
    imported_by_path = {str(r.get("source_path")): r for r in records if isinstance(r, dict) and r.get("source_path")}
    for session in sessions:
        rec = imported_by_path.get(str(session.path))
        if not rec:
            continue
        by_path[str(session.path)] = {
            "source": session.source,
            "source_key": session.source_key,
            "source_path": str(session.path),
            "title": session.title,
            "updated_ms": session.updated_ms,
            "imported_thread_id": rec.get("imported_thread_id"),
            "codex_cwd": CODEX_CWD,
        }
    write_json(IMPORT_MAP, {"records": list(by_path.values())})


def do_import(sessions: list[ImportSession], *, batch: int = 40, dry_run: bool = False) -> None:
    if dry_run:
        print(f"dry_run_import_count={len(sessions)}")
        for session in sessions[:30]:
            print(f"[dry-import] {session.source} {datetime.fromtimestamp(session.updated_ms/1000, tz=timezone.utc).isoformat()} title={session.title} path={session.path}")
        if len(sessions) > 30:
            print(f"[dry-import] ... {len(sessions) - 30} more")
        return

    ensure_quit()
    print(f"importing {len(sessions)} sessions in batches of {batch}")
    total_ok = 0
    total_fail = 0
    for i in range(0, len(sessions), batch):
        chunk = sessions[i:i + batch]
        items = [{"itemType": "SESSIONS", "description": f"ccc-syn import batch {i//batch+1}", "cwd": None, "details": {"plugins": [], "sessions": build_items(chunk)}}]
        items_file = CODEX_IMPORT_DIR / f"codex-import-items-batch-{i//batch+1}.json"
        items_file.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
        out = subprocess.run([sys.executable, str(RPC), "import", str(items_file)], capture_output=True, text=True)
        ok = out.stdout.count('"target"')
        fail = out.stdout.count('"failureStage"')
        total_ok += ok
        total_fail += fail
        print(f"  batch {i//batch+1}: ok~{ok} fail~{fail}")
        items_file.unlink(missing_ok=True)
    print(f"import done ok~{total_ok} fail~{total_fail}")
    write_import_map(sessions)
    sync_session_index_titles()


def sync_session_index_titles() -> None:
    sidx = CODEX / "session_index.jsonl"
    if not sidx.exists() or not STATE_DB.exists():
        return
    with sqlite3.connect(STATE_DB) as con:
        rows = con.execute(
            "select id, title, updated_at_ms, recency_at_ms from threads where cwd=? and archived=0",
            (CODEX_CWD,),
        ).fetchall()
    title_by_id = {thread_id: (title, updated_at_ms, recency_at_ms) for thread_id, title, updated_at_ms, recency_at_ms in rows}
    changed = 0
    output: list[str] = []
    for line in sidx.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            output.append(line)
            continue
        thread_id = row.get("id")
        if thread_id in title_by_id:
            title, updated_at_ms, recency_at_ms = title_by_id[thread_id]
            if row.get("thread_name") != title:
                row["thread_name"] = title
                changed += 1
            if updated_at_ms:
                row["updated_at_ms"] = updated_at_ms
            if recency_at_ms:
                row["recency_at_ms"] = recency_at_ms
        output.append(json.dumps(row, ensure_ascii=False))
    sidx.write_text("\n".join(output) + ("\n" if output else ""), encoding="utf-8")
    print(f"sync_session_index_titles={changed}")


def verify() -> None:
    if not STATE_DB.exists():
        print("no state db")
        return
    with sqlite3.connect(STATE_DB) as con:
        n = con.execute("select count(*) from threads where cwd=? and archived=0", (CODEX_CWD,)).fetchone()[0]
    print(f"verify: threads under {CODEX_CWD} = {n}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import Cursor/Claude Code conversations into Codex via official RPC.")
    parser.add_argument("action", choices=["all", "reset", "convert", "import", "verify", "dry-run"], nargs="?", default="dry-run")
    parser.add_argument("--source", choices=["cursor", "claude", "both"], default="cursor")
    parser.add_argument("--incremental", action="store_true", help="Skip sessions whose source_path already exists in Codex import ledger.")
    parser.add_argument("--since", help="Import sessions updated since a relative window (7d, 24h, 2w) or date.")
    parser.add_argument("--from-date", help="Import sessions updated at/after this date or ISO timestamp.")
    parser.add_argument("--to-date", help="Import sessions updated at/before this date or ISO timestamp.")
    parser.add_argument("--title", help="Only import sessions whose title contains this text.")
    parser.add_argument("--batch", type=int, default=40)
    parser.add_argument(
        "--codex-cwd",
        help="Target Codex workspace cwd where imported conversations should appear. Ask the user to confirm this path is a Codex workspace before real import.",
    )
    args = parser.parse_args(argv)
    configure_codex_cwd(args.codex_cwd)
    dry_run = args.action == "dry-run"
    temp_bridge: tempfile.TemporaryDirectory[str] | None = None
    if dry_run and args.source in {"cursor", "both"}:
        temp_bridge = tempfile.TemporaryDirectory(prefix="ccc-syn-dry-run-")
        temp_root = Path(temp_bridge.name)
        configure_cursor_bridge_paths(
            target_dir=temp_root / "claude-projects" / TARGET_DIR.name,
            sidecar=temp_root / "cursor-claude-bridge-map.json",
        )

    try:
        since_ms = parse_since(args.since)
        from_ms = parse_date_ms(args.from_date)
        to_ms = parse_date_ms(args.to_date)

        if args.action == "reset":
            reset(args.source)
            return 0
        if args.action == "convert":
            if args.source in {"cursor", "both"}:
                convert_cursor()
            return 0
        if args.action == "verify":
            verify()
            return 0

        if args.action == "all":
            reset(args.source)
            if args.source in {"cursor", "both"}:
                convert_cursor()
            incremental = False
            dry_run = False
        else:
            if args.source in {"cursor", "both"}:
                convert_cursor()
            incremental = args.incremental or args.action == "import"

        sessions, stats = apply_filters(
            source_sessions(args.source),
            incremental=incremental,
            since_ms=since_ms,
            from_ms=from_ms,
            to_ms=to_ms,
            title_query=args.title,
        )
        print(
            "selection: "
            f"source={args.source} seen={stats['seen']} selected={len(sessions)} "
            f"skipped_imported={stats['skipped_imported']} skipped_time={stats['skipped_time']} "
            f"skipped_title={stats['skipped_title']} skipped_noise={stats['skipped_noise']} "
            f"incremental={str(incremental).lower()} codex_cwd={CODEX_CWD}"
        )
        do_import(sessions, batch=max(args.batch, 1), dry_run=dry_run)
        if not dry_run:
            verify()
        return 0
    finally:
        if temp_bridge is not None:
            temp_bridge.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
