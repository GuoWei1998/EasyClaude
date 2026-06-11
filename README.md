# EasyClaude

EasyClaude is a teaching project for building a small coding agent step by step.

## Project Layout

```text
codes/
  main.py                 # CLI entry point
  easyclaude/             # Agent implementation modules
    agent.py              # Agent loop, subagents, tool execution
    tools.py              # Tool schemas and tool implementations
    todo.py               # Todo/session planning
    skills.py             # Skill registry and load_skill support
    compact.py            # Context compaction and large-output persistence
    recovery.py           # Error recovery, retry, continuation
    permissions.py        # Permission modes, rules, bash validation
    hooks.py              # Hook loading and execution
    memory.py             # Persistent memory across sessions
    task_graph.py         # Persistent dependency task graph
    background.py         # Long-running background tasks and notifications
    system_prompt.py      # Structured system prompt assembly
    messages.py           # Message normalization helpers
    config.py             # Environment, client, workspace paths
skills/                   # Project-level skill documents
examples/                 # Teaching scaffolds and reference experiments
.hooks.json               # Workspace hook configuration
.claude/.claude_trusted   # Trust marker that enables hooks
```

## Run

```bash
python3 codes/main.py
```

Useful REPL commands:

```text
/mode default
/mode plan
/mode auto
/rules
/hooks
/memory
/tasks
/background
q
```

## Notes

Runtime artifacts are written to `.task_outputs/` and `.transcripts/`.
Persistent local memories are written to `.memory/`.
Persistent task graph records are written to `.tasks/`.
Background task records and logs are written to `.runtime-tasks/`.
Local secrets should stay in `codes/.env`.
