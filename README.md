# codex-snapshot-skill

> 给 OpenAI Codex macOS 桌面应用做本地快照、版本回滚、跨 OpenAI 账号迁移的 skill 套件。

## 这套 skill 解决什么问题

OpenAI 的 Codex macOS 桌面应用（`/Applications/Codex.app`，v26.x 那一代）有 3 个让人头疼的本地数据问题，**官方没有给任何工具**：

1. **没有官方导出/备份机制** —— ChatGPT 网页版才有数据导出，桌面 Codex 完全没这个入口。换电脑、换账号、做实验前想留一份完整环境快照只能手动备份。
2. **换 OpenAI 账号后旧对话"消失"** —— A 账号攒了几百条对话和项目，A 账号额度用完换 B 账号登录，sidebar 突然一片空白。实际上 jsonl 文件全在 `~/.codex/sessions/`，但 Codex 启动时按 `model_provider` / `thread_source` 过滤 sidebar，旧 metadata 跟新账号配置不匹配的全被隐藏。
3. **没有版本回滚** —— 想试新 config / 装个 plugin 玩玩，搞坏了想还原？没工具。

这个 skill 套件用一组 Python 脚本（不依赖任何 Python 第三方包，只用 stdlib）封装了以下 3 个核心场景：

| 场景 | 命令 | 用户感受 |
|---|---|---|
| **日常存档/版本回滚** | `snapshot <ver>` / `restore <ver>` | 像 git checkout 一样傻瓜 |
| **换 OpenAI 账号** | `account-switch prepare` → 重启 Codex → `account-switch finish` | 5 分钟无脑双阶段流程 |
| **跨设备迁移** | `snapshot` → 拷快照到新电脑 → `restore` | 一份 tar.gz 搞定 |

## 为什么这么搞而不是直接用 ccc-syn / Codex_Relay

| 工具 | 定位 | 跟本 skill 关系 |
|---|---|---|
| [Red-noblue/Codex_Relay](https://github.com/Red-noblue/Codex_Relay) | Tauri GUI 应用，跨设备打包 zip + 改 session_id resume | 跨设备场景比本 skill 强，但不解决换号 sidebar 隐藏问题 |
| ccc-syn-skill | 把 Cursor / Claude Code 对话**导入** Codex | 本 skill 仓库 **已包含** ccc-syn-skill 作配套 |
| [pangkk18/codex-history-sync](https://github.com/pangkk18/codex-history-sync) 等 | 改 jsonl + sqlite 的 `model_provider` 字段让旧线程伪装成新 provider | 本 skill 的 `account-switch` 子命令是同样原理的成熟实现 |

本 skill 的差异化价值：

- **双阶段 + 自动保险快照**：换号操作前自动 pre-account-switch 全量快照，任何步骤出问题都能秒回滚
- **可重复 finish**：用户重启 Codex 后又用了一阵，sidebar 还少几条？再跑一遍 finish 兜底
- **强约束 exclude**：永远不快照 `state_5.sqlite` / `cache/` / `auth.json`，避免跨账号污染（这是踩了一晚上的坑得出的关键设计）
- **三端共用**：Claude Code / Cursor / Codex 任一端通过 SKILL.md 触发词调用，都跑同一份 Python 脚本

## 五分钟上手

```bash
# 1. 克隆仓库到本机任意位置
git clone git@github.com:PMtfo/codex-snapshot-skill.git ~/codex-snapshot-skill

# 2. 跑安装脚本(在 ~/.claude/skills/, ~/.codex/skills/, ~/.cursor/agent-shared/skills/ 建 symlink)
bash ~/codex-snapshot-skill/install.sh

# 3. 在 Claude Code / Cursor / Codex 任一端用自然语言触发
#    "快照 codex, 版本号 codex-test"
#    "列出 codex 快照"
#    "codex 换号了, 帮我恢复对话"
```

详细安装步骤见 [INSTALL.md](INSTALL.md)。

## 仓库结构

```
codex-snapshot-skill/
├── README.md               ← 当前文档
├── INSTALL.md              ← 详细安装/卸载/在别人电脑复现
├── DEBUGGING.md            ← 一晚上踩坑完整复盘 + 关键 GitHub issue 链接
├── install.sh              ← 一键安装(建 symlink + 校验依赖)
├── uninstall.sh            ← 卸载
├── examples/
│   └── config.example.toml ← 公司内部 Model 平台 provider 配置样例
└── skills/
    ├── codex-snapshot/     ← 本 skill 主体
    │   ├── SKILL.md        ← Agent 触发词与决策树
    │   ├── README.md       ← skill 内部用户文档
    │   ├── scripts/        ← 8 个 Python 脚本(只用 stdlib)
    │   │   ├── _common.py
    │   │   ├── snapshot.py
    │   │   ├── restore.py
    │   │   ├── account_switch.py  ← 换号核心,双阶段
    │   │   ├── list.py
    │   │   ├── show.py
    │   │   ├── delete.py
    │   │   └── exclude.txt
    │   └── tests/
    │       └── smoke.sh    ← 6 步等价验证,不需真换号
    └── ccc-syn-skill/      ← 配套: Cursor/Claude Code 对话导入 Codex
        ├── SKILL.md
        ├── README.md
        ├── scripts/
        ├── codex-import/
        ├── integrations/
        ├── requirements.txt
        └── tests/
```

## 核心机制 (3 句话讲清)

读懂这 3 句话，你就能自己改这个 skill 适配新场景：

1. **Codex 本地数据完全跟账号解耦**：`~/.codex/sessions/*.jsonl` 是对话真源，`threads` 表 schema **没有 `account_id` 字段**。换账号物理上不丢任何东西。
2. **sidebar 显示 thread 看 3 个字段过滤**：`source IN (...)` + `model_provider IN (...)` + `thread_source != NULL`。任意一个不匹配，sidebar 隐藏。这是 OpenAI 源码 `codex-rs/state/src/runtime/threads.rs` 里 listThreads SQL 的 WHERE 拼装。
3. **`state_5.sqlite` 是可重建索引**：删掉它 + cache + session_index.jsonl，Codex 启动时 `backfill_sessions_with_lease` 会扫 `~/.codex/sessions/` 重建。**这是官方机制**（源码 `codex-rs/cli/src/state_db_recovery.rs` 注释明确写了 "rebuild it from saved data"）。

`account-switch` 就是这 3 句话的工程化：批量改 jsonl 让 model_provider 匹配 → 删 sqlite 让 Codex 重建 → 收尾 UPDATE 让 sidebar 不隐藏。

## 已知边界

- **只支持 macOS Codex.app**（`/Applications/Codex.app`，桌面 GUI 那一代，v26.x+）。Linux / Windows / ChatGPT 网页版均不适用。
- **不解决跨设备/跨账号的对话续写**（你想在新机器/新账号上 `codex resume <id>` 继续聊那条对话）。Codex_Relay 更合适。
- **跑 account-switch 期间不能动 Codex.app**，必须 ⌘Q 退出。
- **快照体积 = `~/.codex/` 总大小减去 plugins / cache / sqlite**。一份典型快照 130-150 MB（450 条会话 + agents + memories）。

## 安全相关

- **永远不快照 `auth.json`**（OpenAI OAuth token，敏感）。
- **永远不快照 `*.bak.*` 系列**（Codex 自己写的临时备份，不进版本控制）。
- **快照文件存本地 `~/.codex-snapshots/`，不上传任何云端**。
- 私有仓库本身只含脚本和文档，**没有任何账号 token / cookie**。

## 致谢与参考

- OpenAI Codex 源码：[openai/codex](https://github.com/openai/codex)
- 关键 issue 与社区反馈：见 [DEBUGGING.md](DEBUGGING.md)
- 配套工具：[ccc-syn-skill](skills/ccc-syn-skill/) 把 Cursor / Claude Code 对话导入 Codex

## License

私有仓库，自用。

## 仓库结构

当前受版本控制的文件共 37 个。仓库同时分发两套 Skill：`codex-snapshot`（本仓库主体）与内嵌的 `ccc-syn-skill`（跨端对话转录，独立仓库的同步副本）。

```
codex-snapshot-skill/
├── install.sh                     校验依赖 + 在三端 skill 目录建 symlink
├── uninstall.sh                   移除 symlink
├── INSTALL.md                     安装 / 卸载 / 换机复现
├── DEBUGGING.md                   排障记录
├── README.md
├── examples/config.example.toml   配置样例
└── skills/
    ├── codex-snapshot/       11   本仓库主 Skill
    │   ├── SKILL.md               触发条件与操作流程
    │   ├── README.md
    │   ├── scripts/          8    snapshot / list / show / restore / delete /
    │   │                          account_switch / _common.py / exclude.txt
    │   └── tests/smoke.sh
    └── ccc-syn-skill/        19   跨端转录 Skill 同步副本（见 ccc-syn-skill 仓库）
```

## 安装

前置条件全部为 macOS 自带或已有组件，脚本只用 Python 标准库，无需 pip：

| 项 | 要求 | 校验命令 |
|---|---|---|
| 操作系统 | macOS（Apple Silicon 或 Intel） | `uname -sm` |
| Python | 3.10+（脚本本身 3.8+ 可跑） | `python3 --version` |
| Codex.app | 已安装于 `/Applications/Codex.app`，任意 26.x | `ls /Applications/Codex.app` |
| git | 任意版本 | `git --version` |
| sqlite3 CLI | 系统自带 | `sqlite3 --version` |

无 `gh` / `npm` / `brew` 强依赖，不需要 `zstd`（脚本用 gzip 压缩）。

```bash
git clone git@github.com:PMtfo/codex-snapshot-skill.git ~/codex-snapshot-skill
bash ~/codex-snapshot-skill/install.sh
```

`install.sh` 会校验依赖，并在 Claude Code / Codex / Cursor 三处 skill 目录建立指向本仓库 `skills/codex-snapshot/` 的 symlink，因此三端共用同一份实现，更新仓库即三端同步。
