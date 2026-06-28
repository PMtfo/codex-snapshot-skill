import json
import os
import subprocess
import sqlite3
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "codex_import.py"
CURSOR_UUID = "11111111-2222-3333-4444-555555555555"


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def seed_cursor_transcript(cursor_projects_dir: Path, uuid: str = CURSOR_UUID) -> Path:
    transcript_path = (
        cursor_projects_dir
        / "Users-example-project"
        / "agent-transcripts"
        / uuid
        / f"{uuid}.jsonl"
    )
    write_jsonl(
        transcript_path,
        [
            {
                "role": "user",
                "message": {"content": "把 Cursor 对话导入 Codex"},
            },
            {
                "role": "assistant",
                "message": {"content": [{"type": "text", "text": "我来生成导入脚本。"}]},
            },
        ],
    )
    return transcript_path


def run_cli(cursor_projects_dir: Path, codex_home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["CCC_CURSOR_PROJECTS_DIR"] = str(cursor_projects_dir)
    env["CODEX_HOME"] = str(codex_home)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def seed_state_db(codex_home: Path) -> Path:
    db_path = codex_home / "state_5.sqlite"
    codex_home.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as con:
        con.execute(
            """
            CREATE TABLE threads (
                id TEXT PRIMARY KEY,
                rollout_path TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                source TEXT NOT NULL,
                model_provider TEXT NOT NULL,
                cwd TEXT NOT NULL,
                title TEXT NOT NULL,
                sandbox_policy TEXT NOT NULL,
                approval_mode TEXT NOT NULL,
                tokens_used INTEGER NOT NULL DEFAULT 0,
                has_user_event INTEGER NOT NULL DEFAULT 0,
                archived INTEGER NOT NULL DEFAULT 0,
                archived_at INTEGER,
                git_sha TEXT,
                git_branch TEXT,
                git_origin_url TEXT,
                cli_version TEXT NOT NULL DEFAULT '',
                first_user_message TEXT NOT NULL DEFAULT '',
                agent_nickname TEXT,
                agent_role TEXT,
                memory_mode TEXT NOT NULL DEFAULT 'enabled',
                model TEXT,
                reasoning_effort TEXT,
                agent_path TEXT,
                created_at_ms INTEGER,
                updated_at_ms INTEGER,
                thread_source TEXT,
                preview TEXT NOT NULL DEFAULT '',
                recency_at INTEGER NOT NULL DEFAULT 0,
                recency_at_ms INTEGER NOT NULL DEFAULT 0
            )
            """
        )
    return db_path


class CodexImportTest(unittest.TestCase):
    def test_dry_run_reports_pending_without_writing_codex_files(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cursor_projects_dir = root / "cursor-projects"
            codex_home = root / "codex"
            seed_cursor_transcript(cursor_projects_dir)

            result = run_cli(cursor_projects_dir, codex_home, "cursor", "--all", "--dry-run")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("would_import=1", result.stdout)
            self.assertFalse((codex_home / "external_agent_session_imports.json").exists())
            self.assertEqual(list((codex_home / "sessions").glob("**/*.jsonl")), [])

    def test_import_writes_codex_session_and_external_import_index(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cursor_projects_dir = root / "cursor-projects"
            codex_home = root / "codex"
            seed_state_db(codex_home)
            source_path = seed_cursor_transcript(cursor_projects_dir)

            result = run_cli(cursor_projects_dir, codex_home, "cursor", "--all")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("imported=1", result.stdout)
            sessions = list((codex_home / "sessions").glob("**/*.jsonl"))
            self.assertEqual(len(sessions), 1)
            rows = [json.loads(line) for line in sessions[0].read_text(encoding="utf-8").splitlines()]
            self.assertEqual(rows[0]["type"], "session_meta")
            self.assertEqual(rows[0]["payload"]["cwd"], str(Path.home() / "Desktop" / "Codex"))
            self.assertEqual(rows[0]["payload"]["originator"], "Codex Desktop")
            self.assertEqual(rows[0]["payload"]["source"], "vscode")
            self.assertEqual(rows[3]["payload"]["role"], "user")
            self.assertIn("把 Cursor 对话导入 Codex", rows[3]["payload"]["content"][0]["text"])
            self.assertEqual(rows[5]["payload"]["role"], "assistant")
            self.assertIn("我来生成导入脚本", rows[5]["payload"]["content"][0]["text"])

            index = json.loads((codex_home / "external_agent_session_imports.json").read_text())
            self.assertEqual(len(index["records"]), 1)
            self.assertEqual(index["records"][0]["source_path"], str(source_path))
            self.assertEqual(index["records"][0]["source_agent"], "cursor")
            self.assertEqual(index["records"][0]["imported_project_cwd"], str(Path.home() / "Desktop" / "Codex"))
            session_index = [
                json.loads(line)
                for line in (codex_home / "session_index.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(session_index), 1)
            self.assertEqual(session_index[0]["id"], index["records"][0]["imported_thread_id"])
            self.assertEqual(session_index[0]["thread_name"], "把 Cursor 对话导入 Codex")
            with sqlite3.connect(codex_home / "state_5.sqlite") as con:
                row = con.execute(
                    "select title, cwd, source, first_user_message, preview from threads"
                ).fetchone()
            self.assertEqual(row[0], "把 Cursor 对话导入 Codex")
            self.assertEqual(row[1], str(Path.home() / "Desktop" / "Codex"))
            self.assertEqual(row[2], "vscode")
            self.assertEqual(row[3], "把 Cursor 对话导入 Codex")
            self.assertEqual(row[4], "把 Cursor 对话导入 Codex")

    def test_import_is_incremental_for_unchanged_sources(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cursor_projects_dir = root / "cursor-projects"
            codex_home = root / "codex"
            seed_cursor_transcript(cursor_projects_dir)

            first = run_cli(cursor_projects_dir, codex_home, "cursor", "--all")
            second = run_cli(cursor_projects_dir, codex_home, "cursor", "--all")

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("skipped=1", second.stdout)
            sessions = list((codex_home / "sessions").glob("**/*.jsonl"))
            self.assertEqual(len(sessions), 1)
            index = json.loads((codex_home / "external_agent_session_imports.json").read_text())
            self.assertEqual(len(index["records"]), 1)
            session_index = [
                json.loads(line)
                for line in (codex_home / "session_index.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(session_index), 1)

    def test_uuid_filter_imports_only_matching_cursor_transcript(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cursor_projects_dir = root / "cursor-projects"
            codex_home = root / "codex"
            seed_cursor_transcript(cursor_projects_dir, CURSOR_UUID)
            seed_cursor_transcript(cursor_projects_dir, "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

            result = run_cli(cursor_projects_dir, codex_home, "cursor", "--uuid", CURSOR_UUID)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("imported=1", result.stdout)
            sessions = list((codex_home / "sessions").glob("**/*.jsonl"))
            self.assertEqual(len(sessions), 1)
            index = json.loads((codex_home / "external_agent_session_imports.json").read_text())
            self.assertEqual(index["records"][0]["source_uuid"], CURSOR_UUID)

    def test_repair_index_adds_missing_rows_from_existing_import_records(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cursor_projects_dir = root / "cursor-projects"
            codex_home = root / "codex"
            seed_cursor_transcript(cursor_projects_dir)
            first = run_cli(cursor_projects_dir, codex_home, "cursor", "--all")
            self.assertEqual(first.returncode, 0, first.stderr)
            (codex_home / "session_index.jsonl").unlink()

            repair = run_cli(cursor_projects_dir, codex_home, "cursor", "--repair-index")

            self.assertEqual(repair.returncode, 0, repair.stderr)
            self.assertIn("repaired_index=1", repair.stdout)
            session_index = [
                json.loads(line)
                for line in (codex_home / "session_index.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(session_index), 1)
            self.assertEqual(session_index[0]["thread_name"], "把 Cursor 对话导入 Codex")

    def test_repair_state_db_adds_missing_thread_rows_from_existing_import_records(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cursor_projects_dir = root / "cursor-projects"
            codex_home = root / "codex"
            seed_state_db(codex_home)
            seed_cursor_transcript(cursor_projects_dir)
            first = run_cli(cursor_projects_dir, codex_home, "cursor", "--all")
            self.assertEqual(first.returncode, 0, first.stderr)
            with sqlite3.connect(codex_home / "state_5.sqlite") as con:
                con.execute("delete from threads")
                con.commit()

            repair = run_cli(cursor_projects_dir, codex_home, "cursor", "--repair-state-db")

            self.assertEqual(repair.returncode, 0, repair.stderr)
            self.assertIn("repaired_state_db=1", repair.stdout)
            with sqlite3.connect(codex_home / "state_5.sqlite") as con:
                rows = con.execute(
                    "select title, cwd, source, first_user_message, preview from threads"
                ).fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][0], "把 Cursor 对话导入 Codex")
            self.assertEqual(rows[0][1], str(Path.home() / "Desktop" / "Codex"))
            self.assertEqual(rows[0][2], "vscode")


if __name__ == "__main__":
    unittest.main()
