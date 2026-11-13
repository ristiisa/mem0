---
name: preamble
description: List, add, or remove the user's Mem0 preamble, the standing instructions loaded into every Claude Code session in every repository. Use when the user asks to add something to, change, or show their preamble or "standing instructions" in Mem0.
disable-model-invocation: false
---

# Preamble

The preamble is a set of standing instructions stored in Mem0 under the user's
ID with `app_id` `preamble`. The session-start hook loads every entry into each
new session, on every machine with this plugin, regardless of repository or
prompt. Entries are stored verbatim, without LLM extraction.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/core/memory_cli.py" --harness "claude-code" --plugin-data-dir "${CLAUDE_PLUGIN_DATA}" preamble list
python3 "${CLAUDE_PLUGIN_ROOT}/core/memory_cli.py" --harness "claude-code" --plugin-data-dir "${CLAUDE_PLUGIN_DATA}" preamble add "<entry text>"
python3 "${CLAUDE_PLUGIN_ROOT}/core/memory_cli.py" --harness "claude-code" --plugin-data-dir "${CLAUDE_PLUGIN_DATA}" preamble remove <id>
```

- Keep each entry to one instruction or one short group, worded as an
  instruction to Claude. Everything here costs context in every session.
- To change an entry, add the new wording, then remove the old id.
- Before removing, run `list` and confirm the entry with the user.
- Changes take effect in sessions started afterwards.
