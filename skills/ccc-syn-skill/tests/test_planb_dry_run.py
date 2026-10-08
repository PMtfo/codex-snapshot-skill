import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "codex-import" / "codex-planB-run.py"


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


class PlanBDryRunTest(unittest.TestCase):
    def test_cursor_dry_run_uses_temporary_bridge_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cursor_projects_dir = root / "cursor-projects"
            codex_home = root / "codex-home"
            workspace = root / "Codex"
            session = "11111111-2222-3333-4444-555555555555"
            write_jsonl(
                cursor_projects_dir / "Users-example" / "agent-transcripts" / session / f"{session}.jsonl",
                [
                    {"role": "user", "message": {"content": "检查 dry-run 不写真实中间目录"}},
                    {"role": "assistant", "message": {"content": "收到，我来验证。"}},
                ],
            )

            env = os.environ.copy()
            env["HOME"] = str(root / "home")
            env["CCC_CURSOR_PROJECTS_DIR"] = str(cursor_projects_dir)
            env["CCC_CURSOR_STATE_DB"] = str(root / "missing-state.vscdb")
            env["CODEX_HOME"] = str(codex_home)
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "dry-run",
                    "--source",
                    "cursor",
                    "--codex-cwd",
                    str(workspace),
                ],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("target_dir=", result.stdout)
            self.assertIn("ccc-syn-dry-run-", result.stdout)
            self.assertNotIn(str(Path.home() / ".claude" / "projects"), result.stdout)
            self.assertFalse((codex_home / "cursor-claude-bridge-map.json").exists())


if __name__ == "__main__":
    unittest.main()
