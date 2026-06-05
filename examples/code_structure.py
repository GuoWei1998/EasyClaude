import os
import subprocess
from dataclasses import dataclass
try:
    import readline
    # #143 UTF-8 backspace fix for macOS libedit
    readline.parse_and_bind('set bind-tty-special-chars off')
    readline.parse_and_bind('set input-meta on')
    readline.parse_and_bind('set output-meta on')
    readline.parse_and_bind('set convert-meta off')
    readline.parse_and_bind('set enable-meta-keybindings on')
except ImportError:
    pass
from anthropic import Anthropic
from dotenv import load_dotenv
script_dir = os.path.dirname(os.path.abspath(__file__))
dotenv_path = os.path.join(script_dir, ".env")
load_dotenv(dotenv_path=dotenv_path, override=True)
client = Anthropic(
    api_key=os.getenv("ANTHROPIC_AUTH_TOKEN"),
    base_url=os.getenv("ANTHROPIC_BASE_URL") or None,
)
MODEL = os.getenv("MODEL_ID")
if not MODEL:
    raise RuntimeError("Missing MODEL_ID environment variable")
SYSTEM = (
    f"You are a coding agent at {os.getcwd()}. "
    "Use bash to inspect and change the workspace. Act first, then report clearly."
)
TOOL_HANDLERS = {
    "bash":       lambda **kw: run_bash(kw["command"]),
    "read_file":  lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
}
TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
]

def run_bash(command: str) -> str:
    pass

def run_read(path: str, limit: int = None) -> str:
    pass

def run_write(path: str, content: str) -> str:
    pass

def run_edit(path: str, old_text: str, new_text: str) -> str:
    pass

# 主流程

@dataclass
class LoopState:
    # The minimal loop state: history, loop count, and why we continue.
    messages: list
    turn_count: int = 1
    transition_reason: str | None = None

def execute_tool_calls(response_content: str) -> list[dict]:
    results = []
    # 解析工具调用
    for block in response_content:
        if block.type != "tool_use":
            continue
        handler = TOOL_HANDLERS.get(block.tool_name)
        output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
        results.append({
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": output,
        })
        return results

def run_one_turn(state: LoopState) -> bool:
    # 1、调用大模型获取响应
    response = client.messages.create(
        model=MODEL,
        system=SYSTEM,
        messages=state.messages,
        tools=TOOLS,
        max_tokens=8000,
    )

    # 2、将模型响应添加到历史记录中
    state.messages.append({"role": "assistant", "content": response.content})

    # 3、检查响应中是否有工具调用，如果没有，则结束循环
    if response.stop_reason != "tool_use":
        state.transition_reason = None
        return False
    
    # 4、执行工具调用并用工具输出更新状态
    results = execute_tool_calls(response.content)
    if not results:
        state.transition_reason = None
        return False
    
    # 5、用工具结果更新状态并继续循环
    state.messages.append({"role": "user", "content": results})
    state.turn_count += 1
    state.transition_reason = "tool_result"

def agent_loop(state: LoopState) -> None:
    # The main agent loop: run one turn, then decide whether to continue.
    while run_one_turn(state):
        continue

def main():
    history = []
    while True:
        # 1、获取用户输入
        try:
            query = input("\033[36ms01 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        # 2、更新历史记录并进入 agent loop
        history.append({"role": "user", "content": query})
        state = LoopState(messages=history)
        agent_loop(state)
        print(f"\033[32mYou: {query}\033[0m")
if __name__ == "__main__":
    main()