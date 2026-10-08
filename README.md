# codex-snapshot-skill

**Codex 换号存档** —— 给 OpenAI Codex **macOS 桌面版**做本地快照、版本回滚、跨账号迁移的 skill 套件。

> 关键词：codex 换号存档

## 它解决什么问题

OpenAI Codex macOS 桌面版（`/Applications/Codex.app`）有三个本地数据问题，**官方一个工具都没给**：

| 问题 | 现象 |
| --- | --- |
| **没有导出/备份机制** | ChatGPT 网页版才有数据导出，桌面版没有。换电脑、做实验前想留一份完整环境快照，只能手动 `cp`。 |
| **换 OpenAI 账号后旧对话"消失"** | A 账号攒了几百条对话，额度用完换 B 账号登录，侧栏突然一片空白。实际上 jsonl 全在 `~/.codex/sessions/`，没丢。 |
| **没有版本回滚** | 想试新 config、装个插件玩玩，搞坏了想还原？没工具。 |

这个 skill 用一组 Python 脚本（只用标准库，零第三方依赖）封装了三个场景：

| 场景 | 命令 | 感受 |
| --- | --- | --- |
| **日常存档 / 版本回滚** | `snapshot <ver>` / `restore <ver>` | 像 `git checkout` 一样 |
| **换 OpenAI 账号** | `account-switch prepare` → 重启 Codex → `account-switch finish` | 双阶段流程，几分钟 |
| **跨设备迁移** | `snapshot` → 拷快照到新机 → `restore` | 一个 tar.gz 搞定 |

## 为什么会"换号后对话消失"

三句话讲清核心机制，读懂了就能自己改：

1. **Codex 本地数据完全跟账号解耦。**
   `~/.codex/sessions/*.jsonl` 是对话真源，`threads` 表 **没有 `account_id` 字段**。换账号物理上不丢任何东西。

2. **侧栏显示 thread 看三个字段过滤。**
   `source IN (...)` + `model_provider IN (...)` + `thread_source != NULL`。任意一个不匹配，侧栏就隐藏这条对话。
   这是 Codex 源码 `codex-rs/state/src/runtime/threads.rs` 里 `listThreads` 的 SQL WHERE 拼装逻辑。

3. **`state_5.sqlite` 是可重建索引。**
   删掉它 + `cache` + `session_index.jsonl`，Codex 启动时 `backfill_sessions_with_lease` 会扫 `~/.codex/sessions/` 重建。这是官方机制（源码 `codex-rs/cli/src/state_db_recovery.rs` 注释里写了 "rebuild it from saved data"）。

`account-switch` 就是这三句话的工程化：批量改 jsonl 让 `model_provider` 匹配新账号 → 删 sqlite 让 Codex 重建 → 收尾 UPDATE 让侧栏不再隐藏。

## 五分钟上手

```bash
# 1. 克隆到本机任意位置
git clone https://github.com/PMtfo/codex-snapshot-skill.git ~/codex-snapshot-skill

# 2. 安装（在 ~/.claude/skills/、~/.codex/skills/、~/.cursor/agent-shared/skills/ 建 symlink）
bash ~/codex-snapshot-skill/install.sh

# 3. 在 Claude Code / Cursor / Codex 任一端用自然语言触发
#    "快照 codex，版本号 codex-test"
#    "列出 codex 快照"
#    "codex 换号了，帮我恢复对话"
```

详细安装、卸载、在别人电脑复现的步骤见 [INSTALL.md](INSTALL.md)。

## 命令速查

```bash
S=~/codex-snapshot-skill/skills/codex-snapshot/scripts

python3 $S/snapshot.py codex-test        # 打快照
python3 $S/list.py                       # 列出所有快照
python3 $S/show.py codex-test            # 看快照详情
python3 $S/restore.py codex-test         # 回滚到该快照
python3 $S/delete.py codex-test          # 删除快照

python3 $S/account_switch.py prepare     # 换号第一步（自动打保险快照）
#   → ⌘Q 退出 Codex，用新账号登录并启动一次
python3 $S/account_switch.py finish      # 换号第二步（可重复跑）
```

`--provider <名字>` 可指定目标 provider（默认 `openai`）。换号期间 `finish` 会把残留的非目标 provider 一并 UPDATE 成目标值。

## 仓库结构

```
codex-snapshot-skill/
├── README.md               当前文档
├── INSTALL.md              安装 / 卸载 / 复现
├── DEBUGGING.md            踩坑完整复盘 + 关键上游 issue 链接
├── install.sh              一键安装（建 symlink + 校验依赖）
├── uninstall.sh
├── examples/
│   └── config.example.toml 第三方网关 provider 配置样例
└── skills/
    ├── codex-snapshot/     本 skill 主体
    │   ├── SKILL.md        Agent 触发词与决策树
    │   ├── README.md
    │   ├── scripts/        8 个 Python 脚本（仅标准库）
    │   │   ├── _common.py
    │   │   ├── snapshot.py
    │   │   ├── restore.py
    │   │   ├── account_switch.py   换号核心，双阶段
    │   │   ├── list.py / show.py / delete.py
    │   │   └── exclude.txt
    │   └── tests/smoke.sh  6 步等价验证，不需要真换号
    └── ccc-syn-skill/     配套：Cursor / Claude Code 对话导入 Codex
```

## 设计取舍

| 工具 | 定位 | 跟本 skill 的关系 |
| --- | --- | --- |
| [Red-noblue/Codex_Relay](https://github.com/Red-noblue/Codex_Relay) | Tauri GUI，跨设备打包 zip + 改 session_id resume | 跨设备续写比本 skill 强，但不解决换号后侧栏隐藏 |
| [ccc-syn-skill](skills/ccc-syn-skill/) | 把 Cursor / Claude Code 对话**导入** Codex | 本仓库已内置为配套 skill |
| [pangkk18/codex-history-sync](https://github.com/pangkk18/codex-history-sync) 等 | 改 jsonl + sqlite 的 `model_provider` 让旧线程伪装成新 provider | 本 skill 的 `account-switch` 是同样原理的成熟实现 |

本 skill 的差异化价值：

- **双阶段 + 自动保险快照**：换号前自动全量快照，任何步骤出问题都能秒回滚。
- **finish 可重复跑**：重启 Codex 后又用了一阵、侧栏还少几条？再跑一遍兜底。
- **强约束 exclude**：永远不快照 `state_5.sqlite` / `cache/` / `auth.json`，避免跨账号污染。
- **三端共用**：Claude Code / Cursor / Codex 任一端通过 SKILL.md 触发词调用，跑同一份脚本。

## 已知边界

- **只支持 macOS Codex.app** 桌面 GUI 那一代。Linux、Windows、ChatGPT 网页版都不适用。
- **不解决跨设备/跨账号的对话续写** —— 也就是在新机器或新账号上 `codex resume <id>` 继续聊那条对话。这个场景用 Codex_Relay 更合适。
- **跑 `account-switch` 期间不能动 Codex.app**，必须 ⌘Q 退出。
- 快照体积 = `~/.codex/` 总大小减去 `plugins` / `cache` / sqlite。一份典型快照 130–150 MB。

## 安全相关

- **永远不快照 `auth.json`**（OpenAI OAuth token）。
- **永远不快照 `*.bak.*`**（Codex 自己写的临时备份）。
- **快照文件只存本地 `~/.codex-snapshots/`**，不上传任何云端。
- 本仓库只含脚本和文档，**没有任何账号 token、cookie 或对话内容**。

## 参考

- OpenAI Codex 源码：[openai/codex](https://github.com/openai/codex)
- 关键 issue 与社区反馈：见 [DEBUGGING.md](DEBUGGING.md)
- 配套工具：[ccc-syn-skill](skills/ccc-syn-skill/)

## License

MIT
