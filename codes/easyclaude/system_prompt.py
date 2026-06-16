import datetime
import os
from pathlib import Path

from .config import MODEL, WORKDIR
from .memory import MEMORY_GUIDANCE


DYNAMIC_BOUNDARY = "=== DYNAMIC_BOUNDARY ==="


class SystemPromptBuilder:
    """
    Assemble the system prompt from independent sections.

    Stable sections are separated from frequently changing context by
    DYNAMIC_BOUNDARY. This makes the prompt easier to inspect and extend.
    """

    def __init__(
        self,
        workdir: Path = None,
        tools: list = None,
        skill_registry=None,
        memory_manager=None,
    ):
        self.workdir = workdir or WORKDIR
        self.tools = tools or []
        self.skill_registry = skill_registry
        self.memory_manager = memory_manager

    def _build_core(self) -> str:
        return (
            f"You are a coding agent operating in {self.workdir}.\n"
            "Use the provided tools to explore, read, write, and edit files.\n"
            "Always verify before assuming. Prefer reading files over guessing.\n"
            "Use task graph tools for multi-step work: task_create, task_update, task_list, task_get.\n"
            "Represent dependencies with blockedBy/blocks instead of a flat todo list.\n"
            "Keep task status current as work advances. Prefer tools over prose.\n"
            "Use task_unclaimed and task_claim to inspect or assign ready unowned tasks.\n"
            "Use create_worktree, bind_worktree, remove_worktree, keep_worktree, and list_worktrees to isolate code changes for tasks.\n"
            "Use background_run for long-running shell commands, then use check_background or background results to continue.\n"
            "Use cron_create, cron_delete, and cron_list to schedule future prompts when the user asks for timed or recurring work.\n"
            "Use spawn_teammate, send_message, read_inbox, broadcast, and list_teammates for persistent named teammates and mailbox coordination.\n"
            "Use shutdown_request/shutdown_response and plan_approval for structured teammate protocols with request_id tracking.\n"
            "Teammates are autonomous: after using idle, they can poll inboxes and auto-claim ready task graph nodes that match their role.\n"
            "When an auto-claimed task has a worktree binding, teammate file and shell tools should operate inside that worktree.\n"
            "Use the task tool to delegate exploration or subtasks.\n"
            "Use load_skill when a task needs specialized instructions before you act.\n"
            "Keep working step by step, and use compact if the conversation gets too long."
        )

    def _build_tool_listing(self) -> str:
        if not self.tools:
            return ""
        lines = ["# Available tools"]
        for tool in self.tools:
            props = tool.get("input_schema", {}).get("properties", {})
            params = ", ".join(props.keys())
            lines.append(f"- {tool['name']}({params}): {tool['description']}")
        return "\n".join(lines)

    def _build_skill_listing(self) -> str:
        if not self.skill_registry:
            return ""
        available = self.skill_registry.describe_available()
        if not available or available == "(no skills available)":
            return ""
        return "# Available skills\n" + available

    def _build_memory_section(self) -> str:
        if not self.memory_manager:
            return ""
        return self.memory_manager.load_memory_prompt()

    def _build_memory_guidance(self) -> str:
        return MEMORY_GUIDANCE

    def _build_claude_md(self) -> str:
        sources = []
        user_claude = Path.home() / ".claude" / "CLAUDE.md"
        if user_claude.exists():
            sources.append(("user global (~/.claude/CLAUDE.md)", user_claude.read_text()))
        project_claude = self.workdir / "CLAUDE.md"
        if project_claude.exists():
            sources.append(("project root (CLAUDE.md)", project_claude.read_text()))
        cwd = Path.cwd()
        if cwd != self.workdir:
            subdir_claude = cwd / "CLAUDE.md"
            if subdir_claude.exists():
                sources.append((f"subdir ({cwd.name}/CLAUDE.md)", subdir_claude.read_text()))
        if not sources:
            return ""
        parts = ["# CLAUDE.md instructions"]
        for label, content in sources:
            parts.append(f"## From {label}")
            parts.append(content.strip())
        return "\n\n".join(parts)

    def _build_dynamic_context(self) -> str:
        lines = [
            f"Current date: {datetime.date.today().isoformat()}",
            f"Working directory: {self.workdir}",
            f"Model: {MODEL}",
            f"Platform: {os.uname().sysname}",
        ]
        return "# Dynamic context\n" + "\n".join(lines)

    def build_sections(self) -> list[str]:
        sections = []
        for section in (
            self._build_core(),
            self._build_tool_listing(),
            self._build_skill_listing(),
            self._build_memory_section(),
            self._build_memory_guidance(),
            self._build_claude_md(),
        ):
            if section:
                sections.append(section)
        sections.append(DYNAMIC_BOUNDARY)
        dynamic = self._build_dynamic_context()
        if dynamic:
            sections.append(dynamic)
        return sections

    def build(self) -> str:
        return "\n\n".join(self.build_sections())


def build_system_reminder(extra: str = None) -> dict | None:
    if not extra:
        return None
    content = f"<system-reminder>\n{extra}\n</system-reminder>"
    return {"role": "user", "content": content}
