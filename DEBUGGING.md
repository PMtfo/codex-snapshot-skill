# DEBUGGING.md — 踩坑完整复盘

> 2026-06-27 一晚上把 codex-snapshot skill 从 v1 干到 v2 的全部弯路。把每一步走错的根因记下来，下次遇到 Codex 类似怪相能 30 秒定位，不用再挖一晚。

## TL;DR

| # | 走错的方向 | 真因 | 正解 |
|---|---|---|---|
| 1 | 以为换号是 OpenAI 账号绑定问题 | Codex 本地 schema **没有 account_id 字段**，完全跟账号解耦 | 看 listThreads 的 SQL WHERE 拼装 |
| 2 | 改 sqlite 的 `thread_source` 字段让它变 'user' | sqlite 是从 jsonl backfill 出来的，每次 Codex 启动会覆盖 | 改 jsonl 第一行 session_meta，让 backfill 写出对的值 |
| 3 | restore 时把 v1 快照里的 `state_5.sqlite` 写回 | 旧账号 A 的 sqlite 写到新账号 B 环境会"半生不熟" | restore 永远跳过 state_5.sqlite，让 Codex 自己 backfill |
| 4 | 以为 `thread_source=NULL` 是能显示的 | 测了 1 条 NULL 还是不显示，结论反了 | 实际是 `thread_source='user'` 才显示 |
| 5 | finish 只清扫 my_proxy | Codex 重启后又用了一会儿，新 thread 还是 NULL，或者 jsonl 后续 turn 里有别的 provider | finish 改为可重复跑 + UPDATE 所有非目标 provider |

完整时间线如下。

---

## 阶段 1: v1 整套打包恢复

**初始目标**：换号场景，把 ~/.codex/ 整个打包 + 解包还原。

**做法**（错的）：
- snapshot 把 ~/.codex/ 下所有文件（**包括 state_5.sqlite**）打成 tar.gz
- restore 解包覆盖回 ~/.codex/

**结果**：换号后 restore，Codex 打开 sidebar 大部分对话不显示。

**追因**：
- 截图显示左侧"项目周报"等分组在，但下面"暂无对话"
- 数据明明在 sqlite 里（`SELECT count(*) FROM threads WHERE cwd='/Users/you/Documents/项目周报'` = 8）
- 但 Codex.app 不显示

---

## 阶段 2: 在 sqlite 里改字段（一通乱试）

**假设 A**：是 `model_provider` 过滤导致

实测 sqlite 里 thread.model_provider = `my_proxy`（账号 A 时期公司 API），但 B 账号 config.toml 里没这个 provider 配置，sidebar UI 按 model_provider 过滤把它们隐藏了。

**操作**：UPDATE threads SET model_provider='openai' WHERE model_provider='my_proxy'。

**结果**：稍好，但启动 5 秒后大部分对话还是消失。

**假设 B**：是 `thread_source` 字段问题

观察到能显示的 4 个 thread 的 `thread_source = NULL`，不能显示的 21 个 `thread_source = 'user'`。

**操作**：UPDATE threads SET thread_source = NULL WHERE thread_source = 'user'。

**结果**：sidebar 一片空白。**反了**。

**假设 C**：批量改 jsonl 的 model_provider

把所有 jsonl 第一行 `session_meta.payload.model_provider` 从 my_proxy 改为 openai。

**结果**：Codex 启动后 sqlite 还是被改回 cato。原因：Codex backfill 不仅看 session_meta 第一行，还扫整个 jsonl 找 model_provider，最后一次出现的会覆盖第一行的。

---

## 阶段 3: 调研 GitHub + 网上方案，找到真正机制

转折点：在网上看到一个"换号教程"提到："只删 state_5.sqlite，保留 sessions/，Codex 重启会自动重建"。

去 OpenAI Codex 官方仓库挖源码：

### 关键源码引用

| 文件 | 关键代码 | 说明 |
|---|---|---|
| [`codex-rs/state/src/lib.rs`](https://github.com/openai/codex/blob/main/codex-rs/state/src/lib.rs) | `STATE_DB_FILENAME = "state_5.sqlite"` | 默认文件名常量 |
| [`codex-rs/rollout/src/metadata.rs`](https://github.com/openai/codex/blob/main/codex-rs/rollout/src/metadata.rs) | `backfill_sessions_with_lease` | 扫 sessions/ + archived_sessions/ 逐个 upsert 到 threads 表 |
| [`codex-rs/state/src/runtime/threads.rs:538`](https://github.com/openai/codex/blob/main/codex-rs/state/src/runtime/threads.rs) | `insert_thread_if_absent` | INSERT SQL，30 字段，`ON CONFLICT(id) DO NOTHING` |
| [`codex-rs/state/src/extract.rs`](https://github.com/openai/codex/blob/main/codex-rs/state/src/extract.rs) | `apply_session_meta_from_item` | 从 jsonl 第一行抽 metadata 喂给 threads 表 |
| [`codex-rs/cli/src/state_db_recovery.rs`](https://github.com/openai/codex/blob/main/codex-rs/cli/src/state_db_recovery.rs) | 注释: `"Moving the damaged local database aside so Codex can rebuild it from saved data."` | **官方明确说 sqlite 可以删让 Codex 重建** |

### 关键 GitHub issue

| Issue | 说明 |
|---|---|
| [#28068](https://github.com/openai/codex/issues/28068) | Windows 用户报告删 state_5.sqlite 后 backfill 失败，sidebar 全空（still OPEN） |
| [#27363](https://github.com/openai/codex/issues/27363) | "Codex Desktop loses visible chat history when local state SQLite index is corrupted" |
| [#27159](https://github.com/openai/codex/issues/27159) | "hides active local threads from sidebar while state_5.sqlite still marks them unarchived" |
| [#28549](https://github.com/openai/codex/issues/28549) | "Sessions flash then disappear after model_provider migration" — **跟我们 5 秒后消失现象一模一样** |
| [#30107](https://github.com/openai/codex/issues/30107) | VSCode 扩展重装后历史不恢复 |
| [#30042](https://github.com/openai/codex/issues/30042) | state runtime migration 失败导致对话被截断（已修复） |

### 同类工具

| 仓库 | 思路 |
|---|---|
| [Red-noblue/Codex_Relay](https://github.com/Red-noblue/Codex_Relay) (155⭐) | Tauri GUI，跨设备打包 zip + 改 session_id resume |
| [GODGOD126/codex-history-sync-tool](https://github.com/GODGOD126/codex-history-sync-tool) | 同步改 sqlite + jsonl 的 model_provider |
| [pangkk18/codex-history-sync](https://github.com/pangkk18/codex-history-sync) | 同上 |
| [CoimgRain/codex-history-sync-tool-mac-account-api-switch](https://github.com/CoimgRain/codex-history-sync-tool-mac-account-api-switch) | 专门换 API 账号 |

---

## 阶段 4: v2 设计的关键决策

读懂源码 + 实测后定下的设计：

### 决策 1: snapshot 永远不打 state_5.sqlite

**为什么**：state_5.sqlite 是 Codex 启动时从 sessions/ jsonl 实时 backfill 出来的，**它在不同账号环境下的内容会不同**。把旧账号的 sqlite restore 到新账号会产生混乱状态（既不是新账号的、也不是真旧账号的）。

放在 `exclude.txt` 永久排除：

```
auth.json
state_5.sqlite
state_5.sqlite-shm
state_5.sqlite-wal
cache/
session_index.jsonl
sqlite/
```

### 决策 2: restore 加 RESTORE_BLACKLIST 二次过滤

即使旧快照里有 state_5.sqlite（v1 时期打的快照），restore 也**永远跳过**。在 `restore.py` 里加了硬编码黑名单：

```python
RESTORE_BLACKLIST = {
    "state_5.sqlite", "state_5.sqlite-shm", "state_5.sqlite-wal",
    "session_index.jsonl", "auth.json",
}
RESTORE_BLACKLIST_PREFIXES = ("cache/", "sqlite/")
```

### 决策 3: account-switch 改成双阶段

不能在一个命令里完成，因为中间需要用户重启 Codex 让 backfill 跑完。

- **prepare**：改所有 jsonl 的 model_provider → 目标 provider；mv state_5.sqlite + cache 到 trash；自动 pre-account-switch 全量快照保险
- **(用户重启 Codex)**
- **finish**：UPDATE thread_source NULL → 'user'；UPDATE 残留 model_provider → 目标；从 pre-snapshot merge title 列

### 决策 4: finish 可重复跑

用户重启 Codex 后又用了一会儿，sidebar 可能又少几条（backfill 拉进新 thread，jsonl 后续 turn 又把旧 provider 带回 sqlite）。finish 必须可以独立重复跑兜底：

- `hint.json` 可选（没跑过 prepare 也能跑 finish 清扫）
- `--provider` 显式参数
- title merge 容错（pre_snapshot 不存在就跳过）

---

## 阶段 5: 最终验证

修复后的状态（实测）：

```
$ sqlite3 ~/.codex/state_5.sqlite "
SELECT thread_source, model_provider, count(*) FROM threads
WHERE archived=0 AND preview <> '' GROUP BY 1,2"

subagent | openai | 1
user     | openai | 492
```

**全部 492 条对话在 sidebar 完整持久显示，不再 5 秒后消失**。

---

## 经验汇总

### 关于 Codex 内部机制

1. **sidebar 拉对话是 SQL 过滤**：`threads.archived=0 AND threads.preview<>'' AND threads.source IN (...) AND threads.model_provider IN (...) AND threads.cwd IN (...)`。任一条件不匹配，sidebar 隐藏。
2. **thread_source 字段**：sqlite 列名，UI 上叫"sourceKinds"。NULL 等于"非交互式 source"会被默认过滤。`'user'` 是显示的关键值。
3. **state_5.sqlite 是可重建的索引**：删掉 + Codex 启动 + backfill_sessions_with_lease。30 秒超时（[#28068](https://github.com/openai/codex/issues/28068)）。
4. **本地数据跟 OpenAI 账号物理上解耦**：jsonl / sqlite schema 都没有 account_id 字段。换号"消失"纯属 UI 过滤。

### 关于这次踩坑

1. **改 sqlite 而不改 jsonl 是徒劳的** —— 下次 Codex 启动 backfill 会覆盖。
2. **改 jsonl 而不删 sqlite 也是徒劳的** —— Codex 不会主动 rescan sessions。
3. **正确顺序**：改 jsonl → 删 sqlite → Codex 重启 backfill → UPDATE 残留。
4. **永远做保险快照**：每次破坏性操作前先 snapshot 留底。这次踩坑期间救了我 N 次。

### 给未来自己的 checklist

遇到 Codex 怪相，按这个顺序排查：

- [ ] `~/Library/Logs/com.openai.codex/` 最新日志里有没有 `error` / `migration` / `backfill`
- [ ] `sqlite3 ~/.codex/state_5.sqlite "SELECT count(*) FROM threads"` 是否合理
- [ ] `sqlite3 ~/.codex/state_5.sqlite "SELECT thread_source, model_provider, count(*) FROM threads GROUP BY 1,2"` 看分布
- [ ] `find ~/.codex/sessions -name '*.jsonl' | wc -l` 看物理文件数
- [ ] 跑 `account-switch finish` 兜底清扫
- [ ] 实在搞不定再考虑 restore 到上一个保险快照

---

## 引用列表

- 官方仓库：https://github.com/openai/codex
- 关键 issue：[#28068](https://github.com/openai/codex/issues/28068)、[#27363](https://github.com/openai/codex/issues/27363)、[#27159](https://github.com/openai/codex/issues/27159)、[#28549](https://github.com/openai/codex/issues/28549)、[#30107](https://github.com/openai/codex/issues/30107)、[#30042](https://github.com/openai/codex/issues/30042)、[#30028](https://github.com/openai/codex/issues/30028)
- 同类工具：[Codex_Relay](https://github.com/Red-noblue/Codex_Relay)、[codex-history-sync-tool](https://github.com/GODGOD126/codex-history-sync-tool)、[pangkk18/codex-history-sync](https://github.com/pangkk18/codex-history-sync)
- 配套：本仓库的 [ccc-syn-skill](skills/ccc-syn-skill/) 把 Cursor / Claude Code 对话导入 Codex
