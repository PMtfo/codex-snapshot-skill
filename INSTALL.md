# 安装 / 卸载 / 在别的电脑复现

> 本 skill 套件设计成"克隆仓库 → 跑 install.sh → 完事"。脚本只用 Python stdlib，不需要 pip 装东西。

## 前置条件

| 项 | 要求 | 校验命令 |
|---|---|---|
| 操作系统 | macOS (Apple Silicon 或 Intel) | `uname -sm` |
| Python | 3.10+ (用了 `tomllib` 之类的可选写法, 但脚本本身用 3.8+ 也跑) | `python3 --version` |
| Codex.app | 已安装 (`/Applications/Codex.app`), 任意 26.x 版本 | `ls /Applications/Codex.app` |
| git | 任意版本 | `git --version` |
| sqlite3 (CLI) | 系统自带 | `sqlite3 --version` |

无 `gh` / `npm` / `brew` 强依赖。`zstd` 不需要（脚本用 gzip 压缩）。

## 安装

### 方式一：克隆仓库 + install.sh （推荐）

```bash
# 1. 克隆到一个常驻位置
git clone git@github.com:PMtfo/codex-snapshot-skill.git ~/codex-snapshot-skill

# 2. 跑安装脚本
bash ~/codex-snapshot-skill/install.sh

# install.sh 做的事:
#   a. 校验依赖 (Python / Codex.app / sqlite3 都得在)
#   b. 在以下 3 个目录建 symlink 指向本仓库 skills/codex-snapshot/:
#      ~/.claude/skills/codex-snapshot                  (Claude Code)
#      ~/.codex/skills/codex-snapshot                   (Codex 自己)
#      ~/.cursor/agent-shared/skills/codex-snapshot     (Cursor)
#   c. 同样对 ccc-syn-skill 建 3 端 symlink
#   d. 给所有 Python 脚本和 smoke.sh 加可执行权限
#   e. 验证 import 不报错
```

### 方式二：手动安装 (绕过 install.sh)

```bash
# 仓库克隆到自定义位置
git clone git@github.com:PMtfo/codex-snapshot-skill.git /path/to/codex-snapshot-skill

# 在你用的 Agent 工具的 skills 目录建 symlink
ln -sfn /path/to/codex-snapshot-skill/skills/codex-snapshot ~/.claude/skills/codex-snapshot
# 如果用 Codex 端:
ln -sfn /path/to/codex-snapshot-skill/skills/codex-snapshot ~/.codex/skills/codex-snapshot
# 如果用 Cursor:
mkdir -p ~/.cursor/agent-shared/skills
ln -sfn /path/to/codex-snapshot-skill/skills/codex-snapshot ~/.cursor/agent-shared/skills/codex-snapshot

# ccc-syn-skill 同理 (如果要装的话)
ln -sfn /path/to/codex-snapshot-skill/skills/ccc-syn-skill ~/.cursor/agent-shared/skills/ccc-syn-skill

# 加执行权限
chmod +x /path/to/codex-snapshot-skill/skills/codex-snapshot/scripts/*.py
chmod +x /path/to/codex-snapshot-skill/skills/codex-snapshot/tests/smoke.sh
```

### 方式三：直接拷文件（不用 symlink）

> 不推荐，丢失 "改一份三端同步" 的便利，但兼容某些 Agent 工具的 sandbox。

```bash
cp -R skills/codex-snapshot ~/.claude/skills/
cp -R skills/codex-snapshot ~/.codex/skills/
cp -R skills/codex-snapshot ~/.cursor/agent-shared/skills/
chmod +x ~/.claude/skills/codex-snapshot/scripts/*.py
```

## 验证安装

跑 6 步 smoke test（不影响你真的 ~/.codex/）：

```bash
bash ~/codex-snapshot-skill/skills/codex-snapshot/tests/smoke.sh
```

最后一行应输出 `[smoke] 全部 6 步通过 ✓`。

如果失败，看 [DEBUGGING.md](DEBUGGING.md) 里 "smoke 失败排查"。

## 在新电脑复现你的 Codex 工作环境

这是 README 里 "跨设备迁移" 场景的详细操作。

### 旧电脑：打快照

```bash
# 1. ⌘Q 退出 Codex.app (重要,避免 sqlite 锁)
osascript -e 'quit app "Codex"'

# 2. 用 skill 打全量快照
python3 ~/.claude/skills/codex-snapshot/scripts/snapshot.py codex-migration

# 输出大约:
#   [codex-snapshot] ✓ 快照完成: ~/.codex-snapshots/codex-migration
#   [codex-snapshot]   文件数 2931, 压缩后 136 MB, 源合计 355 MB

# 3. 拷快照到新电脑
scp -r ~/.codex-snapshots/codex-migration newmac:~/.codex-snapshots/
# 或者 AirDrop / iCloud / 移动硬盘均可
```

### 新电脑：恢复

```bash
# 1. 装 Codex.app + 本 skill 仓库(按上面"安装"那段)
git clone git@github.com:PMtfo/codex-snapshot-skill.git ~/codex-snapshot-skill
bash ~/codex-snapshot-skill/install.sh

# 2. 用账号 B 登录 Codex.app 至少一次, 让 Codex 创建基础 ~/.codex/ 目录和 auth.json
open /Applications/Codex.app
# (用账号登录, 随便发一条 hello 验证可用, 然后 ⌘Q 退出)

# 3. 恢复快照
python3 ~/.claude/skills/codex-snapshot/scripts/restore.py codex-migration
# 注意:restore 永远跳过 state_5.sqlite/cache/auth.json (用账号 B 的, 不覆盖)

# 4. 由于旧电脑是账号 A 的 model_provider, 跑一遍 account-switch finish 兜底
python3 ~/.claude/skills/codex-snapshot/scripts/account_switch.py finish --provider openai

# 5. 重启 Codex 验证 sidebar 完整
open /Applications/Codex.app
```

预期：你旧电脑账号 A 时期的所有对话、agents、memories 都在新电脑账号 B 下完整显示。

## 卸载

```bash
bash ~/codex-snapshot-skill/uninstall.sh
# 或手动
rm ~/.claude/skills/codex-snapshot
rm ~/.codex/skills/codex-snapshot
rm ~/.cursor/agent-shared/skills/codex-snapshot
rm ~/.claude/skills/ccc-syn-skill 2>/dev/null
# ...
```

注意：卸载**只删 symlink，不删 `~/.codex-snapshots/` 下你已经打的快照**。要彻底清空：

```bash
# 慎重! 会删除所有本地快照
rm -rf ~/.codex-snapshots
```

## 同时改多个 Agent 端的 skill

因为三端走的是 symlink → 同一份 `~/codex-snapshot-skill/skills/codex-snapshot/`，所以**改本仓库就等于三端都改了**：

```bash
cd ~/codex-snapshot-skill
vim skills/codex-snapshot/scripts/snapshot.py
# 三端立刻生效
```

如果你想把改动推回 git：

```bash
cd ~/codex-snapshot-skill
git add . && git commit -m "tweak: ..."
git push
```

## 常见问题

### Q: 我的 `~/.codex/` 没什么东西，跑 snapshot 报错吗？

不会报错，但快照可能为空。skill 期望 `~/.codex/` 至少存在（Codex.app 启动登录一次就有），不需要里面有对话。

### Q: 我用别的 provider（不是 OpenAI 或自定义网关），跑 account-switch 怎么传？

```bash
python3 .../account_switch.py prepare --provider my_custom_provider
python3 .../account_switch.py finish  --provider my_custom_provider
```

`account-switch` 把所有 jsonl + sqlite 的 `model_provider` 字段统一改成你传的那个值。

### Q: 安装到 `~/codex-snapshot-skill` 之外的位置可以吗？

可以，仓库可以放任何位置。`install.sh` 会用脚本所在目录的绝对路径建 symlink。

### Q: 这套 skill 在 Linux / Windows 能跑吗？

**不能**。Codex.app 只发布了 macOS 版本。Python 脚本本身跨平台，但 `~/.codex/` 路径、`/Applications/Codex.app/Contents/Info.plist`、`pgrep -f Codex.app` 这些都是 macOS-specific。
