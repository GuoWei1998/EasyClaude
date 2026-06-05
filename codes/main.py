#!/usr/bin/env python3
"""EasyClaude entry point.

The implementation is split into small modules under easyclaude/:
- config.py: environment, client, workspace constants
- tools.py: tool schemas and concrete tool implementations
- agent.py: agent loop, subagents, tool dispatch
- todo.py: session planning
- skills.py: on-demand skill loading
- compact.py: context compaction
- permissions.py: permission modes and rules
- hooks.py: workspace hook system
"""
try:
    import readline

    readline.parse_and_bind("set bind-tty-special-chars off")
    readline.parse_and_bind("set input-meta on")
    readline.parse_and_bind("set output-meta on")
    readline.parse_and_bind("set convert-meta off")
    readline.parse_and_bind("set enable-meta-keybindings on")
except ImportError:
    pass

from easyclaude.agent import AgentRuntime, LoopState
from easyclaude.compact import CompactState
from easyclaude.config import SKILLS_DIR, WORKDIR
from easyclaude.hooks import HookManager
from easyclaude.messages import extract_text
from easyclaude.permissions import MODES, PermissionManager
from easyclaude.skills import SkillRegistry
from easyclaude.todo import TodoManager


def build_system_prompt(skill_registry: SkillRegistry) -> str:
    return f"""You are a coding agent at {WORKDIR}.
Use the todo tool for multi-step work.
Keep exactly one step in_progress when a task has multiple steps.
Refresh the plan as work advances. Prefer tools over prose.
Use the task tool to delegate exploration or subtasks.
Use load_skill when a task needs specialized instructions before you act.
Skills available:
{skill_registry.describe_available()}
Keep working step by step, and use compact if the conversation gets too long.
"""


def choose_permission_mode() -> PermissionManager:
    print("Permission modes: default, plan, auto")
    mode_input = input("Mode (default): ").strip().lower() or "default"
    if mode_input not in MODES:
        mode_input = "default"
    print(f"[Permission mode: {mode_input}]")
    return PermissionManager(mode=mode_input)


def main() -> None:
    skill_registry = SkillRegistry(SKILLS_DIR)
    todo = TodoManager()
    perms = choose_permission_mode()
    hooks = HookManager()
    hooks.run_hooks("SessionStart", {"tool_name": "", "tool_input": {}})
    runtime = AgentRuntime(
        system=build_system_prompt(skill_registry),
        todo=todo,
        skill_registry=skill_registry,
        perms=perms,
        hooks=hooks,
    )

    history = []
    compact_state = CompactState()
    while True:
        try:
            query = input("\033[36ms01 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break

        if query.startswith("/mode"):
            parts = query.split()
            if len(parts) == 2 and parts[1] in MODES:
                perms.mode = parts[1]
                print(f"[Switched to {parts[1]} mode]")
            else:
                print(f"Usage: /mode <{'|'.join(MODES)}>")
            continue
        if query.strip() == "/rules":
            for i, rule in enumerate(perms.rules):
                print(f"  {i}: {rule}")
            continue
        if query.strip() == "/hooks":
            print(hooks.describe())
            continue

        history.append({"role": "user", "content": query})
        state = LoopState(messages=history)
        runtime.agent_loop(state, compact_state)
        final_text = extract_text(history[-1]["content"])
        if final_text:
            print(final_text)
        print()


if __name__ == "__main__":
    main()
