import json
import os
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / "adapters" / "claude" / "hook.py"


def _run(action, tmp_path, **env):
    child_env = {k: v for k, v in os.environ.items() if "API_KEY" not in k}
    child_env.update({"CLAUDE_PLUGIN_DATA": str(tmp_path), "MEM0_TELEMETRY": "false", "HOME": str(tmp_path), **env})
    return subprocess.run(
        [sys.executable, str(HOOK), action, "--plugin-data-dir", str(tmp_path)],
        input="{}", capture_output=True, text=True, env=child_env, timeout=30,
    )


def test_session_start_without_key_tells_the_user(tmp_path):
    result = _run("session-start", tmp_path)
    assert "MEM0_API_KEY" in json.loads(result.stdout)["systemMessage"]


def test_session_start_with_key_stays_silent(tmp_path):
    assert _run("session-start", tmp_path, MEM0_API_KEY="k").stdout == ""
    assert _run("session-start", tmp_path, CLAUDE_PLUGIN_OPTION_API_KEY="k").stdout == ""


def test_other_hooks_never_show_the_notice(tmp_path):
    assert "systemMessage" not in _run("post-tool", tmp_path).stdout
