# Codex 导入管线（最终版）

唯一推荐入口：`codex-planB-run.py`。

它把 Cursor / Claude Code 对话作为 Codex 官方外部 agent import 的输入，调用 `codex app-server --stdio` 的 `externalAgentConfig/import`，让对话显示在 Codex 侧栏。

## 支持来源

- `--source cursor`：扫描所有 `~/.cursor/projects/*/agent-transcripts`，转换为 Claude JSONL 后导入。
- `--source claude`：直接导入 `~/.claude/projects/*/*.jsonl` 的 Claude Code 原生会话。
- `--source both`：同时导入两类来源。

## 常用命令

```bash
cd ~/.cursor/agent-shared/skills/ccc-syn-skill
export CCC_CODEX_PROJECT_CWD="$HOME/Desktop/Codex"

# 预览近一周 Cursor 增量
python3 codex-import/codex-planB-run.py dry-run --source cursor --since 7d --incremental

# 导入近一周 Cursor 增量
python3 codex-import/codex-planB-run.py import --source cursor --since 7d --incremental

# 导入 Claude Code 增量
python3 codex-import/codex-planB-run.py import --source claude --incremental

# Cursor + Claude Code 全部重导
python3 codex-import/codex-planB-run.py all --source both
```

真实导入前必须完全退出 Codex（Cmd+Q）。

真实导入前还必须确认：

- 导入后的对话要显示在哪个路径下。
- 该路径是不是 Codex 正在使用的工作区 cwd。

## 参数

- `--source cursor|claude|both`
- `--incremental`：跳过已在 Codex import ledger 中出现过的 `source_path`
- `--since 7d|24h|2w|YYYY-MM-DD`
- `--from-date YYYY-MM-DD`
- `--to-date YYYY-MM-DD`
- `--title <关键词>`
- `--batch <数量>`
- `--codex-cwd <Codex工作区路径>`

## 标题与排序

- Cursor：复用 Cursor 侧栏标题。
- Claude Code：优先复用 JSONL 内 `ai-title`，否则使用首条用户消息。
- 导入按源更新时间从旧到新提交；Codex 侧栏倒序显示时新对话在上。

## 过滤

Cursor 导入阶段会跳过低价值测试会话，如 `1`、`测试`、`你好1`、`请回复ok`、`ping`、测网速、连接性测试、空白测试等。

## 文件说明

- `codex-planB-run.py`：最终导入入口。
- `codex-planB-claude-bridge.py`：Cursor → Claude JSONL 转换器（内部调用）。
- `codex-appserver-rpc.py`：Codex app-server JSON-RPC 客户端（内部调用）。
