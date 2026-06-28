# CLAUDE.md 片段（Cursor → Claude）

以下内容应存在于 `~/.claude/CLAUDE.md`（或通过 config center 同步的 `CLAUDE.md`）中，供 Claude Code 自然语言触发 B 方向加载。

```markdown
## Cursor Transcript Handoff

当用户要求“读 Cursor 归档对话 / 读取 Cursor transcript / 加载某个 Cursor 对话标题 / 接着 Cursor 那个任务继续”时，先用本地只读脚本定位上下文，不要先自由发挥或猜测文件位置：

```bash
python3 /Users/you/.claude/skills/ccc-syn-skill/scripts/cursor_transcript.py search "<标题或关键词>" --limit 5
```

如果只有一个明显命中，继续加载：

```bash
python3 /Users/you/.claude/skills/ccc-syn-skill/scripts/cursor_transcript.py load "11111111-2222-3333-4444-555555555555" --max-chars 20000
```

把 load 输出当作历史上下文，再继续执行用户当前任务；如果有多个候选，先让用户选择，不要加载错误对话。也可以使用 `/cursor-transcript <标题或关键词>` 快速完成搜索。加载时应把示例 UUID 替换成搜索结果里的真实 UUID，不要把 `<uuid>` 这种尖括号占位符原样粘贴到 zsh。

加载成功后先用一句话确认已加载的标题和 uuid，再继续后续任务；不要在未确认加载结果的情况下长时间沉默。
```
