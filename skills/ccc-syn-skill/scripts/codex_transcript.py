#!/usr/bin/env python3
"""Find and render Codex (Codex Desktop / CLI) transcripts for cross-agent context handoff.

读取 Codex 本地会话（~/.codex/sessions/**/rollout-*.jsonl），提供 search / load，
让 Cursor、Claude Code、Codex 三端可以互相报标题或会话 ID 后读取 Codex 历史对话。
仅只读，不修改任何 Codex 状态。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


HOME = Path.home()
CODEX_HOME = Path(os.environ.get("CODEX_HOME", HOME / ".codex"))
CODEX_SESSIONS_DIR = Path(os.environ.get("CCC_CODEX_SESSIONS_DIR", CODEX_HOME / "sessions"))
CODEX_SESSION_INDEX = Path(
    os.environ.get("CCC_CODEX_SESSION_INDEX", CODEX_HOME / "session_index.jsonl")
)
CODEX_STATE_DB = Path(os.environ.get("CCC_CODEX_STATE_DB", CODEX_HOME / "state_5.sqlite"))
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
# Codex 注入的系统/环境块开头，作为标题或搜索文本时应跳过。
_SYSTEM_PREFIXES = (
    "<environment_context>",
    "<environment_info>",
    "<permissions instructions>",
    "<permissions>",
    "<user_instructions>",
    "<system_reminder>",
    "<persistent_memory>",
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
    text = str(value).strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        try:
            number = float(text)
            if number > 10_000_000_000:
                number = number / 1000
            return datetime.fromtimestamp(number, tz=timezone.utc).isoformat(timespec="seconds")
        except (OSError, OverflowError, ValueError):
            return str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # 规整小数秒为 6 位，兼容 Python 3.9 fromisoformat 与 session_index 里 .4884 这类非标准位数。
    text = re.sub(
        r"\.(\d+)(?=[+-]\d{2}:?\d{2}$|$)",
        lambda m: "." + m.group(1).ljust(6, "0")[:6],
        text,
    )
    try:
        return datetime.fromisoformat(text).astimezone(timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        return str(value)


def _iter_transcript_paths() -> Iterable[Path]:
    if not CODEX_SESSIONS_DIR.exists():
        return []
    return sorted(CODEX_SESSIONS_DIR.glob("**/rollout-*.jsonl"))


def _load_thread_names() -> dict[str, dict[str, str]]:
    """从 session_index.jsonl + state_5.sqlite 读取标题；SQLite 标题优先。"""
    records: dict[str, dict[str, str]] = {}
    if not CODEX_SESSION_INDEX.exists():
        records = {}
    else:
        try:
            for line in CODEX_SESSION_INDEX.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict):
                    continue
                thread_id = str(row.get("id") or "").strip()
                if not thread_id:
                    continue
                records[thread_id] = {
                    "thread_name": str(row.get("thread_name") or "").strip(),
                    "updated_at": str(row.get("updated_at") or "").strip(),
                }
        except OSError:
            records = {}

    if CODEX_STATE_DB.exists():
        try:
            uri = f"file:{CODEX_STATE_DB}?mode=ro"
            with sqlite3.connect(uri, uri=True) as con:
                rows = con.execute("select id, title, updated_at_ms, updated_at from threads where archived=0")
                for thread_id, title, updated_at_ms, updated_at in rows:
                    thread_id = str(thread_id or "").strip()
                    if not thread_id:
                        continue
                    current = records.setdefault(thread_id, {})
                    current["thread_name"] = str(title or "").strip() or current.get("thread_name", "")
                    current["updated_at"] = str(updated_at_ms or updated_at or current.get("updated_at", ""))
        except sqlite3.Error:
            pass
    return records


def _extract_text_parts(content: Any) -> list[str]:
    """Codex content 项类型为 input_text / output_text / text。"""
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                item_type = item.get("type")
                if item_type in {"input_text", "output_text", "text"} and isinstance(
                    item.get("text"), str
                ):
                    parts.append(item["text"])
        return parts
    if isinstance(content, dict):
        return _extract_text_parts(content.get("content"))
    return []


def _payload_message(row: dict[str, Any]) -> dict[str, Any] | None:
    """从 rollout 行里取出 message payload（type=response_item, payload.type=message）。"""
    if str(row.get("type") or "") != "response_item":
        return None
    payload = row.get("payload")
    if not isinstance(payload, dict):
        return None
    if str(payload.get("type") or "") != "message":
        return None
    return payload


def _message_text(payload: dict[str, Any]) -> str:
    return "\n".join(part for part in _extract_text_parts(payload.get("content")) if part.strip()).strip()


def _is_system_noise(text: str) -> bool:
    stripped = text.lstrip()
    return any(stripped.startswith(prefix) for prefix in _SYSTEM_PREFIXES)


def _clean_title(text: str) -> str:
    match = re.search(r"<user_query>\s*(.*?)\s*</user_query>", text, re.S)
    if match:
        text = match.group(1)
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


def _session_meta(rows: list[dict[str, Any]]) -> dict[str, Any]:
    for row in rows:
        if str(row.get("type") or "") == "session_meta":
            payload = row.get("payload")
            if isinstance(payload, dict):
                return payload
    return {}


def _first_user_title(rows: list[dict[str, Any]], fallback: str) -> str:
    for row in rows:
        payload = _payload_message(row)
        if payload is None:
            continue
        if str(payload.get("role") or "") != "user":
            continue
        text = _message_text(payload)
        if not text or _is_system_noise(text):
            continue
        cleaned = _clean_title(text)
        if cleaned:
            return cleaned
    return fallback


def _search_text_from_rows(rows: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for row in rows:
        payload = _payload_message(row)
        if payload is None:
            continue
        if str(payload.get("role") or "") != "user":
            continue
        text = _message_text(payload)
        if not text or _is_system_noise(text):
            continue
        cleaned = _clean_title(text)
        if cleaned:
            parts.append(cleaned)
    return "\n".join(parts)


def _project_from_meta(meta: dict[str, Any], path: Path) -> str:
    cwd = str(meta.get("cwd") or "").strip()
    if cwd:
        return Path(cwd).name or cwd
    return path.parent.name


def _record_from_path(path: Path, thread_names: dict[str, dict[str, str]]) -> TranscriptRef | None:
    rows = _read_rows(path, max_rows=200)
    if not rows:
        return None
    meta = _session_meta(rows)
    session_id = str(meta.get("id") or "").strip()
    if not UUID_RE.match(session_id):
        # 退化：从文件名末尾的 uuid 取
        stem = path.stem
        tail = stem.split("-")
        guess = "-".join(tail[-5:]) if len(tail) >= 5 else ""
        session_id = guess if UUID_RE.match(guess) else ""
    if not session_id:
        return None
    timestamps = [str(row.get("timestamp")) for row in rows if row.get("timestamp")]
    index_record = thread_names.get(session_id, {})
    title = index_record.get("thread_name") or _first_user_title(rows, fallback=session_id)
    updated = index_record.get("updated_at") or (timestamps[-1] if timestamps else "")
    return TranscriptRef(
        session_id=session_id,
        path=path,
        project=_project_from_meta(meta, path),
        title=title,
        created_at=_format_timestamp(str(meta.get("timestamp") or (timestamps[0] if timestamps else ""))),
        updated_at=_format_timestamp(updated),
        search_text=_search_text_from_rows(rows),
    )


def _iter_records() -> Iterable[TranscriptRef]:
    thread_names = _load_thread_names()
    for path in _iter_transcript_paths():
        record = _record_from_path(path, thread_names)
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
        print(f"No Codex transcript matched: {query}", file=sys.stderr)
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
    print("Use: python scripts/codex_transcript.py load <session_id>")
    return 0


def _resolve_transcript(session_id: str) -> TranscriptRef:
    if not UUID_RE.match(session_id):
        raise ValueError(f"Not a valid Codex session id: {session_id}")
    for record in _iter_records():
        if record.session_id == session_id:
            return record
    raise FileNotFoundError(f"Codex transcript not found: {session_id}")


def _compact_json(value: Any, max_chars: int = 600) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        text = repr(value)
    return _single_line(text, max_chars)


def _render_function_call(payload: dict[str, Any]) -> list[str]:
    name = payload.get("name") or "unknown"
    arguments = payload.get("arguments")
    if isinstance(arguments, str):
        summary = _single_line(arguments, 600)
    else:
        summary = _compact_json(arguments)
    return [f"[tool_use] {name}: {summary}"]


def _render_function_output(payload: dict[str, Any]) -> list[str]:
    output = payload.get("output")
    text = ""
    if isinstance(output, dict):
        text = "\n".join(_extract_text_parts(output.get("content"))).strip() or str(
            output.get("content") or ""
        ).strip()
    elif isinstance(output, str):
        text = output.strip()
    return [f"[tool_result] {_single_line(text, 800)}" if text else "[tool_result] (empty)"]


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
        "# Codex Transcript Context",
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
        if str(row.get("type") or "") != "response_item":
            continue
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        payload_type = str(payload.get("type") or "")

        if payload_type == "message":
            role = str(payload.get("role") or "")
            if role not in {"user", "assistant"}:
                continue
            content = _message_text(payload)
            if not content or _is_system_noise(content):
                continue
            message_index += 1
            rendered = f"### {message_index}. {role}\n\n{content}\n\n"
        elif payload_type == "function_call":
            message_index += 1
            rendered = f"### {message_index}. assistant\n\n" + "\n".join(_render_function_call(payload)) + "\n\n"
        elif payload_type == "function_call_output":
            message_index += 1
            rendered = f"### {message_index}. tool\n\n" + "\n".join(_render_function_output(payload)) + "\n\n"
        else:
            continue

        current, keep_going = _append_limited(chunks, rendered, current, max_chars)
        if not keep_going:
            break

    print("".join(chunks))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Search and render Codex transcripts for Cursor / Claude Code handoff."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    search_parser = subparsers.add_parser("search", help="Search Codex transcripts.")
    search_parser.add_argument("query", help="Title keyword or session id.")
    search_parser.add_argument("--limit", type=int, default=10)

    load_parser = subparsers.add_parser("load", help="Render one transcript as Markdown.")
    load_parser.add_argument("session_id", help="Codex session id.")
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
