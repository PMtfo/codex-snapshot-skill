#!/usr/bin/env bash
# uninstall.sh - 卸载 codex-snapshot-skill 的所有 symlink
# 注意: 不删 ~/.codex-snapshots/ 下你已经打的快照
# 注意: 不删本仓库本身

set -euo pipefail

info() { echo "[uninstall] $*"; }

declare -a TARGETS=(
    "$HOME/.claude/skills"
    "$HOME/.codex/skills"
    "$HOME/.cursor/agent-shared/skills"
)
declare -a SKILLS=("codex-snapshot" "ccc-syn-skill")

for target_dir in "${TARGETS[@]}"; do
    for skill in "${SKILLS[@]}"; do
        path="$target_dir/$skill"
        if [[ -L "$path" ]]; then
            target=$(readlink "$path")
            rm "$path"
            info "已删 symlink: $path -> $target"
        elif [[ -e "$path" ]]; then
            info "跳过(不是 symlink, 不动它): $path"
        fi
    done
done

info ""
info "=================================================="
info "✓ symlink 已全部清理"
info "=================================================="
info ""
info "未删除:"
info "  - 仓库本身 ($(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd))"
info "  - 本地快照仓库 ~/.codex-snapshots/  (体量可能大, 慎重)"
info ""
info "如要彻底清空(慎重!):"
info "  rm -rf ~/.codex-snapshots"
