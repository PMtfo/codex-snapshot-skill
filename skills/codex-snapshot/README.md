# codex-snapshot

把 macOS Codex.app 的 `~/.codex/` 工作状态打成带版本号的本地快照, 跨账号、跨快照、跨设备迁移。

## 三个核心场景

1. **日常存档/版本回滚** — snapshot + restore。
2. **换 OpenAI 账号** — A 账号没额度了换 B 账号, 让 A 账号产生的全部对话在 B 账号下重新可见。 `account-switch prepare` + `account-switch finish` 双阶段。
3. **跨设备迁移** — snapshot 后拷到另一台机器, restore 上去。

## 为什么需要

Codex 切账号会发现 sidebar 里的对话都不见了。原理:
- Codex 本地数据存在 `~/.codex/`, 跟账号无强绑定 (没有 account_id 字段)
- 但 sidebar UI 拉对话时按 `model_provider` + `thread_source` 过滤, 旧账号 thread 因 metadata 不匹配被隐藏
- 加上 `state_5.sqlite` 索引是 Codex 启动时按 `sessions/*.jsonl` 第一行 metadata 回填的, 跨账号 metadata 漂移直接连累 sidebar

本 skill 把这套机制封装成 5 分钟无脑流程, 不需要你懂 sqlite 也不需要懂 jsonl。

## 安装(已自动完成)

真源在 `~/agent-bootstrap/skills/codex-snapshot/`,通过 symlink 暴露给三端:

```
~/.claude/skills/codex-snapshot            → 真源
~/.codex/skills/codex-snapshot             → 真源
~/.cursor/agent-shared/skills/codex-snapshot → 真源
```

## 用法

在 Claude Code / Cursor / Codex 任一端, 用自然语言说:

- **快照**:`快照 codex 版本号 codex-0628`(会先提醒你 ⌘Q 退出 Codex)
- **回滚**:`恢复 codex 版本号 codex-0628`
- **换号**:`codex 换号了, 帮我把对话恢复` (双阶段,中间需要重启 Codex)
- **列出**:`列出 codex 快照`
- **详情**:`show codex 快照 codex-0628`
- **删除**:`删除 codex 快照 codex-0628`

也可以直接命令行调用:

```bash
SS=~/agent-bootstrap/skills/codex-snapshot/scripts

# 日常存档/回滚
python3 $SS/snapshot.py codex-0628                 # 推荐先 ⌘Q 退出 Codex
python3 $SS/snapshot.py codex-0628 --allow-while-running   # 在线 backup
python3 $SS/list.py
python3 $SS/show.py codex-0628
python3 $SS/restore.py codex-0628
python3 $SS/delete.py codex-0628

# 换号(A → B): 双阶段
python3 $SS/account_switch.py prepare              # 默认 --provider openai
# (⌘Q 重启 Codex.app, 等 20-30 秒让 backfill 跑完)
python3 $SS/account_switch.py finish
```

## 换号 5 分钟流程详解

```
账号 A 没额度了, 要换 B:

[1] 主 Agent 调 account-switch prepare
    - 自动 pre-account-switch-<ts> 全量快照 (sessions/+agents/+config)
    - 批量改 sessions/*.jsonl 的 model_provider → 'openai'
    - 把 state_5.sqlite + cache/ + session_index.jsonl 移到 trash

[2] 你 ⌘Q 退出 Codex, 重新打开
    - Codex 自动 backfill 扫 sessions/ 重建 state_5.sqlite
    - sidebar 此时可能只显示几条对话, 这是正常的, 别慌

[3] 主 Agent 调 account-switch finish
    - UPDATE thread_source NULL → 'user' (sidebar 默认隐藏 NULL 的)
    - UPDATE 残留 model_provider → 'openai'
    - 从 pre-account-switch 快照 merge title 列 (LLM 总结的简洁标题)

[4] 你再 ⌘Q 重启 Codex, sidebar 完整显示所有旧对话
```

> **强约束**:
> - snapshot/restore/account-switch 都要求 Codex.app 已退出 (snapshot 可以 `--allow-while-running` 绕过)
> - 永不快照/恢复 `auth.json` / `state_5.sqlite*` / `cache/` / `session_index.jsonl` (Codex 启动自动重建)
> - account-switch 必须分两步, 中间用户重启 Codex, 不能一步到位

## 快照范围

**包含**(全部 `~/.codex/` 下用户内容):

- `config.toml`、`.codex-global-state.json`
- `logs_2.sqlite`、`state_5.sqlite`、`memories_1.sqlite`、`goals_1.sqlite`(用 sqlite3 backup API 在线快照,Codex.app 可继续运行)
- `session_index.jsonl`、`sessions/`、`archived_sessions/`、`history.jsonl`
- `agents/`、`memories/`(含 .git)、`shell_snapshots/`
- 其他 ~/.codex/ 下未被排除的内容

**排除**:

- `auth.json`(登录态,换号后必须用新账号重登)
- `*.sqlite-shm`、`*.sqlite-wal`(sqlite 边车,backup API 已生成纯净副本)
- 各种 `*.bak.*` 临时备份
- `plugins/`、`computer-use/`、`vendor_imports/`、`local-marketplaces/`(可重装的二进制)
- `installation_id`、`.personality_migration`、`process_manager/`(机器指纹/运行态)
- macOS `~/Library/{Caches,Logs,HTTPStorages,Application Support}/com.openai.codex/`(缓存/日志/崩溃)

## 恢复行为(强约束)

1. **先做 pre-restore 自动快照**(不可关)。
2. **diff manifest**:用 `(size, mtime_ns)` 快筛 + sha256 确证;sqlite 用"表行数+integrity_check+rowid 指纹"避免 WAL 干扰。
3. **有差异必停下**:打印前 50 个变动文件 + 总数 + 提示 `输入 yes 继续:`,严格等于 `yes` 才覆盖,其他输入(含 y/Y/YES)一律退出。
4. **memories git HEAD 变更额外二次确认**。
5. **白名单同步**:rsync 仅按 manifest 文件集恢复,不带 `--delete`,本地"快照外"的 auth.json 等保留。
6. **Codex.app 运行中默认拒绝写 sqlite**,加 `--force-while-running` 才覆盖(警告)。

## 快照仓库布局

```
~/.codex-snapshots/
├── INDEX.json                # 全局索引
├── .trash/                   # 软删除,30 天后真清
└── <version>/
    ├── manifest.json
    ├── payload.tar.gz
    └── payload.sha256
```

## 换账号实战流程

```
# 1. 当前账号 A 用着,先存档
说"快照 codex 版本号 codex-0627"

# 2. 在 Codex.app 退出账号 A、登录账号 B
# (这一步会清空 auth.json,但其他内容都在)

# 3. 如果发现内容真的不见了(罕见,但保险),恢复
说"恢复 codex 版本号 codex-0627"

# 4. 提示用新账号 B 登录 Codex 本体,搞定
```

## 测试

```bash
bash ~/agent-bootstrap/skills/codex-snapshot/tests/smoke.sh
```

走 6 步等价验证(快照 → 制造差异 → 拦截 → 覆盖 → SQLite 在线一致性 → 整目录还原),不需要真换号。
