from dataclasses import dataclass

from .compact import (
    CONTEXT_LIMIT,
    CompactState,
    compact_history,
    estimate_context_size,
    micro_compact,
    persist_large_output,
    track_recent_file,
)
from .config import MODEL, WORKDIR, client
from .messages import normalize_messages
from .permissions import PermissionManager
from .tools import CHILD_TOOLS, TOOLS, build_tool_handlers


SUBAGENT_SYSTEM = f"You are a coding subagent at {WORKDIR}. Complete the given task, then summarize your findings."


@dataclass
class LoopState:
    messages: list
    turn_count: int = 1
    transition_reason: str | None = None


class AgentRuntime:
    def __init__(self, system: str, todo, skill_registry, perms: PermissionManager, hooks):
        self.system = system
        self.todo = todo
        self.perms = perms
        self.hooks = hooks
        self.tool_handlers = build_tool_handlers(todo, skill_registry)

    def run_subagent(self, prompt: str) -> str:
        sub_messages = [{"role": "user", "content": prompt}]
        child_handlers = {
            name: handler
            for name, handler in self.tool_handlers.items()
            if name in {"bash", "read_file", "write_file", "edit_file"}
        }
        for _ in range(30):
            response = client.messages.create(
                model=MODEL,
                system=SUBAGENT_SYSTEM,
                messages=sub_messages,
                tools=CHILD_TOOLS,
                max_tokens=8000,
            )
            sub_messages.append({"role": "assistant", "content": response.content})
            if response.stop_reason != "tool_use":
                break
            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                handler = child_handlers.get(block.name)
                output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(output)[:50000],
                })
            sub_messages.append({"role": "user", "content": results})
        return "".join(b.text for b in response.content if hasattr(b, "text")) or "(no summary)"

    def execute_tool_calls(self, response_content, compact_state: CompactState) -> tuple[list[dict], bool, str | None]:
        results = []
        manual_compact = False
        compact_focus = None
        used_todo = False
        for block in response_content:
            if block.type != "tool_use":
                continue

            tool_input = dict(block.input or {})
            hook_context = {"tool_name": block.name, "tool_input": tool_input}
            pre_result = self.hooks.run_hooks("PreToolUse", hook_context)
            hook_notes = [f"[Hook message]: {msg}" for msg in pre_result.get("messages", [])]
            if pre_result.get("blocked"):
                reason = pre_result.get("block_reason", "Blocked by hook")
                output = f"Tool blocked by PreToolUse hook: {reason}"
                if hook_notes:
                    output = f"{output}\n" + "\n".join(hook_notes)
                content = persist_large_output(block.id, str(output))
                print(f"> {block.name}: {content[:200]}")
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": content,
                })
                print(block.name, "\n")
                continue
            tool_input = hook_context.get("tool_input", tool_input)

            permission_override = pre_result.get("permission_override")
            if permission_override in {"allow", "deny", "ask"}:
                decision = {
                    "behavior": permission_override,
                    "reason": f"PreToolUse hook returned permissionDecision={permission_override}",
                }
            else:
                decision = self.perms.check(block.name, tool_input)

            if decision["behavior"] == "deny":
                output = f"Permission denied: {decision['reason']}"
                print(f"  [DENIED] {block.name}: {decision['reason']}")
            elif decision["behavior"] == "ask" and not self.perms.ask_user(block.name, tool_input):
                output = "Permission denied by user"
                print(f"  [USER DENIED] {block.name}: {decision['reason']}")
            else:
                if block.name == "task":
                    desc = tool_input.get("description", "subtask")
                    prompt = tool_input.get("prompt", "")
                    print(f"> task ({desc}): {prompt[:80]}")
                    output = self.run_subagent(prompt)
                else:
                    handler = self.tool_handlers.get(block.name)
                    output = handler(**tool_input) if handler else f"Unknown tool: {block.name}"
                if block.name in {"read_file", "write_file", "edit_file"}:
                    path = tool_input.get("path")
                    if path:
                        track_recent_file(compact_state, path)
                if block.name == "compact":
                    manual_compact = True
                    compact_focus = tool_input.get("focus")
                hook_context["tool_output"] = output
                post_result = self.hooks.run_hooks("PostToolUse", hook_context)
                for message in post_result.get("messages", []):
                    hook_notes.append(f"[Hook note]: {message}")

            if hook_notes:
                output = f"{output}\n" + "\n".join(hook_notes)
            content = persist_large_output(block.id, str(output))
            print(f"> {block.name}: {content[:200]}")
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": content,
            })
            if block.name == "todo":
                used_todo = True
            print(block.name, "\n")

        if used_todo:
            self.todo.state.rounds_since_update = 0
        else:
            self.todo.note_round_without_update()
            reminder = self.todo.reminder()
            if reminder and results:
                results[-1]["content"] = f"{results[-1]['content']}\n\n{reminder}"
        return results, manual_compact, compact_focus

    def run_one_turn(self, state: LoopState, compact_state: CompactState) -> bool:
        state.messages[:] = micro_compact(state.messages)
        if estimate_context_size(state.messages) > CONTEXT_LIMIT:
            print("[auto compact]")
            state.messages[:] = compact_history(state.messages, compact_state)

        response = client.messages.create(
            model=MODEL,
            system=self.system,
            messages=normalize_messages(state.messages),
            tools=TOOLS,
            max_tokens=8000,
        )
        state.messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            state.transition_reason = None
            return False
        results, manual_compact, compact_focus = self.execute_tool_calls(response.content, compact_state)
        if not results:
            state.transition_reason = None
            return False
        state.messages.append({"role": "user", "content": results})
        if manual_compact:
            print("[manual compact]")
            state.messages[:] = compact_history(state.messages, compact_state, focus=compact_focus)
        state.turn_count += 1
        state.transition_reason = "tool_result"
        return True

    def agent_loop(self, state: LoopState, compact_state: CompactState) -> None:
        while self.run_one_turn(state, compact_state):
            pass

