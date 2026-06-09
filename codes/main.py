#!/usr/bin/env python3
"""EasyClaude entry point.

The implementation is split into small modules under easyclaude/:
- config.py: environment, client, workspace constants
- tools.py: tool schemas and concrete tool implementations
- agent.py: agent loop, subagents, tool dispatch
- todo.py: session planning
- skills.py: on-demand skill loading
- compact.py: context compaction
- memory.py: persistent memory
- task_graph.py: persistent dependency task graph
- permissions.py: permission modes and rules
- system_prompt.py: structured prompt assembly
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
from easyclaude.config import SKILLS_DIR
from easyclaude.hooks import HookManager
from easyclaude.memory import MemoryManager
from easyclaude.messages import extract_text
from easyclaude.permissions import MODES, PermissionManager
from easyclaude.skills import SkillRegistry
from easyclaude.system_prompt import DYNAMIC_BOUNDARY, SystemPromptBuilder
from easyclaude.task_graph import TaskManager
from easyclaude.todo import TodoManager
from easyclaude.tools import TOOLS


def choose_permission_mode() -> PermissionManager:
    print("Permission modes: default, plan, auto")
    mode_input = input("Mode (default): ").strip().lower() or "default"
    if mode_input not in MODES:
        mode_input = "default"
    print(f"[Permission mode: {mode_input}]")
    return PermissionManager(mode=mode_input)

def main() -> None:
    skill_registry = SkillRegistry(SKILLS_DIR)
    memory_manager = MemoryManager()
    memory_manager.load_all()
    task_manager = TaskManager()
    todo = TodoManager()
    perms = choose_permission_mode()
    hooks = HookManager()
    hooks.run_hooks("SessionStart", {"tool_name": "", "tool_input": {}})
    prompt_builder = SystemPromptBuilder(
        tools=TOOLS,
        skill_registry=skill_registry,
        memory_manager=memory_manager,
    )
    system_prompt = prompt_builder.build()
    print(f"[System prompt assembled: {len(system_prompt)} chars, {len(prompt_builder.build_sections())} sections]")
    runtime = AgentRuntime(
        system=system_prompt,
        todo=todo,
        skill_registry=skill_registry,
        memory_manager=memory_manager,
        task_manager=task_manager,
        perms=perms,
        hooks=hooks,
    )
    runtime.prompt_builder = prompt_builder

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
        if query.strip() == "/memory":
            print(f"Memory dir: {memory_manager.memory_dir}")
            print(f"Loaded memories: {len(memory_manager.memories)}")
            for name, memory in memory_manager.memories.items():
                print(f"  - {name}: {memory['description']} [{memory['type']}]")
            continue
        if query.strip() == "/tasks":
            print(task_manager.list_all())
            continue
        if query.strip() == "/prompt":
            print("--- System Prompt ---")
            print(prompt_builder.build())
            print("--- End ---")
            continue
        if query.strip() == "/sections":
            prompt = prompt_builder.build()
            for line in prompt.splitlines():
                if line.startswith("# ") or line == DYNAMIC_BOUNDARY:
                    print(f"  {line}")
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
