---
description: Load a Cursor archived Agent conversation by title or keyword
argument-hint: "<title-or-keyword>"
allowed-tools: Bash(python3 ~/.claude/skills/ccc-syn-skill/scripts/cursor_transcript.py:*)
---

# Cursor Transcript

## Cursor Transcript Search

Search Cursor's local archived Agent conversations:

```!
python3 ~/.claude/skills/ccc-syn-skill/scripts/cursor_transcript.py search "$ARGUMENTS" --limit 5
```

Immediately print the search results to the user. Do not stay silent.

## Next Step

If there is exactly one strong match, load it with:

```bash
python3 ~/.claude/skills/ccc-syn-skill/scripts/cursor_transcript.py load "11111111-2222-3333-4444-555555555555" --max-chars 20000
```

Treat the loaded Markdown as historical context, then continue the user's current task. Replace the example UUID with the real UUID from search results; do not paste angle-bracket placeholders into zsh.

If there are multiple plausible matches, ask the user which UUID or title to load.
