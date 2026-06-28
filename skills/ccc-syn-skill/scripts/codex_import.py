#!/usr/bin/env python3
"""Import Cursor Agent transcripts into Codex Desktop's local session folder."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import shutil
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


HOME = Path.home()
CODEX_HOME = Path(os.environ.get("CODEX_HOME", HOME / ".codex"))
CURSOR_PROJECTS_DIR = Path(os.environ.get("CCC_CURSOR_PROJECTS_DIR", HOME / ".cursor" / "projects"))
CURSOR_STATE_DB = Path(
    os.environ.get(
        "CCC_CURSOR_STATE_DB",
        HOME / "Library/Application Support/Cursor/User/globalStorage/state.vscdb",
    )
)
DEFAULT_CODEX_PROJECT_CWD = Path(os.environ.get("CCC_CODEX_PROJECT_CWD", HOME / "Desktop" / "Codex"))
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


@dataclass(frozen=True)
class CursorTranscript:
    uuid: str
    path: Path
    project: str
    title: str
    created_at: str
    updated_at: str
    modified_ns: int
    sha256: str


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _unix_seconds() -> int:
    return int(time.time())


def _uuid7_like() -> str:
    """Return a UUIDv7-shaped id compatible with Codex session filenames."""
    ts_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rand_a = random.getrandbits(12)
    rand_b = random.getrandbits(62)
    value = (ts_ms << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
    text = f"{value:032x}"
    return f"{text[:8]}-{text[8:12]}-{text[12:16]}-{text[16:20]}-{text[20:]}"


def _single_line(text: str, max_chars: int = 180) -> str:
    line = re.sub(r"\s+", " ", text).strip()
    if len(line) <= max_chars:
        return line
    return line[: max_chars - 1].rstrip() + "…"


def _format_timestamp(value: int | float | str | None) -> str:
    if value in (None, ""):
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number > 10_000_000_000:
        number = number / 1000
    try:
        return datetime.fromtimestamp(number, tz=timezone.utc).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        )
    except (OSError, OverflowError, ValueError):
        return str(value)


def _read_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        return []
    return rows


def _extract_text_parts(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                item_type = item.get("type")
                if item_type == "text" and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif item_type == "tool_use":
                    name = item.get("name") or "unknown"
                    tool_input = item.get("input")
                    try:
                        summary = json.dumps(tool_input, ensure_ascii=False, sort_keys=True)
                    except TypeError:
                        summary = repr(tool_input)
                    parts.append(f"[tool_use] {name}: {_single_line(summary, 600)}")
                elif item_type == "tool_result":
                    result_text = "\n".join(_extract_text_parts(item.get("content"))).strip()
                    parts.append(f"[tool_result] {_single_line(result_text, 800)}" if result_text else "[tool_result] (empty)")
        return parts
    if isinstance(content, dict):
        return _extract_text_parts(content.get("content"))
    return []


def _message_text(row: dict[str, Any]) -> str:
    message = row.get("message")
    if isinstance(message, dict):
        return "\n\n".join(part for part in _extract_text_parts(message.get("content")) if part.strip()).strip()
    return ""


def _clean_title(text: str) -> str:
    match = re.search(r"<user_query>\s*(.*?)\s*</user_query>", text, re.S)
    if match:
        text = match.group(1)
    text = re.sub(r"<[^>]+>", " ", text)
    return _single_line(text, 180)


def _content_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_cursor_titles() -> dict[str, dict[str, Any]]:
    if not CURSOR_STATE_DB.exists():
        return {}
    records: dict[str, dict[str, Any]] = {}
    try:
        uri = f"file:{CURSOR_STATE_DB}?mode=ro&immutable=1"
        with sqlite3.connect(uri, uri=True) as con:
            rows = con.execute("SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'")
            for key, value in rows:
                uuid = str(key).split("composerData:", 1)[-1]
                if not UUID_RE.match(uuid):
                    continue
                text = value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(data, dict):
                    records[uuid] = data
    except sqlite3.Error:
        return {}
    return records


def _iter_cursor_transcripts(uuid_filter: str | None = None) -> Iterable[CursorTranscript]:
    titles = _load_cursor_titles()
    if not CURSOR_PROJECTS_DIR.exists():
        return
    for project_dir in sorted(CURSOR_PROJECTS_DIR.iterdir()):
        transcripts_dir = project_dir / "agent-transcripts"
        if not transcripts_dir.is_dir():
            continue
        for session_dir in sorted(transcripts_dir.iterdir()):
            if not session_dir.is_dir() or not UUID_RE.match(session_dir.name):
                continue
            if uuid_filter and session_dir.name != uuid_filter:
                continue
            path = session_dir / f"{session_dir.name}.jsonl"
            if not path.is_file():
                continue
            stat = path.stat()
            rows = _read_rows(path)
            first_user = ""
            for row in rows:
                if str(row.get("role") or row.get("type") or "") == "user":
                    first_user = _clean_title(_message_text(row))
                    if first_user:
                        break
            title_record = titles.get(session_dir.name, {})
            title = str(title_record.get("name") or "").strip() or first_user or "(untitled)"
            created = _format_timestamp(title_record.get("createdAt")) or _format_timestamp(stat.st_ctime)
            updated = _format_timestamp(title_record.get("lastUpdatedAt")) or _format_timestamp(stat.st_mtime)
            yield CursorTranscript(
                uuid=session_dir.name,
                path=path,
                project=project_dir.name,
                title=title,
                created_at=created,
                updated_at=updated,
                modified_ns=stat.st_mtime_ns,
                sha256=_content_sha256(path),
            )


def _index_path() -> Path:
    return CODEX_HOME / "external_agent_session_imports.json"


def _load_import_index() -> dict[str, Any]:
    path = _index_path()
    if not path.exists():
        return {"records": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"records": []}
    if not isinstance(data, dict):
        return {"records": []}
    records = data.get("records")
    if not isinstance(records, list):
        data["records"] = []
    return data


def _codex_project_cwd() -> str:
    """Choose the Codex project that imported sessions should appear under."""
    explicit = os.environ.get("CCC_CODEX_PROJECT_CWD")
    if explicit:
        return explicit
    state_path = CODEX_HOME / ".codex-global-state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        atom_state = state.get("electron-persisted-atom-state")
        if isinstance(atom_state, dict):
            active = atom_state.get("active-workspace-roots")
            if isinstance(active, list) and active and isinstance(active[0], str):
                return active[0]
    except (OSError, json.JSONDecodeError):
        pass
    return str(DEFAULT_CODEX_PROJECT_CWD)


def _already_imported(index: dict[str, Any], transcript: CursorTranscript) -> bool:
    source_path = str(transcript.path)
    for record in index.get("records", []):
        if not isinstance(record, dict):
            continue
        if (
            record.get("source_path") == source_path
            and record.get("content_sha256") == transcript.sha256
            and int(record.get("source_modified_at") or 0) == transcript.modified_ns
        ):
            return True
    return False


def _write_import_index(index: dict[str, Any]) -> None:
    path = _index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_name(path.name + ".bak-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(path, backup)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _session_index_path() -> Path:
    return CODEX_HOME / "session_index.jsonl"


def _load_session_index() -> list[dict[str, Any]]:
    path = _session_index_path()
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("id"):
            rows.append(row)
    return rows


def _write_session_index(rows: list[dict[str, Any]]) -> None:
    path = _session_index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_name(path.name + ".bak-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(path, backup)
    deduped: dict[str, dict[str, Any]] = {}
    for row in rows:
        thread_id = str(row.get("id") or "")
        if thread_id:
            deduped[thread_id] = row
    ordered = sorted(deduped.values(), key=lambda row: str(row.get("updated_at") or ""))
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in ordered), encoding="utf-8")
    tmp.replace(path)


def _session_index_row(thread_id: str, thread_name: str, updated_at: str) -> dict[str, str]:
    return {
        "id": thread_id,
        "thread_name": _single_line(thread_name, 120) or "(untitled)",
        "updated_at": updated_at.replace("Z", "000Z") if updated_at.endswith(".") else updated_at,
    }


def _append_session_index(row: dict[str, Any]) -> bool:
    rows = _load_session_index()
    if any(existing.get("id") == row.get("id") for existing in rows):
        return False
    rows.append(row)
    _write_session_index(rows)
    return True


def _state_db_path() -> Path:
    return CODEX_HOME / "state_5.sqlite"


def _parse_iso_timestamp_ms(value: str | None) -> tuple[int, int]:
    if not value:
        now = int(time.time())
        return now, now * 1000
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        now = int(time.time())
        return now, now * 1000
    seconds = int(dt.timestamp())
    millis = int(dt.timestamp() * 1000)
    return seconds, millis


def _first_user_message_from_codex_rows(rows: list[dict[str, Any]]) -> str:
    for row in rows:
        if row.get("type") != "response_item":
            continue
        payload = row.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "message" or payload.get("role") != "user":
            continue
        texts: list[str] = []
        for item in payload.get("content") or []:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                texts.append(item["text"])
        text = _clean_title("\n".join(texts))
        if text:
            return text
    return ""


def _thread_row_from_import_record(record: dict[str, Any]) -> dict[str, Any] | None:
    thread_id = str(record.get("imported_thread_id") or "")
    session_path = Path(str(record.get("imported_session_path") or ""))
    if not thread_id or not session_path.exists():
        return None
    rows = _read_rows(session_path)
    if not rows:
        return None
    meta = rows[0].get("payload") if rows[0].get("type") == "session_meta" else {}
    if not isinstance(meta, dict):
        meta = {}
    timestamp = str(meta.get("timestamp") or rows[0].get("timestamp") or "")
    created_at, created_at_ms = _parse_iso_timestamp_ms(timestamp)
    updated_at, updated_at_ms = created_at, created_at_ms
    first_user = _first_user_message_from_codex_rows(rows)
    title = str(record.get("source_title") or meta.get("title") or first_user or "(untitled)").strip()
    cwd = str(record.get("imported_project_cwd") or meta.get("cwd") or _codex_project_cwd())
    return {
        "id": thread_id,
        "rollout_path": str(session_path),
        "created_at": created_at,
        "updated_at": updated_at,
        "source": "vscode",
        "model_provider": "openai",
        "cwd": cwd,
        "title": _single_line(title, 200) or "(untitled)",
        "sandbox_policy": '{"type":"read-only"}',
        "approval_mode": "on-request",
        "tokens_used": 0,
        "has_user_event": 0,
        "archived": 0,
        "archived_at": None,
        "git_sha": None,
        "git_branch": None,
        "git_origin_url": None,
        "cli_version": "0.142.0-alpha.6",
        "first_user_message": first_user,
        "agent_nickname": None,
        "agent_role": None,
        "memory_mode": "enabled",
        "model": None,
        "reasoning_effort": None,
        "agent_path": None,
        "created_at_ms": created_at_ms,
        "updated_at_ms": updated_at_ms,
        "thread_source": None,
        "preview": first_user,
        "recency_at": updated_at,
        "recency_at_ms": updated_at_ms,
    }


def _insert_thread_rows(thread_rows: list[dict[str, Any]]) -> int:
    if not thread_rows:
        return 0
    db_path = _state_db_path()
    if not db_path.exists():
        return 0
    columns = [
        "id",
        "rollout_path",
        "created_at",
        "updated_at",
        "source",
        "model_provider",
        "cwd",
        "title",
        "sandbox_policy",
        "approval_mode",
        "tokens_used",
        "has_user_event",
        "archived",
        "archived_at",
        "git_sha",
        "git_branch",
        "git_origin_url",
        "cli_version",
        "first_user_message",
        "agent_nickname",
        "agent_role",
        "memory_mode",
        "model",
        "reasoning_effort",
        "agent_path",
        "created_at_ms",
        "updated_at_ms",
        "thread_source",
        "preview",
        "recency_at",
        "recency_at_ms",
    ]
    placeholders = ", ".join("?" for _ in columns)
    sql = f"INSERT OR IGNORE INTO threads ({', '.join(columns)}) VALUES ({placeholders})"
    inserted = 0
    with sqlite3.connect(db_path) as con:
        before = con.total_changes
        con.executemany(sql, [[row.get(column) for column in columns] for row in thread_rows])
        con.commit()
        inserted = con.total_changes - before
    return inserted


def repair_cursor_state_db() -> int:
    db_path = _state_db_path()
    if not db_path.exists():
        print("repaired_state_db=0 state_db_missing=true")
        return 0
    backup = db_path.with_name(db_path.name + ".bak-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    with sqlite3.connect(db_path) as source, sqlite3.connect(backup) as target:
        source.backup(target)

    index = _load_import_index()
    with sqlite3.connect(db_path) as con:
        existing_ids = {row[0] for row in con.execute("select id from threads")}

    thread_rows: list[dict[str, Any]] = []
    for record in index.get("records", []):
        if not isinstance(record, dict):
            continue
        source_path = str(record.get("source_path") or "")
        if record.get("source_agent") != "cursor" and "/.cursor/" not in source_path:
            continue
        thread_id = str(record.get("imported_thread_id") or "")
        if not thread_id or thread_id in existing_ids:
            continue
        row = _thread_row_from_import_record(record)
        if row is not None:
            thread_rows.append(row)
    repaired = _insert_thread_rows(thread_rows)
    with sqlite3.connect(db_path) as con:
        integrity = con.execute("pragma integrity_check").fetchone()[0]
        con.execute("pragma wal_checkpoint(full)")
    print(f"repaired_state_db={repaired} integrity={integrity} backup={backup}")
    return 0


def _codex_session_path(thread_id: str, timestamp: str) -> Path:
    dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(timezone.utc)
    folder = CODEX_HOME / "sessions" / f"{dt.year:04d}" / f"{dt.month:02d}" / f"{dt.day:02d}"
    filename = f"rollout-{dt.strftime('%Y-%m-%dT%H-%M-%S')}-{thread_id}.jsonl"
    return folder / filename


def _codex_content(role: str, text: str) -> list[dict[str, str]]:
    content_type = "input_text" if role == "user" else "output_text"
    return [{"type": content_type, "text": text}]


def _event_row(timestamp: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"timestamp": timestamp, "type": "event_msg", "payload": payload}


def _message_row(timestamp: str, role: str, text: str) -> dict[str, Any]:
    return {
        "timestamp": timestamp,
        "type": "response_item",
        "payload": {"type": "message", "role": role, "content": _codex_content(role, text)},
    }


def _convert_to_codex_rows(
    transcript: CursorTranscript, thread_id: str, timestamp: str, project_cwd: str
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = [
        {
            "timestamp": timestamp,
            "type": "session_meta",
            "payload": {
                "id": thread_id,
                "timestamp": timestamp,
                "cwd": project_cwd,
                "originator": "Codex Desktop",
                "source": "vscode",
                "thread_source": "user",
                "title": transcript.title,
                "external_source": {
                    "agent": "cursor",
                    "uuid": transcript.uuid,
                    "project": transcript.project,
                    "path": str(transcript.path),
                    "created": transcript.created_at,
                    "updated": transcript.updated_at,
                },
            },
        },
        _event_row(
            timestamp,
            {
                "type": "task_started",
                "turn_id": _uuid7_like(),
                "started_at": _unix_seconds(),
                "collaboration_mode_kind": "imported",
            },
        ),
    ]

    for source_row in _read_rows(transcript.path):
        role = str(source_row.get("role") or source_row.get("type") or "unknown")
        if role not in {"user", "assistant"}:
            continue
        text = _message_text(source_row)
        if not text:
            continue
        if role == "user":
            rows.append(
                _event_row(
                    timestamp,
                    {
                        "type": "user_message",
                        "message": _single_line(text, 1000),
                        "images": [],
                        "local_images": [],
                        "text_elements": [],
                    },
                )
            )
        else:
            rows.append(
                _event_row(
                    timestamp,
                    {
                        "type": "agent_message",
                        "message": _single_line(text, 1000),
                        "phase": "imported",
                        "memory_citation": None,
                    },
                )
            )
        rows.append(_message_row(timestamp, role, text))
    return rows


def _write_session(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    tmp.replace(path)


def repair_cursor_session_index() -> int:
    index = _load_import_index()
    rows = _load_session_index()
    existing_ids = {str(row.get("id")) for row in rows if row.get("id")}
    repaired = 0
    for record in index.get("records", []):
        if not isinstance(record, dict):
            continue
        source_path = str(record.get("source_path") or "")
        if record.get("source_agent") != "cursor" and "/.cursor/" not in source_path:
            continue
        thread_id = str(record.get("imported_thread_id") or "")
        if not thread_id or thread_id in existing_ids:
            continue
        session_path_text = str(record.get("imported_session_path") or "")
        if session_path_text and not Path(session_path_text).exists():
            continue
        title = str(record.get("source_title") or "").strip() or "(untitled)"
        imported_at = int(record.get("imported_at") or _unix_seconds())
        updated_at = datetime.fromtimestamp(imported_at, tz=timezone.utc).isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        )
        rows.append(_session_index_row(thread_id, title, updated_at))
        existing_ids.add(thread_id)
        repaired += 1
    if repaired:
        _write_session_index(rows)
    print(f"repaired_index={repaired}")
    return 0


def import_cursor(uuid_filter: str | None, dry_run: bool) -> int:
    if uuid_filter and not UUID_RE.match(uuid_filter):
        print(f"Error: invalid Cursor UUID: {uuid_filter}", file=sys.stderr)
        return 1

    transcripts = list(_iter_cursor_transcripts(uuid_filter))
    if uuid_filter and not transcripts:
        print(f"Error: Cursor transcript not found: {uuid_filter}", file=sys.stderr)
        return 1

    index = _load_import_index()
    project_cwd = _codex_project_cwd()
    imported = 0
    skipped = 0
    would_import = 0

    for transcript in transcripts:
        if _already_imported(index, transcript):
            skipped += 1
            continue
        if dry_run:
            would_import += 1
            print(f"would import cursor {transcript.uuid} title={transcript.title!r} path={transcript.path}")
            continue

        thread_id = _uuid7_like()
        timestamp = _iso_now()
        session_path = _codex_session_path(thread_id, timestamp)
        rows = _convert_to_codex_rows(transcript, thread_id, timestamp, project_cwd)
        _write_session(rows, session_path)
        _append_session_index(_session_index_row(thread_id, transcript.title, timestamp))
        thread_row = _thread_row_from_import_record(
            {
                "source_title": transcript.title,
                "imported_thread_id": thread_id,
                "imported_session_path": str(session_path),
                "imported_project_cwd": project_cwd,
            }
        )
        if thread_row is not None:
            _insert_thread_rows([thread_row])
        index.setdefault("records", []).append(
            {
                "source_path": str(transcript.path),
                "source_agent": "cursor",
                "source_uuid": transcript.uuid,
                "source_project": transcript.project,
                "source_title": transcript.title,
                "content_sha256": transcript.sha256,
                "imported_thread_id": thread_id,
                "imported_session_path": str(session_path),
                "imported_project_cwd": project_cwd,
                "imported_at": _unix_seconds(),
                "source_modified_at": transcript.modified_ns,
            }
        )
        imported += 1

    if not dry_run and imported:
        _write_import_index(index)

    print(
        f"cursor_transcripts={len(transcripts)} imported={imported} skipped={skipped} "
        f"would_import={would_import} dry_run={str(dry_run).lower()}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import external agent transcripts into Codex sessions.")
    subparsers = parser.add_subparsers(dest="source", required=True)
    cursor_parser = subparsers.add_parser("cursor", help="Import Cursor Agent transcripts into Codex.")
    cursor_parser.add_argument("--all", action="store_true", help="Import all Cursor transcripts.")
    cursor_parser.add_argument("--uuid", help="Import one Cursor transcript UUID.")
    cursor_parser.add_argument("--dry-run", action="store_true", help="Show what would be imported without writing.")
    cursor_parser.add_argument(
        "--repair-index",
        action="store_true",
        help="Repair Codex session_index.jsonl from existing Cursor import records.",
    )
    cursor_parser.add_argument(
        "--repair-state-db",
        action="store_true",
        help="Repair Codex state_5.sqlite threads table from existing Cursor import records.",
    )

    args = parser.parse_args(argv)
    if args.source == "cursor":
        if args.repair_index:
            if args.all or args.uuid or args.dry_run or args.repair_state_db:
                print(
                    "Error: --repair-index cannot be combined with --all, --uuid, --dry-run, or --repair-state-db.",
                    file=sys.stderr,
                )
                return 1
            return repair_cursor_session_index()
        if args.repair_state_db:
            if args.all or args.uuid or args.dry_run:
                print("Error: --repair-state-db cannot be combined with --all, --uuid, or --dry-run.", file=sys.stderr)
                return 1
            return repair_cursor_state_db()
        if args.all and args.uuid:
            print("Error: use either --all or --uuid, not both.", file=sys.stderr)
            return 1
        if not args.all and not args.uuid:
            print("Error: pass --all or --uuid <cursor-uuid>.", file=sys.stderr)
            return 1
        return import_cursor(args.uuid, args.dry_run)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
