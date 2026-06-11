import os
import subprocess
from pathlib import Path

from .config import WORKDIR

CONCURRENCY_SAFE = {"read_file"}
CONCURRENCY_UNSAFE = {"write_file", "edit_file"}


def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(item in command for item in dangerous):
        return "Error: Dangerous command blocked"
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=os.getcwd(),
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"
    output = (result.stdout + result.stderr).strip()
    return output[:50000] if output else "(no output)"


def run_read(path: str, limit: int = None) -> str:
    try:
        text = safe_path(path).read_text()
        lines = text.splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        fp = safe_path(path)
        content = fp.read_text()
        if old_text not in content:
            return f"Error: Text not found in {path}"
        fp.write_text(content.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


def run_compact() -> str:
    return "Compacting conversation..."


TOOLS = [
    {
        "name": "bash",
        "description": "Run a shell command.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "Read file contents.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": "Replace exact text in a file once.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
    {
        "name": "task_create",
        "description": "Create a persistent task graph node.",
        "input_schema": {
            "type": "object",
            "properties": {
                "subject": {"type": "string"},
                "description": {"type": "string"},
                "blockedBy": {"type": "array", "items": {"type": "integer"}},
                "owner": {"type": "string"},
            },
            "required": ["subject"],
        },
    },
    {
        "name": "task_update",
        "description": "Update a persistent task node status, owner, or dependencies.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "completed", "deleted"],
                },
                "owner": {"type": "string"},
                "addBlockedBy": {"type": "array", "items": {"type": "integer"}},
                "addBlocks": {"type": "array", "items": {"type": "integer"}},
                "removeBlockedBy": {"type": "array", "items": {"type": "integer"}},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "task_list",
        "description": "List persistent task graph nodes and ready tasks.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "task_get",
        "description": "Get full details of a persistent task by ID.",
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "integer"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "background_run",
        "description": "Run a long-running shell command in the background and return a task id immediately.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
    {
        "name": "check_background",
        "description": "Check one background task by task_id, or list all background tasks when task_id is omitted.",
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
        },
    },
    {
        "name": "cron_create",
        "description": "Schedule a recurring or one-shot future prompt using a 5-field cron expression.",
        "input_schema": {
            "type": "object",
            "properties": {
                "cron": {
                    "type": "string",
                    "description": "5-field cron expression: minute hour day-of-month month day-of-week",
                },
                "prompt": {
                    "type": "string",
                    "description": "Prompt to inject into the conversation when the schedule fires.",
                },
                "recurring": {
                    "type": "boolean",
                    "description": "true repeats until deleted, false fires once then deletes itself.",
                },
                "durable": {
                    "type": "boolean",
                    "description": "true persists to .claude/scheduled_tasks.json, false is session-only.",
                },
            },
            "required": ["cron", "prompt"],
        },
    },
    {
        "name": "cron_delete",
        "description": "Delete a scheduled task by id.",
        "input_schema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    },
    {
        "name": "cron_list",
        "description": "List scheduled tasks.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "task",
        "description": "Spawn a subagent with fresh context. It shares the filesystem but not conversation history.",
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "description": {"type": "string", "description": "Short description of the task"},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "load_skill",
        "description": "Load the full body of a named skill into the current context.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "compact",
        "description": "Summarize earlier conversation so work can continue in a smaller context.",
        "input_schema": {
            "type": "object",
            "properties": {
                "focus": {"type": "string"},
            },
        },
    },
    {
        "name": "save_memory", 
        "description": "Save a persistent memory that survives across sessions.",
        "input_schema": {
                "type": "object", 
                "properties":{
                    "name": {"type": "string", "description": "Short identifier (e.g. prefer_tabs, db_schema)"},
                    "description": {"type": "string", "description": "One-line summary of what this memory captures"},
                    "type": {"type": "string", "enum": ["user", "feedback", "project", "reference"],
                            "description": "user=preferences, feedback=corrections, project=non-obvious project conventions or decision reasons, reference=external resource pointers"},
                    "content": {"type": "string", "description": "Full memory content (multi-line OK)"},
                }, 
                "required": ["name", "description", "type", "content"]
        }
    },

]


CHILD_TOOLS = [
    tool for tool in TOOLS
    if tool["name"] in {"bash", "read_file", "write_file", "edit_file"}
]


def build_tool_handlers(
    todo,
    skill_registry,
    memory_manager,
    task_manager,
    background_manager=None,
    scheduler=None,
):
    return {
        "bash": lambda **kw: run_bash(kw["command"]),
        "read_file": lambda **kw: run_read(kw["path"], kw.get("limit")),
        "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
        "edit_file": lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
        "load_skill": lambda **kw: skill_registry.load_full_text(kw["name"]),
        "compact": lambda **kw: run_compact(),
        "save_memory": lambda **kw: memory_manager.save_memory(
            kw["name"], kw["description"], kw["type"], kw["content"]
        ),
        "task_create": lambda **kw: task_manager.create(
            kw["subject"], kw.get("description", ""), kw.get("blockedBy"), kw.get("owner", "")
        ),
        "task_update": lambda **kw: task_manager.update(
            kw["task_id"],
            kw.get("status"),
            kw.get("owner"),
            kw.get("addBlockedBy"),
            kw.get("addBlocks"),
            kw.get("removeBlockedBy"),
        ),
        "task_list": lambda **kw: task_manager.list_all(),
        "task_get": lambda **kw: task_manager.get(kw["task_id"]),
        "background_run": lambda **kw: (
            background_manager.run(kw["command"])
            if background_manager
            else "Error: background manager is not configured"
        ),
        "check_background": lambda **kw: (
            background_manager.check(kw.get("task_id"))
            if background_manager
            else "Error: background manager is not configured"
        ),
        "cron_create": lambda **kw: (
            scheduler.create(
                kw["cron"],
                kw["prompt"],
                kw.get("recurring", True),
                kw.get("durable", False),
            )
            if scheduler
            else "Error: scheduler is not configured"
        ),
        "cron_delete": lambda **kw: (
            scheduler.delete(kw["id"])
            if scheduler
            else "Error: scheduler is not configured"
        ),
        "cron_list": lambda **kw: (
            scheduler.list_tasks()
            if scheduler
            else "Error: scheduler is not configured"
        ),
    }
