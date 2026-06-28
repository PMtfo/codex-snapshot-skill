#!/usr/bin/env bash
# install.sh - 一键安装 codex-snapshot-skill 到三端 Agent skill 目录
# 用 symlink, 改一份三端同步

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILLS_DIR="$SCRIPT_DIR/skills"

info() { echo "[install] $*"; }
warn() { echo "[install][WARN] $*" >&2; }
err()  { echo "[install][ERR] $*"  >&2; }

# ===== 1. 校验依赖 =====
info "校验依赖..."

if [[ "$(uname)" != "Darwin" ]]; then
    err "只支持 macOS, 当前是 $(uname)"
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    err "找不到 python3, 请先装"
    exit 1
fi

PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
info "  Python: $PY_VER (要求 >= 3.8)"

if [[ ! -d /Applications/Codex.app ]]; then
    warn "  Codex.app 没装在 /Applications/Codex.app, skill 仍可装, 但跑起来会报错"
else
    APP_VER=$(/usr/libexec/PlistBuddy -c "Print CFBundleShortVersionString" /Applications/Codex.app/Contents/Info.plist 2>/dev/null || echo "unknown")
    info "  Codex.app: $APP_VER"
fi

if ! command -v sqlite3 >/dev/null 2>&1; then
    err "找不到 sqlite3 CLI"
    exit 1
fi

if ! command -v git >/dev/null 2>&1; then
    warn "  git 没装, 但本 skill 不强依赖, 可忽略"
fi

# ===== 2. 给所有脚本加可执行权限 =====
info "给所有脚本加可执行权限..."
find "$SKILLS_DIR/codex-snapshot/scripts" -type f -name '*.py' -exec chmod +x {} \;
chmod +x "$SKILLS_DIR/codex-snapshot/tests/smoke.sh" 2>/dev/null || true
if [[ -d "$SKILLS_DIR/ccc-syn-skill/scripts" ]]; then
    find "$SKILLS_DIR/ccc-syn-skill/scripts" -type f -name '*.py' -exec chmod +x {} \;
fi
if [[ -d "$SKILLS_DIR/ccc-syn-skill/codex-import" ]]; then
    find "$SKILLS_DIR/ccc-syn-skill/codex-import" -type f -name '*.py' -exec chmod +x {} \;
fi

# ===== 3. 在三端建 symlink =====
info "建立三端 symlink..."

declare -a TARGETS=(
    "$HOME/.claude/skills"
    "$HOME/.codex/skills"
    "$HOME/.cursor/agent-shared/skills"
)

declare -a SKILLS_TO_LINK=(
    "codex-snapshot"
    "ccc-syn-skill"
)

for target_dir in "${TARGETS[@]}"; do
    if [[ ! -d "$target_dir" ]]; then
        info "  建目录: $target_dir"
        mkdir -p "$target_dir"
    fi
    for skill in "${SKILLS_TO_LINK[@]}"; do
        src="$SKILLS_DIR/$skill"
        dst="$target_dir/$skill"
        if [[ ! -d "$src" ]]; then
            warn "  source 不存在, 跳过: $src"
            continue
        fi
        # 若 dst 已存在且不是符号链接 -> 警告并备份
        if [[ -e "$dst" && ! -L "$dst" ]]; then
            bak="${dst}.bak-$(date +%Y%m%d-%H%M%S)"
            warn "  $dst 已存在(且不是 symlink), 备份为 $bak"
            mv "$dst" "$bak"
        fi
        ln -sfn "$src" "$dst"
        info "  ✓ $dst -> $src"
    done
done

# ===== 4. import 烟测 =====
info "Python import 烟测..."
python3 - <<PY
import sys
sys.path.insert(0, "$SKILLS_DIR/codex-snapshot/scripts")
import _common, snapshot, restore, list as _list, show, delete, account_switch
print("  ✓ codex-snapshot 所有模块 import OK")
PY

# ===== 5. 提示 =====
info ""
info "=================================================="
info "✓ 安装完成"
info "=================================================="
info ""
info "试一下:"
info "  python3 $SKILLS_DIR/codex-snapshot/tests/smoke.sh  # 6 步等价验证"
info "  python3 $SKILLS_DIR/codex-snapshot/scripts/list.py # 列当前快照"
info ""
info "或者在你的 Agent 里(Claude Code / Cursor / Codex)用自然语言说:"
info "  快照 codex, 版本号 codex-init"
info "  列出 codex 快照"
info "  codex 换号了, 帮我恢复对话"
info ""
info "详细用法: $SCRIPT_DIR/README.md"
info "踩坑复盘: $SCRIPT_DIR/DEBUGGING.md"
