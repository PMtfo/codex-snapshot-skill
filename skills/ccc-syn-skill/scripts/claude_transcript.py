#!/usr/bin/env python3
"""Find and render Claude Code transcripts for Cursor context handoff."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


HOME = Path.home()
CLAUDE_PROJECTS_DIR = Path(os.environ.get("CLAUDE_PROJECTS_DIR", HOME / ".claude" / "projects"))
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


@dataclass(frozen=True)
class TranscriptRef:
    session_id: str
    path: Path
    project: str
    title: str
    created_at: str
    updated_at: str
    search_text: str


def _single_line(text: str, max_chars: int = 160) -> str:
    line = re.sub(r"\s+", " ", text).strip()
    if len(line) <= max_chars:
        return line
    return line[: max_chars - 1].rstrip() + "…"


def _format_timestamp(value: str | None) -> str:
    if not value:
        return ""
    text = str(value)
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text).astimezone(timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        return str(value)


def _iter_transcript_paths() -> Iterable[Path]:
    if not CLAUDE_PROJECTS_DIR.exists():
        return []
    return sorted(CLAUDE_PROJECTS_DIR.glob("*/*.jsonl"))


def _extract_text_parts(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                item_type = item.get("type")
                if item_type == "text" and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif item_type == "tool_result":
                    parts.extend(_extract_text_parts(item.get("content")))
            elif isinstance(item, str):
                parts.append(item)
        return parts
    if isinstance(content, dict):
        return _extract_text_parts(content.get("content"))
    return []


def _message_text(row: dict[str, Any]) -> str:
    message = row.get("message")
    if isinstance(message, dict):
        return "\n".join(_extract_text_parts(message.get("content"))).strip()
    attachment = row.get("attachment")
    if isinstance(attachment, dict) and isinstance(attachment.get("content"), str):
        return attachment["content"].strip()
    content = row.get("content")
    if isinstance(content, str):
        return content.strip()
    return ""


def _clean_title(text: str) -> str:
    text = re.sub(r"<command-message>\s*(.*?)\s*</command-message>", r"\1", text, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return _single_line(text, 180)


def _read_rows(path: Path, max_rows: int | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle):
                if max_rows is not None and index >= max_rows:
                    break
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        return []
    return rows


def _title_from_rows(rows: list[dict[str, Any]], fallback: str) -> str:
    for row in rows:
        if row.get("type") == "user":
            text = _clean_title(_message_text(row))
            if text:
                return text
    for row in rows:
        if row.get("type") == "queue-operation" and isinstance(row.get("content"), str):
            text = _clean_title(row["content"])
            if text:
                return text
    return fallback


def _search_text_from_rows(rows: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for row in rows:
        if row.get("type") != "user":
            continue
        text = _clean_title(_message_text(row))
        if text:
            parts.append(text)
    return "\n".join(parts)


def _record_from_path(path: Path) -> TranscriptRef | None:
    session_id = path.stem
    if not UUID_RE.match(session_id):
        return None
    rows = _read_rows(path, max_rows=80)
    if not rows:
        return None
    timestamps = [str(row.get("timestamp")) for row in rows if row.get("timestamp")]
    title = _title_from_rows(rows, fallback=session_id)
    return TranscriptRef(
        session_id=session_id,
        path=path,
        project=path.parent.name,
        title=title,
        created_at=_format_timestamp(timestamps[0] if timestamps else ""),
        updated_at=_format_timestamp(timestamps[-1] if timestamps else ""),
        search_text=_search_text_from_rows(rows),
    )


def _iter_records() -> Iterable[TranscriptRef]:
    for path in _iter_transcript_paths():
        record = _record_from_path(path)
        if record is not None:
            yield record


def _score(query: str, record: TranscriptRef) -> int:
    q = query.casefold().strip()
    if not q:
        return 1
    if q == record.session_id.casefold():
        return 1000
    title = record.title.casefold()
    if q == title:
        return 900
    if q in title:
        return 700 + min(len(q), 100)
    search_text = record.search_text.casefold()
    if q in search_text:
        return 600 + min(len(q), 100)
    haystacks = [title, search_text, record.session_id.casefold(), record.project.casefold()]
    tokens = [token for token in re.split(r"\s+", q) if token]
    matches = sum(1 for token in tokens if any(token in haystack for haystack in haystacks))
    return matches * 100 if matches else 0


def search(query: str, limit: int) -> int:
    rows: list[tuple[int, TranscriptRef]] = []
    for record in _iter_records():
        score = _score(query, record)
        if score > 0:
            rows.append((score, record))
    rows.sort(key=lambda item: (item[0], item[1].updated_at), reverse=True)
    rows = rows[:limit]

    if not rows:
        print(f"No Claude Code transcript matched: {query}", file=sys.stderr)
        return 1

    print("| # | score | title | session_id | updated | project |")
    print("|---:|---:|---|---|---|---|")
    for index, (score, record) in enumerate(rows, 1):
        safe_title = record.title.replace("|", "\\|")
        print(
            f"| {index} | {score} | {safe_title} | `{record.session_id}` | "
            f"{record.updated_at or '-'} | `{record.project}` |"
        )
    print()
    print("Use: python scripts/claude_transcript.py load <session_id>")
    return 0


def _resolve_transcript(session_id: str) -> TranscriptRef:
    if not UUID_RE.match(session_id):
        raise ValueError(f"Not a valid Claude Code session id: {session_id}")
    for record in _iter_records():
        if record.session_id == session_id:
            return record
    raise FileNotFoundError(f"Claude Code transcript not found: {session_id}")


def _compact_json(value: Any, max_chars: int = 600) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        text = repr(value)
    return _single_line(text, max_chars)


def _summarize_tool_input(data: Any) -> str:
    if not isinstance(data, dict):
        return _compact_json(data)
    keys = ("description", "command", "path", "query", "pattern", "url", "toolName")
    selected = {key: data[key] for key in keys if key in data and data[key] not in (None, "")}
    return _compact_json(selected or data)


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
        return [f"[tool_use] {name}: {_summarize_tool_input(item.get('input'))}"]
    if item_type == "tool_result":
        text = "\n".join(_extract_text_parts(item.get("content"))).strip()
        return [f"[tool_result] {_single_line(text, 800)}"] if text else ["[tool_result] (empty)"]
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


def load(session_id: str, max_chars: int) -> int:
    record = _resolve_transcript(session_id)
    chunks: list[str] = []
    header = [
        "# Claude Code Transcript Context",
        "",
        f"- title: {record.title}",
        f"- session_id: {record.session_id}",
        f"- project: {record.project}",
        f"- path: {record.path}",
        f"- created: {record.created_at or '-'}",
        f"- updated: {record.updated_at or '-'}",
        "",
        "## Conversation",
        "",
    ]
    current = 0
    current, _ = _append_limited(chunks, "\n".join(header), current, max_chars)

    message_index = 0
    for row in _read_rows(record.path):
        row_type = str(row.get("type") or "")
        if row_type not in {"user", "assistant"}:
            continue
        message = row.get("message")
        if not isinstance(message, dict):
            continue
        content = _render_message_content(message.get("content"))
        if not content:
            continue
        role = str(message.get("role") or row_type)
        message_index += 1
        rendered = f"### {message_index}. {role}\n\n{content}\n\n"
        current, keep_going = _append_limited(chunks, rendered, current, max_chars)
        if not keep_going:
            break

    print("".join(chunks))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Search and render Claude Code transcripts for Cursor."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    search_parser = subparsers.add_parser("search", help="Search Claude Code transcripts.")
    search_parser.add_argument("query", help="Title keyword or session id.")
    search_parser.add_argument("--limit", type=int, default=10)

    load_parser = subparsers.add_parser("load", help="Render one transcript as Markdown.")
    load_parser.add_argument("session_id", help="Claude Code session id.")
    load_parser.add_argument("--max-chars", type=int, default=40000)

    args = parser.parse_args(argv)
    try:
        if args.command == "search":
            return search(args.query, max(args.limit, 1))
        if args.command == "load":
            return load(args.session_id, max(args.max_chars, 1000))
    except Exception as exc:  # noqa: BLE001 - command-line utility needs a concise error.
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
