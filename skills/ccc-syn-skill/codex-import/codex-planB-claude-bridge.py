#!/usr/bin/env python3
"""Plan B: convert Cursor transcripts into Claude Code JSONL so Codex's native
external-agent importer (which only understands Claude format) can ingest them.

Writes converted files under ~/.claude/projects/<CLAUDE_IMPORT_DIR>/<uuid>.jsonl
and appends minimal native-format records to external_agent_session_imports.json.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
import uuid as uuidlib
from datetime import datetime, timezone
from pathlib import Path


_SYSTEMISH = re.compile(
    r"(workspace context|read heartbeat|if it exists|HEARTBEAT\.md|MEMORY\.md|"
    r"<system_reminder|untrusted metadata\):\s*$)",
    re.I,
)

_NOISE_TITLE = re.compile(
    r"^(?:\d+|ok|收到|你好\d*|hello\d*|hi\d*|test\d*|测试\d*|ping\d*|请回复ok|回复ok|测网速|测速|网络测试|"
    r"连接性测试|连通性测试|测试连接|测试网络|测试一下|试一下)$",
    re.I,
)
_NOISE_KEYWORDS = re.compile(r"(测网速|测速|网络测试|连接性测试|连通性测试|测试连接|测试网络|ping\s*测试)", re.I)


_SECRET_PATTERNS = [
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),  # JWT
]


def redact_secrets(text: str) -> str:
    if not text:
        return text
    for pat in _SECRET_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    return text


def clean_text(text: str) -> str:
    """Strip Cursor/Feishu prompt scaffolding so titles and bodies read cleanly."""
    if not text:
        return ""
    m = re.search(r"<user_query>\s*(.*?)\s*</user_query>", text, re.S)
    if m:
        text = m.group(1)
    text = re.sub(r"<timestamp>.*?</timestamp>", " ", text, flags=re.S)
    text = re.sub(r"<system_reminder>.*?</system_reminder>", " ", text, flags=re.S)
    text = re.sub(r"^System:.*?(?:\n\n|$)", " ", text, flags=re.S)
    # Feishu sender/conversation/reply metadata wrappers (carry open_id / message_id / timestamps)
    text = re.sub(r"Replied message \(untrusted, for context\):\s*", "", text)
    text = re.sub(r"(?:Sender|Conversation info) \(untrusted metadata\):\s*", "", text)
    text = re.sub(r"^(?:\[[^\]]*\]\s*)+", "", text)  # leading [message_id:...] / [Thu ...] blocks
    text = re.sub(r"^[^\s:：\n]{1,12}[:：]\s*(?!//)(?=\S)", "", text)  # leading "Sender: " prefix (keep url schemes)
    text = re.sub(r"\bo[un]_[0-9a-z]{20,}\b", "", text)  # stray open_id / message_id tokens
    text = re.sub(r"\bom_[0-9a-z]{12,}\b", "", text)
    text = re.sub(r"```[a-zA-Z]*\s.*?```", " ", text, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return redact_secrets(text)


def _basename_from_path(match: re.Match) -> str:
    token = match.group(0).lstrip("@")
    name = Path(token).name.strip()
    if not name:
        return ""
    return re.sub(r"\.(zip|xlsx?|csv|md|jsonl?|py|sh|txt|pdf)$", "", name, flags=re.I)


def _url_label(match: re.Match) -> str:
    url = match.group(0)
    m = re.match(r"https?://([^/\s]+)(?:/([^?\s#]+))?", url, re.I)
    if not m:
        return "网页"
    host = m.group(1).replace("www.", "")
    path = (m.group(2) or "").strip("/")
    if path:
        leaf = Path(path).name or path.split("/", 1)[0]
        return f"{host}/{leaf}"[:36]
    return host


def _low_signal_title(title: str) -> bool:
    compact = re.sub(r"\s+", "", title).strip()
    if not compact:
        return True
    if re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}", compact):
        return True
    if re.fullmatch(r"[0-9a-fA-F]{24,}", compact):
        return True
    if re.fullmatch(r"(?:网页|index|索引|readme|skill|SKILL)", compact, re.I):
        return True
    return False


def compact_title(text: str, fallback: str = "(untitled)") -> str:
    """Build a short semantic title instead of using the raw user prompt."""
    cleaned = clean_text(text)
    if not cleaned:
        return fallback
    cleaned = re.sub(r"@?/(?:Users|private|tmp|var|Applications)/[^\s，。；；,;:：)）]+", _basename_from_path, cleaned)
    cleaned = re.sub(r"@[A-Za-z0-9_.-]+/[^\s，。；；,;:：)）]+", _basename_from_path, cleaned)
    cleaned = re.sub(r"https?://[^\s，。；；,;:：)）]+", _url_label, cleaned)
    cleaned = re.sub(r"(^|\s)/[A-Za-z0-9_-]+(?=\s|$)", " ", cleaned)
    cleaned = re.sub(r"\b(?:skill|SKILL)\b[-_:/A-Za-z0-9]*", " ", cleaned)
    cleaned = re.sub(r"#{1,6}\s*", " ", cleaned)
    cleaned = re.sub(r"`([^`]+)`", r"\1", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ，。；;:：-")
    if not cleaned:
        return fallback

    # Prefer the actionable clause; drop operational follow-ups that make titles noisy.
    splitters = [
        "，先", "。先", "；先", ",先",
        "，然后", "。然后", "；然后", ",然后",
        "，注意", "。注意", "；注意", ",注意",
        "，有需要", "。有需要",
        "\n",
    ]
    for sep in splitters:
        if sep in cleaned:
            cleaned = cleaned.split(sep, 1)[0].strip()
            break

    # Keep common task verbs visible near the front.
    verbs = "安装|配置|更新|修复|统计|查询|查|分析|生成|导入|同步|整理|拆分|上传|抓取|部署|迁移|检查|验证|解释|讲讲|设置|清理|脱敏|汇总|计算|跑通"
    match = re.search(rf"({verbs})", cleaned)
    if match and match.start() > 0 and match.start() < 24:
        prefix = cleaned[:match.start()].strip(" ，。；;:：-")
        suffix = cleaned[match.start():]
        if len(prefix) > 18 or "/" in prefix:
            cleaned = suffix

    cleaned = cleaned.strip(" ，。；;:：-")
    title = cleaned[:36].rstrip(" ，。；;:：-")
    return title if title and not _low_signal_title(title) else fallback


def clean_title(text: str, fallback: str = "(untitled)") -> str:
    return compact_title(text, fallback=fallback)


def cursor_title(text: str, fallback: str = "(untitled)") -> str:
    """Use Cursor's sidebar title as-is, with only whitespace/secret cleanup."""
    title = redact_secrets(re.sub(r"\s+", " ", str(text or "")).strip())
    if not title or title == "(untitled)" or _low_signal_title(title):
        return fallback
    return title[:80].rstrip(" ，。；;:：-")


def pick_title(turns, fallback_text: str = "") -> str:
    """Prefer Cursor's sidebar title; fallback to compacted user prompt only when missing."""
    title = cursor_title(fallback_text, fallback="")
    if title:
        return title

    user_texts = [t for r, t in turns if r == "user"]
    for t in user_texts:
        if not _SYSTEMISH.search(t):
            title = compact_title(t)
            if title != "(untitled)" and not _low_signal_title(title):
                return title
    if user_texts:
        title = compact_title(user_texts[0])
        if title != "(untitled)" and not _low_signal_title(title):
            return title
    return compact_title(fallback_text)


def is_noise_conversation(turns) -> bool:
    """Skip short no-value connectivity / greeting probes before importing to Codex."""
    user_texts = [clean_text(t) for r, t in turns if r == "user"]
    user_texts = [t for t in user_texts if t]
    if not user_texts:
        return True
    first = user_texts[0]
    compact = re.sub(r"\s+", "", first).strip()
    whole = " ".join(user_texts)

    if len(compact) <= 12 and _NOISE_TITLE.fullmatch(compact):
        return True
    if len(turns) <= 4 and _NOISE_KEYWORDS.search(whole):
        return True
    return False

SKILL_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SKILL_SCRIPTS))
import codex_import as ci  # noqa: E402

HOME = Path.home()
CODEX = Path(os.environ.get("CODEX_HOME", HOME / ".codex"))
IMPORTS = CODEX / "external_agent_session_imports.json"
CLAUDE_PROJECTS = HOME / ".claude" / "projects"
# directory name encodes a cwd so Codex groups these under the Codex project
CODEX_CWD = ci._codex_project_cwd()
CLAUDE_IMPORT_DIRNAME = "-" + CODEX_CWD.strip("/").replace("/", "-")
TARGET_DIR = Path(os.environ.get("CCC_CURSOR_BRIDGE_TARGET_DIR", CLAUDE_PROJECTS / CLAUDE_IMPORT_DIRNAME))
SIDECAR = Path(os.environ.get("CCC_CURSOR_BRIDGE_SIDECAR", CODEX / "cursor-claude-bridge-map.json"))


def _iso(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _timestamp_ms(value: str, fallback_ns: int = 0) -> int:
    text = str(value or "").strip()
    if text:
        try:
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            dt = datetime.fromisoformat(text)
            return int(dt.timestamp() * 1000)
        except ValueError:
            pass
    if fallback_ns:
        return int(fallback_ns / 1_000_000)
    return int(time.time() * 1000)


def _turns(transcript_path: Path):
    rows = ci._read_rows(transcript_path)
    turns = []
    for row in rows:
        role = str(row.get("role") or row.get("type") or "")
        if role not in ("user", "assistant"):
            continue
        text = ci._message_text(row)
        if role == "user":
            text = clean_text(text)
        else:
            text = redact_secrets(text)
        if not text.strip():
            continue
        turns.append((role, text))
    return turns


def _write_claude_file(transcript, turns) -> Path:
    sid = str(uuidlib.uuid4())
    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    out = TARGET_DIR / f"{sid}.jsonl"
    source_updated_ms = _timestamp_ms(transcript.updated_at, transcript.modified_ns)
    base_ms = source_updated_ms - max(len(turns), 1) * 1000
    lines = []
    parent = None
    for i, (role, text) in enumerate(turns):
        u = str(uuidlib.uuid4())
        ts = _iso(base_ms + i)
        if role == "user":
            msg = {"role": "user", "content": [{"type": "text", "text": text}]}
        else:
            msg = {"role": "assistant", "content": [{"type": "text", "text": text}]}
        lines.append(json.dumps({
            "parentUuid": parent,
            "isSidechain": False,
            "type": role,
            "message": msg,
            "uuid": u,
            "timestamp": ts,
            "cwd": CODEX_CWD,
            "sessionId": sid,
            "version": "2.0.0",
            "userType": "external",
        }, ensure_ascii=False))
        parent = u
    title = pick_title(turns, transcript.title)
    if title == "(untitled)":
        title = clean_title(transcript.title)
    lines.append(json.dumps({"type": "ai-title", "aiTitle": title, "sessionId": sid, "timestamp": _iso(source_updated_ms)}, ensure_ascii=False))
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="only convert first N transcripts (0 = all)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    # sidecar maps cursor uuid -> converted claude file (no ledger writes here;
    # the official externalAgentConfig/import RPC owns the dedup ledger).
    sidecar = SIDECAR
    bridge_map = json.loads(sidecar.read_text(encoding="utf-8")) if sidecar.exists() else {}
    done_cursor = set(bridge_map.keys())
    seen_cursor = set(done_cursor)

    print(f"codex_cwd={CODEX_CWD}")
    print(f"target_dir={TARGET_DIR}")

    converted = 0
    skipped_noise = 0
    transcripts = sorted(
        ci._iter_cursor_transcripts(),
        key=lambda item: _timestamp_ms(item.updated_at, item.modified_ns),
    )
    print(f"cursor_transcripts_seen={len(transcripts)}")
    for transcript in transcripts:
        if args.limit and converted >= args.limit:
            break
        if transcript.uuid in seen_cursor:
            continue
        seen_cursor.add(transcript.uuid)
        turns = _turns(transcript.path)
        if not turns:
            continue
        if is_noise_conversation(turns):
            skipped_noise += 1
            if args.dry_run:
                print(f"[skip-noise] {transcript.uuid} title={pick_title(turns, transcript.title)[:40]}")
            continue
        if args.dry_run:
            print(f"[dry] {transcript.uuid} turns={len(turns)} title={pick_title(turns, transcript.title)[:40]}")
            converted += 1
            continue
        out = _write_claude_file(transcript, turns)
        bridge_map[transcript.uuid] = {
            "claude_file": str(out),
            "source_updated_at": transcript.updated_at,
            "source_modified_ns": transcript.modified_ns,
            "source_title": transcript.title,
            "source_project": transcript.project,
        }
        converted += 1

    if not args.dry_run and converted:
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(json.dumps(bridge_map, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"converted={converted} skipped_noise={skipped_noise} sidecar_total={len(bridge_map)}")


if __name__ == "__main__":
    main()
