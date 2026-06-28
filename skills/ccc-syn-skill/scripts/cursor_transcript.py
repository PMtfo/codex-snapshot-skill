#!/usr/bin/env python3
"""Find and render Cursor agent transcripts for Claude Code context handoff."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


HOME = Path.home()
CURSOR_PROJECTS_DIR = HOME / ".cursor" / "projects"
CURSOR_STATE_DB = HOME / "Library/Application Support/Cursor/User/globalStorage/state.vscdb"
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


@dataclass(frozen=True)
class TranscriptRef:
    uuid: str
    path: Path
    project: str


@dataclass
class ComposerRecord:
    uuid: str
    title: str = ""
    created_at: int | float | str | None = None
    last_updated_at: int | float | str | None = None
    subtitle: str = ""


def _decode_db_value(value: Any) -> dict[str, Any] | None:
    if isinstance(value, bytes):
        text = value.decode("utf-8", "replace")
    elif isinstance(value, str):
        text = value
    else:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _connect_state_db() -> sqlite3.Connection:
    if not CURSOR_STATE_DB.exists():
        raise FileNotFoundError(f"Cursor state DB not found: {CURSOR_STATE_DB}")
    uri = f"file:{CURSOR_STATE_DB}?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True)


def _load_composer_records() -> dict[str, ComposerRecord]:
    records: dict[str, ComposerRecord] = {}
    with _connect_state_db() as con:
        rows = con.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'"
        )
        for key, value in rows:
            uuid = str(key).split("composerData:", 1)[-1]
            if not UUID_RE.match(uuid):
                continue
            data = _decode_db_value(value)
            if not data:
                continue
            records[uuid] = ComposerRecord(
                uuid=uuid,
                title=str(data.get("name") or "").strip(),
                created_at=data.get("createdAt"),
                last_updated_at=data.get("lastUpdatedAt"),
                subtitle=str(data.get("subtitle") or "").strip(),
            )
    return records


def _iter_transcripts() -> Iterable[TranscriptRef]:
    if not CURSOR_PROJECTS_DIR.exists():
        return
    for project_dir in sorted(CURSOR_PROJECTS_DIR.iterdir()):
        transcripts_dir = project_dir / "agent-transcripts"
        if not transcripts_dir.is_dir():
            continue
        for session_dir in sorted(transcripts_dir.iterdir()):
            if not session_dir.is_dir() or not UUID_RE.match(session_dir.name):
                continue
            transcript_path = session_dir / f"{session_dir.name}.jsonl"
            if transcript_path.is_file():
                yield TranscriptRef(
                    uuid=session_dir.name,
                    path=transcript_path,
                    project=project_dir.name,
                )


def _transcript_index() -> dict[str, list[TranscriptRef]]:
    index: dict[str, list[TranscriptRef]] = {}
    for ref in _iter_transcripts() or []:
        index.setdefault(ref.uuid, []).append(ref)
    return index


def _extract_text_parts(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text" and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif item.get("type") == "tool_result":
                    result = item.get("content")
                    if isinstance(result, str):
                        parts.append(result)
                    elif isinstance(result, list):
                        parts.extend(_extract_text_parts(result))
        return parts
    if isinstance(content, dict):
        return _extract_text_parts(content.get("content"))
    return []


def _first_user_query(path: Path, max_chars: int = 220) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("role") != "user":
                    continue
                message = row.get("message") or {}
                text = "\n".join(_extract_text_parts(message.get("content"))).strip()
                match = re.search(r"<user_query>\s*(.*?)\s*</user_query>", text, re.S)
                if match:
                    text = match.group(1).strip()
                return _single_line(text, max_chars)
    except OSError:
        return ""
    return ""


def _single_line(text: str, max_chars: int = 120) -> str:
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
        return datetime.fromtimestamp(number, tz=timezone.utc).isoformat(timespec="seconds")
    except (OSError, OverflowError, ValueError):
        return str(value)


def _score(query: str, title: str, fallback: str, uuid: str) -> int:
    q = query.casefold().strip()
    haystacks = [title.casefold(), fallback.casefold(), uuid.casefold()]
    if not q:
        return 1
    if q == uuid.casefold():
        return 1000
    if title and q == title.casefold():
        return 900
    if title and q in title.casefold():
        return 700 + min(len(q), 100)
    if fallback and q in fallback.casefold():
        return 500 + min(len(q), 100)
    tokens = [token for token in re.split(r"\s+", q) if token]
    if not tokens:
        return 0
    matches = sum(1 for token in tokens if any(token in hay for hay in haystacks))
    return matches * 100 if matches else 0


def search(query: str, limit: int) -> int:
    records = _load_composer_records()
    transcripts = _transcript_index()
    rows: list[tuple[int, float, str, str, str, str, str]] = []

    for uuid, refs in transcripts.items():
        record = records.get(uuid, ComposerRecord(uuid=uuid))
        fallback = _first_user_query(refs[0].path) if not record.title else ""
        display_title = record.title or fallback or "(untitled)"
        score = _score(query, display_title, fallback, uuid)
        if score <= 0:
            continue
        updated_raw = record.last_updated_at or record.created_at or 0
        try:
            updated_sort = float(updated_raw)
        except (TypeError, ValueError):
            updated_sort = 0.0
        rows.append(
            (
                score,
                updated_sort,
                uuid,
                display_title,
                _format_timestamp(record.last_updated_at),
                refs[0].project,
                str(refs[0].path),
            )
        )

    rows.sort(key=lambda item: (item[0], item[1]), reverse=True)
    rows = rows[:limit]

    if not rows:
        print(f"No Cursor transcript matched: {query}", file=sys.stderr)
        return 1

    print("| # | score | title | uuid | updated | project |")
    print("|---:|---:|---|---|---|---|")
    for index, (score, _updated_sort, uuid, title, updated, project, _path) in enumerate(rows, 1):
        safe_title = title.replace("|", "\\|")
        print(
            f"| {index} | {score} | {safe_title} | `{uuid}` | {updated or '-'} | `{project}` |"
        )
    print()
    print("Use: python scripts/cursor_transcript.py load <uuid>")
    return 0


def _resolve_transcript(uuid: str) -> TranscriptRef:
    if not UUID_RE.match(uuid):
        raise ValueError(f"Not a valid Cursor transcript UUID: {uuid}")
    refs = _transcript_index().get(uuid) or []
    if not refs:
        raise FileNotFoundError(f"Transcript not found for UUID: {uuid}")
    return refs[0]


def _compact_json(value: Any, max_chars: int = 600) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        text = repr(value)
    return _single_line(text, max_chars)


def _summarize_tool_input(data: Any) -> str:
    if not isinstance(data, dict):
        return _compact_json(data)
    keys = (
        "description",
        "command",
        "path",
        "target_notebook",
        "target_directory",
        "glob_pattern",
        "pattern",
        "query",
        "server",
        "toolName",
        "url",
    )
    selected = {key: data[key] for key in keys if key in data and data[key] not in (None, "")}
    if selected:
        return _compact_json(selected)
    return _compact_json(data)


def _render_content_item(item: Any) -> list[str]:
    if isinstance(item, str):
        return [item.strip()] if item.strip() else []
    if not isinstance(item, dict):
        return []

    item_type = item.get("type")
    if item_type == "text":
        text = str(item.get("text") or "").strip()
        return [text] if text else []
    if item_type == "tool_use":
        name = item.get("name") or "unknown"
        summary = _summarize_tool_input(item.get("input"))
        return [f"[tool_use] {name}: {summary}"]
    if item_type == "tool_result":
        result_text = "\n".join(_extract_text_parts(item.get("content"))).strip()
        if not result_text:
            return ["[tool_result] (empty)"]
        return [f"[tool_result] {_single_line(result_text, 800)}"]
    return []


def _render_message_content(content: Any) -> str:
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            parts.extend(_render_content_item(item))
        return "\n\n".join(part for part in parts if part).strip()
    if isinstance(content, str):
        return content.strip()
    return "\n".join(_extract_text_parts(content)).strip()


def _append_limited(chunks: list[str], text: str, current: int, max_chars: int) -> tuple[int, bool]:
    if current >= max_chars:
        return current, False
    remaining = max_chars - current
    if len(text) <= remaining:
        chunks.append(text)
        return current + len(text), True
    chunks.append(text[:remaining].rstrip())
    chunks.append("\n\n[truncated: max chars reached]\n")
    return max_chars, False


def load(uuid: str, max_chars: int) -> int:
    ref = _resolve_transcript(uuid)
    record = _load_composer_records().get(uuid, ComposerRecord(uuid=uuid))

    chunks: list[str] = []
    header = [
        "# Cursor Transcript Context",
        "",
        f"- title: {record.title or _first_user_query(ref.path) or '(untitled)'}",
        f"- uuid: {uuid}",
        f"- project: {ref.project}",
        f"- path: {ref.path}",
        f"- created: {_format_timestamp(record.created_at) or '-'}",
        f"- updated: {_format_timestamp(record.last_updated_at) or '-'}",
        "",
        "## Conversation",
        "",
    ]
    current = 0
    current, _ = _append_limited(chunks, "\n".join(header), current, max_chars)

    try:
        with ref.path.open("r", encoding="utf-8", errors="replace") as handle:
            for line_number, line in enumerate(handle, 1):
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                role = str(row.get("role") or row.get("type") or "unknown")
                message = row.get("message") or {}
                content = _render_message_content(message.get("content"))
                if not content:
                    continue
                rendered = f"### {line_number}. {role}\n\n{content}\n\n"
                current, keep_going = _append_limited(chunks, rendered, current, max_chars)
                if not keep_going:
                    break
    except OSError as exc:
        print(f"Failed to read transcript: {exc}", file=sys.stderr)
        return 1

    print("".join(chunks))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Search and render Cursor agent transcripts for Claude Code."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    search_parser = subparsers.add_parser("search", help="Search Cursor transcript titles.")
    search_parser.add_argument("query", help="Title, keyword, or transcript UUID.")
    search_parser.add_argument("--limit", type=int, default=10, help="Maximum matches to print.")

    load_parser = subparsers.add_parser("load", help="Render one transcript as Markdown.")
    load_parser.add_argument("uuid", help="Cursor transcript UUID.")
    load_parser.add_argument(
        "--max-chars",
        type=int,
        default=40000,
        help="Maximum Markdown characters to print.",
    )

    args = parser.parse_args(argv)
    try:
        if args.command == "search":
            return search(args.query, max(args.limit, 1))
        if args.command == "load":
            return load(args.uuid, max(args.max_chars, 1000))
    except Exception as exc:  # noqa: BLE001 - 命令行工具需要给出简洁错误。
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
