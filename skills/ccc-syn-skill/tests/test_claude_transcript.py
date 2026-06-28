import json
import os
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "claude_transcript.py"


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def run_cli(projects_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["CLAUDE_PROJECTS_DIR"] = str(projects_dir)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


class ClaudeTranscriptLoaderTest(unittest.TestCase):
    def test_search_uses_first_user_message_as_title(self) -> None:
        with self.subTest("search"):
            import tempfile

            with tempfile.TemporaryDirectory() as temp:
                tmp_path = Path(temp)
                session = "11111111-2222-3333-4444-555555555555"
                write_jsonl(
                    tmp_path / "-Users-example" / f"{session}.jsonl",
                    [
                        {"type": "queue-operation", "operation": "enqueue", "sessionId": session},
                        {
                            "type": "user",
                            "sessionId": session,
                            "timestamp": "2026-06-13T10:00:00.000Z",
                            "message": {"role": "user", "content": "把 Claude 任务同步给 Cursor"},
                            "uuid": "u1",
                        },
                    ],
                )

                result = run_cli(tmp_path, "search", "Claude 任务")

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("把 Claude 任务同步给 Cursor", result.stdout)
                self.assertIn(session, result.stdout)
                self.assertIn("`-Users-example`", result.stdout)

    def test_load_renders_conversation_markdown(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            session = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
            write_jsonl(
                tmp_path / "-Users-example" / f"{session}.jsonl",
                [
                    {
                        "type": "user",
                        "sessionId": session,
                        "timestamp": "2026-06-13T10:00:00.000Z",
                        "message": {"role": "user", "content": "继续昨天的任务"},
                        "uuid": "u1",
                    },
                    {
                        "type": "assistant",
                        "sessionId": session,
                        "timestamp": "2026-06-13T10:00:01.000Z",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {"type": "text", "text": "我会先检查状态。"},
                                {
                                    "type": "tool_use",
                                    "name": "Bash",
                                    "input": {"command": "git status --short"},
                                },
                            ],
                        },
                        "uuid": "a1",
                    },
                ],
            )

            result = run_cli(tmp_path, "load", session, "--max-chars", "5000")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("# Claude Code Transcript Context", result.stdout)
            self.assertIn("title: 继续昨天的任务", result.stdout)
            self.assertIn("### 1. user", result.stdout)
            self.assertIn("### 2. assistant", result.stdout)
            self.assertIn("[tool_use] Bash", result.stdout)

    def test_search_matches_later_user_messages(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temp:
            tmp_path = Path(temp)
            session = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
            write_jsonl(
                tmp_path / "-Users-example" / f"{session}.jsonl",
                [
                    {
                        "type": "user",
                        "sessionId": session,
                        "timestamp": "2026-06-13T10:00:00.000Z",
                        "message": {"role": "user", "content": "cursor-transcript"},
                        "uuid": "u1",
                    },
                    {
                        "type": "user",
                        "sessionId": session,
                        "timestamp": "2026-06-13T10:01:00.000Z",
                        "message": {"role": "user", "content": "3，把Agent配置到Claude"},
                        "uuid": "u2",
                    },
                ],
            )

            result = run_cli(tmp_path, "search", "Agent配置到Claude")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(session, result.stdout)


if __name__ == "__main__":
    unittest.main()
