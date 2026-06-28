---
name: codex-snapshot
description: 快照、恢复、跨账号迁移本地 Codex（macOS GUI 应用）的工作状态。支持三个核心场景:(1) 日常存档/版本回滚 (snapshot/restore),(2) **换 OpenAI 账号后让旧账号对话在新账号下重新可见 (account-switch)**,(3) 跨设备/跨机器迁移。当用户说"快照 codex"、"codex 备份"、"换账号前保存 codex"、"恢复 codex 版本"、"codex 换号了"、"换号后对话不见了"、"B 账号看不到 A 账号的对话"、"列出 codex 快照"、"删除 codex 快照"时触发。永远不快照/恢复 auth.json / state_5.sqlite / cache,这些由 Codex 启动自动重建。Claude Code / Cursor / Codex 三端共用同一份脚本。
---

# codex-snapshot — Codex 状态快照与恢复

## 一句话总结

`~/.codex/` 下的所有"用户内容性"东西(配置/对话/agents/memories)打包成带版本号的本地快照,换账号后一键恢复。**只缺登录态,需要你用新账号重新登录 Codex 本体**。

## 数据事实

- Codex.app(macOS GUI)把数据放在 `~/.codex/`,共 ~57 个文件/目录。
- `codex logout` **只删 `auth.json` 一个文件**(OpenAI 源码 `codex-rs/login/src/auth/manager.rs:864` 已确认),其他用户内容不会丢。
- 用户感觉"换号丢东西"是因为 Codex 按账号 metadata 过滤会话列表,**不是数据真丢**。
- 但保险起见,本 skill 仍然完整快照所有用户内容,确保任意情况下都能精确还原。

## 子命令速查

| 命令 | 行为 |
|---|---|
| `snapshot <version> [--force] [--prune-logs <days>] [--allow-while-running]` | 创建快照。版本号正则 `^[\w.-]+$`(例 `codex-0627`)。**默认要求先退出 Codex.app**;`--allow-while-running` 才能在运行中快照。**v2 起永不快照 state_5.sqlite / cache / session_index.jsonl / auth.json**(这些由 Codex 启动自动重建,跨账号/跨快照不能用旧索引覆盖)。 |
| `restore <version> [--force-while-running]` | 恢复快照。**强制**先做 `pre-restore-<ts>` 自动快照,再 diff manifest,有差异要求输入 `yes` 才覆盖。**v2 起永远跳过 state_5.sqlite / auth.json / cache/**(即使旧快照里有)。 |
| **`account-switch prepare [--provider openai]`** | **v2 新增**。换 OpenAI 账号后让旧账号对话在新账号下重新可见 - 阶段 1。先全量保险快照,然后批量改 sessions/+archived_sessions/ 所有 jsonl 的 model_provider 字段为目标值(默认 `openai`),最后将 state_5.sqlite 三件套 + cache/ + session_index.jsonl 移动到 trash。**要求 Codex.app 已退出**。 |
| **`account-switch finish [--title-from <ver>]`** | **v2 新增**。阶段 2,要求 prepare 跑完且用户已重启 Codex 让 backfill 跑完。三件清扫:① UPDATE thread_source NULL→'user';② UPDATE model_provider→目标 provider;③ 从 pre-account-switch 快照 merge title 列。 |
| `list` | 按时间倒序列出所有快照。 |
| `show <version>` | 输出某快照详情。 |
| `delete <version>` | 软删除到 `~/.codex-snapshots/.trash/<version>-<ts>/`,30 天后真清。 |

## 调用入口

统一 Python 入口,放在 `~/agent-bootstrap/skills/codex-snapshot/scripts/`:

```bash
SS=~/agent-bootstrap/skills/codex-snapshot/scripts

# 日常
python3 $SS/snapshot.py <version> [--force] [--prune-logs N] [--allow-while-running]
python3 $SS/restore.py  <version> [--force-while-running]
python3 $SS/list.py
python3 $SS/show.py     <version>
python3 $SS/delete.py   <version>

# 换号
python3 $SS/account_switch.py prepare [--provider openai]
# (用户重启 Codex.app 等 30 秒)
python3 $SS/account_switch.py finish  [--title-from <ver>]
```

## 触发与参数解析

**触发词**:
- **快照**:快照 codex / codex 备份 / 换账号前保存 codex
- **恢复版本**:恢复 codex(版本/版本号) / 回滚 codex 到 codex-xxxx
- **换号恢复**:codex 换号了 / 换号后对话不见了 / B 账号看不到 A 账号的对话 / codex 切账号 / codex 同步对话
- **列**:列出 codex 快照 / codex 都有哪些版本
- **详情**:show codex 快照 codex-xxxx
- **删除**:删除 codex 快照 codex-xxxx

**版本号解析**:
- 用户原话里匹配 `codex-\S+` 或"版本号 X" → 直接用。
- 用户没给版本号且是 `snapshot` 操作 → fallback 用 `codex-YYYYMMDD-HHMM`(以本地时间生成),**必须先回显给用户确认才执行**。
- `restore/show/delete` 必须明确指定版本号,没说就让用户先 `list`。
- `account-switch prepare` 不需要版本号,fallback `pre-account-switch-<ts>` 自动生成保险快照。

## 关键行为约定(不可跨)

1. **永远不快照 `auth.json` / `state_5.sqlite*` / `cache/` / `session_index.jsonl` / `sqlite/`**。这些是登录态/本地索引,Codex 启动会从 sessions/*.jsonl 自动重建,跨账号/跨快照必须让 Codex 自己重建,不能用旧索引覆盖。
2. **snapshot 默认要求先退出 Codex.app**:Codex 在运行时直接拒绝并提醒用户 ⌘Q 退出后重跑,只有显式 `--allow-while-running` 才在线快照(走 sqlite backup API)。
3. **恢复前永远先做 pre-restore 自动快照**(不可关)。
4. **有任何 manifest 差异都必须停下来要求用户输 `yes`,不可静默覆盖**。
5. **restore 永远跳过 RESTORE_BLACKLIST**(state_5/auth.json/cache/),即使旧快照里有这些文件。
6. **account-switch 是双阶段命令**,prepare 和 finish 之间必须用户重启 Codex.app 让 backfill 跑完。Codex.app 运行中拒绝跑这两个阶段任何一个。
7. **跨机恢复时机器指纹文件不动**(installation_id 等,在 exclude 中)。
8. **memories git HEAD 变更要二次确认**。

## 用户体验建议(主 Agent 收到触发词时主动提示)

**场景 A:用户说"快照 codex"**

> "建议先彻底退出 Codex.app(⌘Q)再开始快照,这样能拿到 sqlite WAL 已合并的真.干净版本。如果不方便退出,可以加 `--allow-while-running` 在运行中快照。"

**场景 B:用户说"恢复 codex 版本 X"**

直接 `restore.py X`,流程内置 pre-restore + diff 提示。

**场景 C:用户说"codex 换号了" / "换号后对话不见了" / "切换账号"**

这是 **account-switch 双阶段流程**,主 Agent 必须按以下脚本引导:

> "我会分两步帮你处理换号后旧对话不显示的问题:
>
> **第一步**(我现在跑):
> - 自动做一份保险快照 `pre-account-switch-<时间戳>`
> - 批量改 sessions 下所有 jsonl 的 model_provider 为 openai (默认 B 账号)
> - 把旧的 state_5.sqlite + cache 移动到 trash(不真删)
>
> **请你做的事**(第一步跑完后):
> - ⌘Q 退出 Codex.app
> - 重新打开 Codex.app, 等 20-30 秒(Codex 会自动 backfill 重建索引)
> - 此时 sidebar 可能只显示几条对话, 这是正常的
> - 回来告诉我"重启完了"
>
> **第二步**(收尾,等你重启完 Codex 跑):
> - UPDATE thread_source NULL → 'user'
> - UPDATE 残留的 cato/旧 provider → openai
> - 从保险快照 merge title 列(让标题回到 LLM 总结版,不是 first_user_message)
>
> 跑完后请重启 Codex 验证 sidebar 是否完整。"

主 Agent 第一阶段直接跑 `account_switch.py prepare`,等用户回信"重启完了"再跑 `account_switch.py finish`。**不要把两个阶段合一**。

## 决策树(Agent 收到触发词时按此走)

```
用户说话命中触发词
  ├─ 提到"换号了/换账号后/对话不见了/切账号"
  │   └─ account-switch 双阶段流程(见上文"场景 C")
  │       step1: account_switch.py prepare [--provider openai]
  │       (等用户回信"重启完了")
  │       step2: account_switch.py finish
  ├─ 提到"快照/备份/保存"
  │   ├─ 有版本号 → snapshot.py <version>
  │   └─ 没版本号 → 回显 fallback `codex-YYYYMMDD-HHMM` → 用户确认 → snapshot.py
  ├─ 提到"恢复/回滚/restore"(注意是回到某版本,不是换号)
  │   ├─ 有版本号 → restore.py <version>
  │   └─ 没版本号 → 先 list.py 让用户选
  ├─ 提到"列出/有哪些/list"
  │   └─ list.py
  ├─ 提到"详情/show"
  │   └─ show.py <version>
  └─ 提到"删除/delete"
      └─ delete.py <version>  (软删除,30 天可恢复)
```

**辨别 restore vs account-switch**:
- restore = "回到 codex-0628 那个版本" / "回滚到上周快照" (有明确版本号 = 回滚)
- account-switch = "换号后看不到旧对话" / "B 账号没显示 A 的会话" (跟换号有关 = 走 account-switch)
- 拿不准就问用户 "你是想回到某个版本快照,还是想让换号后的旧对话重新可见?"

## HANDOFF 协议(Agent 间转交时填)

本 skill 不属于任何业务 subagent,**主 Agent 直接执行即可**,不需要委托。如果需要委托(比如恢复后顺带做收尾通知),HANDOFF 写:

```
## 用户原话
<原话粘贴>

## 已确认上下文
- 快照版本号: <version>
- 操作: snapshot / restore / list / show / delete
- 是否在 Codex.app 运行中执行:<yes/no>

## 用户指定的 skill / 工具 / MCP
codex-snapshot(本 skill)

## 交付标准
- snapshot:打印新快照路径 + 大小 + manifest 摘要
- restore:打印 pre-restore 路径 + 变动文件数 + 用户确认结果 + 恢复结果

## 禁止项
- 不准跳过用户确认
- 不准快照 auth.json
- 不准在 Codex.app 运行中静默写 sqlite
```

## 跨端调用

- **Claude Code**:`~/.claude/skills/codex-snapshot` symlink。
- **Cursor**:`~/.cursor/agent-shared/skills/codex-snapshot` symlink。
- **Codex.app**:`~/.codex/skills/codex-snapshot` symlink,Codex 内置 shell 工具可调用同样的 Python 脚本。

三端跑同一份脚本,无差异。

## 验证

跑 `bash ~/agent-bootstrap/skills/codex-snapshot/tests/smoke.sh` 走 6 步等价验证,不需要真换号。

<!-- ⟨writer:claude-code ts:2026-06-27T02:15+08:00⟩ -->
