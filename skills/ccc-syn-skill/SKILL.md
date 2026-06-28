---
name: ccc-syn-skill
description: Cross-agent conversation handoff and Codex import for Cursor, Claude Code, and Codex. Use when the user wants to read a conversation by app/title/id to continue work, or batch import Cursor/Claude Code conversations into Codex.
---

# CCC Sync Skill

本 skill 只保留最终生效链路：

- **读取继续任务**：Cursor / Claude Code / Codex 三端互读本地对话，按标题、关键词或会话 ID 定位后渲染 Markdown 作为上下文。
- **批量导入 Codex**：只做 Cursor → Codex、Claude Code → Codex。通过 Codex 官方 `externalAgentConfig/import` RPC 导入，保证会话显示在 Codex 侧栏。

## 决策树

收到用户请求后，先判断目标：

### A. 用户要“读取某端对话，继续当前任务”

典型说法：读 Cursor/Claude/Codex 里的某篇对话、加载某个会话、接着那个任务继续、按标题/ID 找对话。

1. 询问或识别目标应用：`cursor`、`claude`/`cc`/`claudecode`、`codex`。
2. 询问或识别定位信息：对话标题、关键词或会话 ID。
3. 运行对应脚本：

```bash
# 读 Cursor 对话
python3 ~/.cursor/agent-shared/skills/ccc-syn-skill/scripts/cursor_transcript.py search "<标题或关键词或uuid>" --limit 5
python3 ~/.cursor/agent-shared/skills/ccc-syn-skill/scripts/cursor_transcript.py load "<cursor-uuid>" --max-chars 40000

# 读 Claude Code 对话
python3 ~/.cursor/agent-shared/skills/ccc-syn-skill/scripts/claude_transcript.py search "<标题或关键词或session-id>" --limit 5
python3 ~/.cursor/agent-shared/skills/ccc-syn-skill/scripts/claude_transcript.py load "<session-id>" --max-chars 40000

# 读 Codex 对话
python3 ~/.cursor/agent-shared/skills/ccc-syn-skill/scripts/codex_transcript.py search "<标题或关键词或session-id>" --limit 5
python3 ~/.cursor/agent-shared/skills/ccc-syn-skill/scripts/codex_transcript.py load "<session-id>" --max-chars 40000
```

4. 若 search 唯一强命中，直接 load；若多候选，展示候选让用户选。
5. 把 load 输出当作历史上下文，继续当前任务。加载成功后先简短确认标题和 ID。

### B. 用户要“批量导入对话到 Codex”

典型说法：导入 Cursor/Claude 对话到 Codex、同步近一周对话、增量导入、全部导入、按时间筛选导入。

**强制走七步向导**（Step 1 → Step 7）。即使用户只甩一句“把 claudecode 对话导入到 codex”，也必须先把 Step 1 / Step 2 走完再动手，不能凭直觉选 `--source`。原因见下方“来源真相”。

#### 来源真相（决策树 B 的事实底座）

物理存储层只有 3 套真实数据。必须在 Step 1 把这件事跟用户对清楚（特别要打消"Cursor 内置 Agent 和 Cursor 里的 Claude Code 扩展是两个独立来源"的错觉——它们在磁盘上是同一份）：

| 用户口语 | 真实物理路径 | 本 skill `--source` |
| -- | -- | -- |
| ① Cursor（含内置 Agent 与 Claude Code 扩展，**磁盘同结构、同目录、同 `role+message.content` 格式，无字段可分**） | `~/.cursor/projects/<proj>/agent-transcripts/<uuid>/<uuid>.jsonl` | `cursor` |
| ② Claude Code CLI（终端 `claude` 命令） | `~/.claude/projects/<proj-slug>/<uuid>.jsonl` | `claude` |
| ③ Claude Code 桌面版（claude.ai macOS App） | `~/Library/Application Support/Claude/Local Storage/leveldb/`，只有 UI 缓存（几 KB），**不含对话**；对话在云端 claude.ai | **本地无源** |

#### Step 1：来源确认（必填，用 AskUserQuestion）

用 AskUserQuestion 抛出 3 选 1（可多选，题面要把上表的真实路径放在 description 里）：

- ① Cursor（含内置 Agent + Claude Code 扩展）
- ② Claude Code CLI（终端命令）
- ③ Claude Code 桌面版（claude.ai macOS App）

按勾选映射：
- ① → 直接进 Step 2；最终走 `--source cursor`。
- ② → 直接进 Step 2；最终走 `--source claude`。
- ③ → 走【桌面版兜底文案】，本次向导结束；不要硬跑脚本。
- 多选 → 拆成多组，逐组分别跑 Step 2~6。

【桌面版兜底文案】（用户选 ③ 时原样回给用户）：
> 你选了「Claude Code 桌面版」。这部分对话存放在云端 claude.ai，
> 本地 `~/Library/Application Support/Claude/Local Storage/leveldb/`
> 只有 UI 缓存（约几 KB），不含对话正文。
> 请在 claude.ai 网页右上角「设置 → 导出数据」生成 JSON 包，
> 解压后告诉我 `conversations.json` 路径，再用 `--source claude` 路径手动落 jsonl 后导入。

**Cursor 项目分布查看（可选辅助）**：用户在 Step 2 取 cwd 名前，如果想先看一眼本机有哪些 Cursor 项目方便起名，可以临时跑 `ls ~/.cursor/projects/` 给用户参考；但**不要把它当成一步必跑流程**——目前 `codex-planB-run.py` 也没有 project 级过滤参数，列出来也只是看不能选。

#### Step 2：导出位置确认（目标 codex_cwd）

每组独立问一个 cwd。用 AskUserQuestion 收路径，然后跑：

```bash
test -d "<path>" && echo OK || echo MISSING
```

- 返回 OK → 进 Step 3。
- 返回 MISSING → 再用 AskUserQuestion 问“是否创建 <path>？”：
  - 同意 → `mkdir -p "<path>"`。
  - 拒绝 → 整组中止，不进 Step 3。
  - **不要静默 mkdir**。

#### Step 3：导入模式 + 可选筛选

- 增量 / 全部（沿用现状）。
- `--since` / `--from-date` / `--to-date` / `--title`：按需问，不强问。

#### Step 4：执行前最终确认（必跑）

1. 检查 Codex GUI 是否已退出：
   ```bash
   pgrep -fl "Codex.app/Contents/MacOS/Codex"; pgrep -fl "Resources/codex app-server"
   ```
   有进程 → 提示用户 `Cmd+Q`，等用户回“已退出”再继续。
2. 用人话给用户复述一份摘要：每组的 source / Cursor 项目 / 目标 cwd / 增量 or 全部 / 筛选条件。
3. （可选）先跑一次 dry-run 拿预计条数：
   ```bash
   python3 codex-import/codex-planB-run.py dry-run --source <cursor|claude> --codex-cwd "<path>" [筛选]
   ```
4. 用户最终一句“开始”再进 Step 5。

#### Step 5：真实导入（每组分别跑）

```bash
cd ~/.cursor/agent-shared/skills/ccc-syn-skill

python3 codex-import/codex-planB-run.py import \
  --source <cursor|claude> \
  --codex-cwd "<path>" \
  [--incremental] \
  [--since 7d|--from-date YYYY-MM-DD|--to-date YYYY-MM-DD|--title 关键词]
```

可用参数：

- `--source cursor|claude|both`
- `--incremental`
- `--since 7d|24h|2w|YYYY-MM-DD`
- `--from-date YYYY-MM-DD`
- `--to-date YYYY-MM-DD`
- `--title <关键词>`
- `--batch <数量>`
- `--codex-cwd <Codex工作区路径>`

cwd 也可用环境变量指定（命令行 `--codex-cwd` 优先）：

```bash
export CCC_CODEX_PROJECT_CWD="$HOME/Desktop/Codex"
```

#### Step 6：cwd 可识别性验证（必跑）

每组导入完立即跑：

```bash
python3 codex-import/codex-planB-run.py verify --codex-cwd "<path>"
```

把 `verify: threads under <path> = N` 原样给用户。

- `N > 0` → cwd 确实在 Codex SQLite 中可见，告诉用户：“打开 Codex GUI → 在主界面 Open Folder 指向 `<path>` → 侧栏应出现 N 条对话（按更新时间倒序）”。
- `N = 0` → 走【cwd 排错清单】：
  1. Codex GUI 没启动 → 启动后强制 Open Folder 指向 `<path>`。
  2. 路径拼写不一致（尾巴有/无 `/`，大小写）→ 用 `ls "<path>"` 复核绝对路径。
  3. ledger 里 `codex_cwd` 为 None（老版历史污染）→ 先 `reset --source <src>`，再重跑 Step 5。

#### Step 7：收尾汇总

用一段中文向用户汇报每组的 `(来源类型, 项目目录或 source, codex_cwd, 写入条数)`，并提醒下一步在 Codex GUI 怎么打开。

## 导入规则

- Cursor 导入复用 Cursor 侧栏原始标题；Claude Code 导入优先复用 Claude JSONL 内的 `ai-title`，否则退回首条用户消息。
- Cursor 读取范围是所有 `~/.cursor/projects/*/agent-transcripts`，不是当前打开工作区。
- 导入按源对话更新时间从旧到新提交；Codex 侧栏倒序显示时，新对话在上、旧对话在下。
- 增量导入会跳过 Codex import ledger 中已有的相同 `source_path`。
- Cursor 导入按 UUID 去重，避免多个 Cursor project 缓存重复。
- 无意义测试对话会过滤：`1`、`测试`、`你好1`、`请回复ok`、`ping`、测网速/连接性测试/空白测试等。
- 导入前会备份 `~/.codex/state_5.sqlite`、`~/.codex/external_agent_session_imports.json` 和本 skill 的 import map。
- **Claude Code 桌面版本地无对话源**：`~/Library/Application Support/Claude/` 下只有几 KB 的 UI 缓存（leveldb），对话存放在云端 claude.ai；要导入必须先从 claude.ai 网页「设置 → 导出数据」拿到 JSON 包，再人工指定路径。
- **Cursor 内置 Agent 与 Cursor 里的 Claude Code 扩展在磁盘上是同一份**：同一 `~/.cursor/projects/*/agent-transcripts/` 目录、同一 `role + message.content` 格式，transcript 内容无字段可区分。因此本 skill 把它们合并为一个来源 `--source cursor`，**不再向用户呈现"扩展 vs 内置"的二选一**。如需按 Cursor project 分流，当前 `codex-planB-run.py` 还没加 `--cursor-project-include` 过滤，留作后续。

## 非目标

- 不把 Codex 对话导入 Cursor/Claude。
- 不伪造任一端的原生 resume 会话；读取只生成 Markdown 上下文。
- 不再对外推荐直写 `state_5.sqlite` 或旧版 `scripts/codex_import.py cursor --all` 路径；它只作为内部工具/兼容脚本保留。
