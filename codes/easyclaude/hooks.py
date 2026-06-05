import json
import os
import subprocess
from pathlib import Path

from .config import WORKDIR


HOOK_EVENTS = ("PreToolUse", "PostToolUse", "SessionStart")
HOOK_TIMEOUT = 30
TRUST_MARKER = WORKDIR / ".claude" / ".claude_trusted"


def is_workspace_trusted(workspace: Path = None) -> bool:
    ws = workspace or WORKDIR
    return (ws / ".claude" / ".claude_trusted").exists()


class HookManager:
    """
    Load and execute workspace hooks from .hooks.json.

    Exit code contract:
    - 0: continue
    - 1: block the action
    - 2: inject stderr as additional context
    """

    def __init__(self, config_path: Path = None, sdk_mode: bool = False):
        self.hooks = {event: [] for event in HOOK_EVENTS}
        self._sdk_mode = sdk_mode
        config_path = config_path or (WORKDIR / ".hooks.json")
        self.config_path = config_path
        if not config_path.exists():
            return
        try:
            config = json.loads(config_path.read_text())
            for event in HOOK_EVENTS:
                self.hooks[event] = config.get("hooks", {}).get(event, [])
            print(f"[Hooks loaded from {config_path}]")
        except Exception as e:
            print(f"[Hook config error: {e}]")

    def _check_workspace_trust(self) -> bool:
        if self._sdk_mode:
            return True
        return is_workspace_trusted(WORKDIR)

    def describe(self) -> str:
        lines = []
        for event in HOOK_EVENTS:
            lines.append(f"{event}: {len(self.hooks.get(event, []))}")
        trust = "trusted" if self._check_workspace_trust() else "untrusted"
        return f"Hooks config: {self.config_path} ({trust})\n" + "\n".join(lines)

    def run_hooks(self, event: str, context: dict = None) -> dict:
        result = {"blocked": False, "messages": []}
        if not self._check_workspace_trust():
            return result
        for hook_def in self.hooks.get(event, []):
            matcher = hook_def.get("matcher")
            if matcher and context:
                tool_name = context.get("tool_name", "")
                if matcher != "*" and matcher != tool_name:
                    continue
            command = hook_def.get("command", "")
            if not command:
                continue

            env = dict(os.environ)
            env["HOOK_EVENT"] = event
            if context:
                env["HOOK_TOOL_NAME"] = context.get("tool_name", "")
                env["HOOK_TOOL_INPUT"] = json.dumps(
                    context.get("tool_input", {}), ensure_ascii=False
                )[:10000]
                if "tool_output" in context:
                    env["HOOK_TOOL_OUTPUT"] = str(context["tool_output"])[:10000]

            try:
                completed = subprocess.run(
                    command,
                    shell=True,
                    cwd=WORKDIR,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=HOOK_TIMEOUT,
                )
            except subprocess.TimeoutExpired:
                print(f"  [hook:{event}] Timeout ({HOOK_TIMEOUT}s)")
                continue
            except Exception as e:
                print(f"  [hook:{event}] Error: {e}")
                continue

            stdout = completed.stdout.strip()
            stderr = completed.stderr.strip()
            if completed.returncode == 0:
                if stdout:
                    print(f"  [hook:{event}] {stdout[:100]}")
                try:
                    hook_output = json.loads(stdout)
                except (json.JSONDecodeError, TypeError):
                    hook_output = {}
                if "updatedInput" in hook_output and context:
                    context["tool_input"] = hook_output["updatedInput"]
                if "additionalContext" in hook_output:
                    result["messages"].append(hook_output["additionalContext"])
                if "permissionDecision" in hook_output:
                    result["permission_override"] = hook_output["permissionDecision"]
            elif completed.returncode == 1:
                result["blocked"] = True
                result["block_reason"] = stderr or "Blocked by hook"
                print(f"  [hook:{event}] BLOCKED: {result['block_reason'][:200]}")
            elif completed.returncode == 2:
                if stderr:
                    result["messages"].append(stderr)
                    print(f"  [hook:{event}] INJECT: {stderr[:200]}")
        return result

