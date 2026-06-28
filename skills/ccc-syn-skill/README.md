# ccc-syn-skill

Cursor、Claude Code、Codex 三端对话接力 + 批量导入 Codex 的 macOS 本地 skill。

> **本仓库面向我换机/还原使用**。私有,不对外。clone 下来按「换机部署」章节直接恢复即可。

---

## 这个 skill 干两件事

### 1. 跨端读对话,继续上一次任务

不管对话在 Cursor 项目侧栏、Claude Code CLI 历史、还是 Codex 历史里,用标题/关键词/会话 ID 搜出来,渲染成 Markdown 作为当前对话的上下文。

### 2. 把 Cursor / Claude Code CLI 的对话批量导入 Codex 侧栏

通过 Codex 官方 `externalAgentConfig/import` RPC 写库,会话直接出现在 Codex 侧栏,按更新时间倒序。

**不做反向迁移**(Codex → Cursor/Claude),也**不做 Cursor ↔ Claude 原生导入**。

---

## 三种来源的真相(必读,踩过坑)

物理存储层只有 **3 套真实数据**,但口语上常被说成 4 种,容易踩坑:

| 用户口语 | 真实物理路径 | `--source` |
| -- | -- | -- |
| ① Cursor(**内置 Agent 和 Claude Code 扩展同一份**,磁盘上同结构、同 `role+message.content` 格式,无字段可分) | `~/.cursor/projects/<proj>/agent-transcripts/<uuid>/<uuid>.jsonl` | `cursor` |
| ② Claude Code CLI(终端 `claude` 命令) | `~/.claude/projects/<proj-slug>/<uuid>.jsonl` | `claude` |
| ③ Claude Code 桌面版(claude.ai macOS App) | `~/Library/Application Support/Claude/` 只有 UI 缓存,**对话在云端 claude.ai**,本地无源 | **本地不可导** |

桌面版要导入只能靠 claude.ai 网页「设置 → 导出数据」拿 JSON 后人工转 jsonl。

---

## 七步导入向导(SKILL.md 决策树 B 的核心)

| Step | 内容 |
| -- | -- |
| **1** 来源确认 | 3 选 1(可多选):① Cursor / ② Claude Code CLI / ③ 桌面版(走兜底文案) |
| **2** 导出位置 | 询问目标 `--codex-cwd`,`test -d` 不存在再问"是否创建",**不静默 mkdir** |
| **3** 模式+筛选 | 增量/全部;可选 `--since/--from/--to/--title` |
| **4** 最终确认 | `pgrep` 检查 Codex 已退出;摘要复述;可选 dry-run;用户最后一句"开始" |
| **5** 真实导入 | 跑 `codex-planB-run.py import --source ... --codex-cwd ...` |
| **6** 可识别校验 | 跑 `verify`,看 SQLite `threads` 表里 cwd 下条数 > 0 才算成功 |
| **7** 收尾 | 中文一句话汇总 `(来源, cwd, 写入条数)` + 引导用户在 Codex GUI Open Folder |

详细步骤、AskUserQuestion 提示语、兜底文案、排错清单见 [SKILL.md](SKILL.md)。

---

## 命令速查

```bash
cd ~/.cursor/agent-shared/skills/ccc-syn-skill

# 读对话(继续任务用)
python3 scripts/cursor_transcript.py search "<标题/关键词/uuid>" --limit 5
python3 scripts/cursor_transcript.py load "<cursor-uuid>" --max-chars 40000

python3 scripts/claude_transcript.py search "<标题/关键词/session-id>" --limit 5
python3 scripts/claude_transcript.py load "<session-id>" --max-chars 40000

python3 scripts/codex_transcript.py search "<标题/关键词/session-id>" --limit 5
python3 scripts/codex_transcript.py load "<session-id>" --max-chars 40000

# 批量导入(Codex 必须先 Cmd+Q)
python3 codex-import/codex-planB-run.py dry-run --source cursor --codex-cwd "$HOME/Desktop/Codex"
python3 codex-import/codex-planB-run.py import  --source cursor --codex-cwd "$HOME/Desktop/Codex" --incremental
python3 codex-import/codex-planB-run.py import  --source claude --codex-cwd "$HOME/Desktop/Codex" --incremental
python3 codex-import/codex-planB-run.py reset   --source cursor       # 仅清掉 cursor 历史 ledger 与误写 thread
python3 codex-import/codex-planB-run.py verify  --codex-cwd "$HOME/Desktop/Codex"
```

可用参数:

- `--source cursor|claude|both`
- `--incremental` 跳过 Codex import ledger 中已有相同 source_path 的会话
- `--since 7d|24h|2w|YYYY-MM-DD` / `--from-date` / `--to-date` / `--title` / `--batch`
- `--codex-cwd <绝对路径>` 真实导入前必须让用户确认这条路径就是 Codex GUI 里要 Open Folder 的工作区

环境变量 `CCC_CODEX_PROJECT_CWD` 可代替 `--codex-cwd`(命令行优先)。

---

## 换机部署

### 0. 本机前置(假设新 Mac)

| 工具 | 必需 | 安装 | 备注 |
| -- | -- | -- | -- |
| **Codex.app**(OpenAI macOS App) | 是 | <https://chat.openai.com/codex> 下载 | 至少启动一次,生成 `~/.codex/state_5.sqlite` |
| **Claude Code CLI** | 可选(只在用 Claude Code CLI 当源时必需) | `npm i -g @anthropic-ai/claude-code` | 至少跑一次 `claude` 让 `~/.claude/projects/` 出现 |
| **Cursor** | 可选(只在用 Cursor 当源时必需) | <https://cursor.com> | 至少在一个项目里用过 Agent,让 `~/.cursor/projects/*/agent-transcripts/` 出现 |
| **Python 3.9+** | 是 | macOS 自带 / `brew install python` | 脚本只用标准库 |
| **gh** | 可选(用来拉这个 repo) | `brew install gh` | `gh auth login` |

### 1. 拉 repo + 安到 skill 注册目录

```bash
mkdir -p ~/.cursor/agent-shared/skills
cd ~/.cursor/agent-shared/skills
gh repo clone PMtfo/ccc-syn-skill ccc-syn-skill

# 三端 skill 注册目录的软链(让 Cursor / Claude / Codex 都能加载)
mkdir -p ~/.cursor/skills ~/.claude/skills ~/.codex/skills
ln -sfn ~/.cursor/agent-shared/skills/ccc-syn-skill ~/.cursor/skills/ccc-syn-skill
ln -sfn ~/.cursor/agent-shared/skills/ccc-syn-skill ~/.claude/skills/ccc-syn-skill
ln -sfn ~/.cursor/agent-shared/skills/ccc-syn-skill ~/.codex/skills/ccc-syn-skill
```

### 2. 校验脚本能跑

```bash
cd ~/.cursor/agent-shared/skills/ccc-syn-skill
python3 -m py_compile scripts/*.py codex-import/*.py
python3 codex-import/codex-planB-run.py --help
```

### 3. 第一次跑导入

按 [SKILL.md](SKILL.md) 的 7 步向导走。先 `dry-run` 看条数,再 `import`,最后 `verify` 看 cwd 下 threads 条数 > 0,然后在 Codex GUI 里 Open Folder 指向你选的 cwd,侧栏就会出现导入的会话。

---

## 不会推到这个仓库的本机数据

`.gitignore` 已经把以下文件挡掉(它们是各机器独立的运行时状态,不该跨机同步):

- `~/.codex/state_5.sqlite`(Codex 本机数据库)
- `~/.codex/external_agent_session_imports.json`(Codex 全局 import ledger)
- `~/.codex/ccc-syn-import-map.json`(本 skill 自己的 import ledger)
- `~/.codex/cursor-claude-bridge-map.json`(Cursor sidecar map)
- `~/.claude/projects/`、`~/.cursor/projects/`(对话原始数据,各机器自然不同)

换机后这些会重新生成。**最好的迁移姿势是:在新机器自然产生新对话,而不是搬运旧机器的对话数据库**。

---

## 已知边界

- Codex 必须用 macOS 应用,不支持 Codex CLI(它没有 RPC 入口)。
- `--source cursor` 一次扫光 `~/.cursor/projects/*` 所有项目,**没有 project 级过滤**;要按项目分流到不同 cwd,需先给 `codex-planB-run.py` 加 `--cursor-project-include` 参数(待实现)。
- 桌面版 Claude(claude.ai macOS App)对话在云端,本地无源,只能靠 claude.ai 数据导出 JSON 后人工落 jsonl 再走 `--source claude`。
- Codex GUI 不退出时执行 `import` 会被 `ensure_quit()` 阻断报 `ABORT: Codex still running. Cmd+Q first.`。

---

## 文件结构

```text
ccc-syn-skill/
├── SKILL.md                              # Cursor/Claude/Codex 三端的 skill 主文件,含 7 步向导
├── README.md                             # 本文件
├── requirements.txt                      # 实际仅用标准库,留空清单
├── .gitignore
├── .github/workflows/lint.yml            # 最小 Python lint
├── scripts/
│   ├── cursor_transcript.py              # Cursor 对话读取
│   ├── claude_transcript.py              # Claude Code CLI 对话读取
│   ├── codex_transcript.py               # Codex 对话读取
│   └── codex_import.py                   # 旧版直写 SQLite 工具(已退役为内部兼容)
├── codex-import/
│   ├── codex-planB-run.py                # ★ 唯一推荐 Codex 导入入口
│   ├── codex-planB-claude-bridge.py      # Cursor sidecar → Claude jsonl 格式桥接
│   ├── codex-appserver-rpc.py            # Codex app-server RPC 调用封装
│   └── README.md
├── integrations/                         # Cursor rule / Claude command / handoff snippet
└── tests/                                # 离线最小测试(不依赖 Codex/Claude 真实安装)
```

---

## 引用与起源

本 skill 基于本人 Cursor / Claude Code / Codex 三端日常使用习惯沉淀,核心是利用 Codex `externalAgentConfig/import` RPC 把外部会话(Cursor 项目对话、Claude Code CLI 对话)伪装成 Codex 原生会话写入侧栏。所有读写都是本机数据,无外部 API 调用。
