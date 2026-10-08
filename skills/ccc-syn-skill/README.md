# ccc-syn-skill

**跨 Agent 对话复用** —— 让 Cursor、Claude Code、Codex 三个 Agent 共享同一份对话上下文。

> 关键词：跨 agent 对话复用

## 它解决什么问题

同时用 Cursor 写前端、Claude Code CLI 跑重构、Codex 桌面版做大任务，是很多人的日常。问题在于：**三个 Agent 各存各的对话，互相看不见**。

- 早上在 Cursor 里跟 Agent 讨论清楚了方案，下午想在 Claude Code 里接着写 → 得手动复制粘贴。
- 上周在 Claude Code 里排查过的 bug，这周在 Codex 里又碰到 → 想找那段推理过程，只能翻终端历史。
- 想统一管理对话，把 Cursor / Claude Code 的历史都收进 Codex 侧栏 → 官方没给导入入口。

这个 skill 干两件事：

| 能力 | 说明 |
| --- | --- |
| **跨端读取** | 按标题 / 关键词 / 会话 ID，把 Cursor、Claude Code、Codex 的本地对话渲染成 Markdown，直接当作当前 Agent 的上下文继续干活。 |
| **批量导入 Codex** | 把 Cursor / Claude Code CLI 的对话批量写进 Codex 侧栏，走 Codex 官方 `externalAgentConfig/import` RPC，不是改数据库 hack。 |

不做反向迁移（Codex → Cursor/Claude），也不做 Cursor ↔ Claude 原生导入。

## 三种来源的真相（必读）

物理存储层只有 **3 套真实数据**，但口语上常被说成 4 种，最容易踩的坑在这里：

| 口语说法 | 真实物理路径 | `--source` |
| --- | --- | --- |
| Cursor（内置 Agent **和** Claude Code 扩展是同一份） | `~/.cursor/projects/<proj>/agent-transcripts/<uuid>/<uuid>.jsonl` | `cursor` |
| Claude Code CLI（终端 `claude` 命令） | `~/.claude/projects/<proj-slug>/<uuid>.jsonl` | `claude` |
| Claude Code 桌面版（claude.ai macOS App） | `~/Library/Application Support/Claude/` 只有几 KB UI 缓存，**对话在云端** | 本地无源 |

- Cursor 里的内置 Agent 和 Claude Code 扩展，在磁盘上是**同目录、同结构、同 `role + message.content` 格式**，没有任何字段能区分。所以本 skill 把它们合并成一个来源 `cursor`。
- Claude Code 桌面版对话存在 claude.ai 云端，本地导不出来。要迁移只能先在 claude.ai 网页「设置 → 导出数据」拿 JSON 包。

## 快速开始

### 安装

```bash
mkdir -p ~/.cursor/agent-shared/skills
cd ~/.cursor/agent-shared/skills
git clone https://github.com/PMtfo/ccc-syn-skill.git ccc-syn-skill

# 三端 skill 注册目录建软链，让 Cursor / Claude Code / Codex 都能加载
mkdir -p ~/.cursor/skills ~/.claude/skills ~/.codex/skills
ln -sfn ~/.cursor/agent-shared/skills/ccc-syn-skill ~/.cursor/skills/ccc-syn-skill
ln -sfn ~/.cursor/agent-shared/skills/ccc-syn-skill ~/.claude/skills/ccc-syn-skill
ln -sfn ~/.cursor/agent-shared/skills/ccc-syn-skill ~/.codex/skills/ccc-syn-skill
```

依赖：Python 3.9+（只用标准库，无第三方包）。

### 用法一：读对话，接着上一次的任务继续

```bash
cd ~/.cursor/agent-shared/skills/ccc-syn-skill

# 按标题 / 关键词 / 会话 ID 搜索（三端各一套脚本）
python3 scripts/cursor_transcript.py search "<标题或关键词或 uuid>" --limit 5
python3 scripts/claude_transcript.py search "<标题或关键词或 session-id>" --limit 5
python3 scripts/codex_transcript.py  search "<标题或关键词或 session-id>" --limit 5

# 命中后加载全文（Markdown）
python3 scripts/cursor_transcript.py load "<cursor-uuid>" --max-chars 40000
python3 scripts/claude_transcript.py load "<session-id>"  --max-chars 40000
python3 scripts/codex_transcript.py  load "<session-id>"  --max-chars 40000
```

`load` 的输出就是一段 Markdown，直接丢给当前 Agent 当历史上下文即可。

### 用法二：批量导入对话到 Codex

唯一推荐入口是 `codex-import/codex-planB-run.py`：

```bash
cd ~/.cursor/agent-shared/skills/ccc-syn-skill

python3 codex-import/codex-planB-run.py dry-run --source cursor --codex-cwd "$HOME/CodexWorkspace"
python3 codex-import/codex-planB-run.py import  --source cursor --codex-cwd "$HOME/CodexWorkspace" --incremental
python3 codex-import/codex-planB-run.py import  --source claude --codex-cwd "$HOME/CodexWorkspace" --incremental
python3 codex-import/codex-planB-run.py verify  --codex-cwd "$HOME/CodexWorkspace"
```

**导入前必须先 ⌘Q 退出 Codex 桌面版**，否则脚本会以 `ABORT: Codex still running` 拒绝执行。

常用参数：

| 参数 | 说明 |
| --- | --- |
| `--source cursor\|claude\|both` | 来源 |
| `--incremental` | 跳过 ledger 里已有相同 `source_path` 的会话 |
| `--since 7d\|24h\|2w\|YYYY-MM-DD` | 只导入此时间之后更新的对话 |
| `--from-date` / `--to-date` | 显式时间区间 |
| `--title <关键词>` | 按标题过滤 |
| `--batch <数量>` | 限制单次导入条数 |
| `--codex-cwd <绝对路径>` | 导入后在 Codex 里的工作区路径；也可用环境变量 `CCC_CODEX_PROJECT_CWD` |

`verify` 会打印 `threads under <path> = N`。`N > 0` 表示 cwd 已在 Codex SQLite 中可见，此时打开 Codex GUI → Open Folder 指向该路径，侧栏就会出现导入的对话（按更新时间倒序）。

### 导入向导（Agent 侧）

仓库里的 [SKILL.md](SKILL.md) 定义了 Agent 触发本 skill 后的 **七步导入向导**：

1. **来源确认** —— 三选一，把上面「来源真相」表讲给用户听
2. **导出位置** —— 确认目标 cwd 存在，不存在先问再建，不静默 `mkdir`
3. **模式与筛选** —— 增量 / 全部，可选时间或标题过滤
4. **最终确认** —— `pgrep` 检查 Codex 已退出 + 人话复述摘要 + 可选 dry-run
5. **真实导入** —— 执行 `import`
6. **可识别性校验** —— 跑 `verify`，看 SQLite 里 cwd 下的条数
7. **收尾汇总** —— 中文一句话汇总并引导用户在 GUI 里打开

## 导入行为细节

- Cursor 导入复用 Cursor 侧栏的原始标题；Claude Code 导入优先用 JSONL 里的 `ai-title`，没有则退回首条用户消息。
- 读取范围是 `~/.cursor/projects/*/agent-transcripts` 全部项目，不是当前打开的工作区。
- 按源对话更新时间从旧到新提交，Codex 侧栏倒序显示时新对话在上、旧对话在下。
- Cursor 侧按 UUID 去重，避免多个 project 缓存重复导入。
- 自动过滤低价值测试对话：`1`、`测试`、`你好1`、`请回复ok`、`ping`、测网速 / 连接性测试 / 空白测试等。
- 导入前自动备份 `~/.codex/state_5.sqlite`、`~/.codex/external_agent_session_imports.json` 和本 skill 的 import ledger。

## 仓库结构

```
ccc-syn-skill/
├── SKILL.md                          Agent 主文件，含七步导入向导
├── README.md
├── requirements.txt                  空清单（仅标准库）
├── scripts/
│   ├── cursor_transcript.py          Cursor 对话读取
│   ├── claude_transcript.py          Claude Code CLI 对话读取
│   ├── codex_transcript.py           Codex 对话读取
│   └── codex_import.py               旧版直写 SQLite（已退役，仅存档）
├── codex-import/
│   ├── codex-planB-run.py            ★ 唯一推荐的导入入口
│   ├── codex-planB-claude-bridge.py  Cursor sidecar → Claude jsonl 格式桥接
│   ├── codex-appserver-rpc.py        Codex app-server RPC 调用封装
│   └── README.md
├── integrations/
│   ├── cursor/claude-transcript-handoff.mdc   Cursor rule
│   └── claude/CLAUDE-handoff-snippet.md       Claude Code 触发片段
├── tests/                            离线测试（不依赖真实 Codex / Cursor 安装）
└── .github/workflows/lint.yml
```

## 已知边界

- **Codex 必须是 macOS 桌面应用**，Codex CLI 没有 RPC 入口，不支持。
- `--source cursor` 一次扫光 `~/.cursor/projects/*` 下所有项目，**没有 project 级过滤**；要按项目分流到不同 cwd，需要给 `codex-planB-run.py` 加 `--cursor-project-include`（尚未实现）。
- Claude Code 桌面版对话在云端，本地无源。
- Codex 未退出时执行 `import` 会被 `ensure_quit()` 阻断。
- 所有读写都是本机数据，不调用任何外部 API。

## 安全说明

- 仓库只含脚本与文档，**没有任何对话内容、账号凭据或机器专属路径**。
- 脚本读的是你本机的对话文件，产物只写到本机（Codex SQLite / stdout）。
- 导入前会备份受影响的 Codex 数据库文件。

## License

MIT
