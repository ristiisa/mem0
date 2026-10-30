#!/usr/bin/env python3
"""Claude Code hooks for Mem0."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_here = Path(__file__).resolve()
_bundled_core = _here.parents[2] / "core"
_core_dir = _bundled_core if (_bundled_core / "memory_core.py").is_file() else _bundled_core / "python"
sys.path.insert(0, str(_core_dir))
sys.path.insert(0, str(_here.parent))

import hook_runner  # noqa: E402
import telemetry  # noqa: E402
from memory_core import (  # noqa: E402
    api_key,
    clear_stale_api_key_cache,
    configure_harness,
    record_subagent_start,
    record_subagent_stop,
    record_tool,
)
from transcript import record_stop  # noqa: E402

configure_harness("claude-code", data_dir_name="claude-code-plugin", source_tag="claude_code_plugin")
telemetry.init(harness="claude-code", source_tag="CLAUDE_CODE_PLUGIN")


MISSING_KEY_NOTICE = (
    "mem0: no API key, so memory is off. Add it under /plugin > mem0 > Configure options, "
    "or as MEM0_API_KEY in the env block of ~/.claude/settings.json, then restart."
)


def _missing_key_notice() -> None:
    """Tell the user, not the model, that the plugin is installed but has no key."""
    if sys.argv[1:2] != ["session-start"]:
        return
    try:
        clear_stale_api_key_cache()
        if not api_key():
            print(json.dumps({"systemMessage": MISSING_KEY_NOTICE}))
    except Exception:
        pass


def _sidekick_start(store, hook_input):
    context = record_subagent_start(store, {"agent_type": "mem0:sidekick", **hook_input})
    if context:
        return {
            "hookSpecificOutput": {
                "hookEventName": "SubagentStart",
                "additionalContext": context,
            },
        }


def _sidekick_stop(store, hook_input):
    record_subagent_stop(store, {"agent_type": "mem0:sidekick", **hook_input})


if __name__ == "__main__":
    _missing_key_notice()
    hook_runner.entry_point(
        record_stop_fn=record_stop,
        extra_actions={
            "post-tool-failure": lambda s, h: record_tool(s, h, failed=True),
            "sidekick-start": _sidekick_start,
            "sidekick-stop": _sidekick_stop,
        },
        data_dir_env="MEM0_CODE_DATA_DIR",
        automatic_flush_reasons={"session-end", "pre-compact"},
    )
