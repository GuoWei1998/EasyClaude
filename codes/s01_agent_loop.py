#!/usr/bin/env python3
"""
s01_agent_loop.py - The Agent Loop Skeleton

核心思想：
    while stop_reason == "tool_use":
        response = LLM(messages, tools)
        execute tools
        append results

图示：
    +----------+      +-------+      +---------+
    |   User   | ---> |  LLM  | ---> |  Tool   |
    |  prompt  |      |       |      | execute |
    +----------+      +---+---+      +----+----+
                          ^               |
                          |   tool_result |
                          +---------------+
                          (loop continues)

把工具执行结果反馈给大模型，直到模型决定不再调用工具为止。
"""

import os
import json
import subprocess
from openai import OpenAI
from dotenv import load_dotenv

# ==========================================
# 1. 初始化设置
# ==========================================
# 加载 .env 环境变量
load_dotenv(override=True)

# 使用 OpenAI SDK 初始化 DeepSeek Client
client = OpenAI(
    api_key="",
    base_url="https://api.deepseek.com"
)

# 使用 DeepSeek Chat 模型 (支持工具调用)
MODEL = "deepseek-reasoner"

# System Prompt，告诉 Agent 它的身份、所处环境以及它能做什么
SYSTEM = f"You are a coding agent at {os.getcwd()}. Use bash to solve tasks. Act, don't explain."

# 定义 Tools 列表，按照 OpenAI/DeepSeek 的格式要求
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The bash command to run"
                    }
                },
                "required": ["command"],
            },
        }
    }
]

# ==========================================
# 2. 工具实现
# ==========================================
def run_bash(command: str) -> str:
    """
    执行传入的 bash 命令，并返回它的终端输出。
    """
    # TODO: (可选) 定义危险命令列表，防止执行 `rm -rf /` 等危险操作
    
    # TODO: 使用 subprocess.run 执行命令
    # 提示：需要考虑当前工作目录 (cwd)、捕获输出 (capture_output) 和超时机制 (timeout)
    
    # TODO: 合并 stdout 和 stderr，将结果截断防过长，并返回
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(command, shell=True, cwd=os.getcwd(),
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


# ==========================================
# 3. 核心 Agent Loop
# ==========================================
def agent_loop(messages: list):
    """
    核心模式：不断调用 LLM 并执行它要求的工具，直到它不再需要工具为止。
    """
    while True:
        # 第 1 步：调用大模型 (传入 model, system, messages, tools 等参数)
        print("Waiting for model response...") # 占位
        
        # DeepSeek 把 system prompt 当作 messages 的第一条。
        # 为了不破坏我们原本纯粹的对话历史记录 (history)，我们在请求时临时把它拼在最前面
        current_messages = [{"role": "system", "content": SYSTEM}] + messages
        
        response = client.chat.completions.create(
            model=MODEL, 
            messages=current_messages,
            tools=TOOLS, 
            max_tokens=8000,
        )
        print(current_messages)
        print(response)
        
        # 获取模型回复的消息对象
        message = response.choices[0].message
        
        # 第 2 步：把 assistant 的完整回复存入 messages 列表
        # 注意: 无论是单纯的文本回复还是工具调用，我们都需要把 message 原封不动塞回上下文中
        messages.append(message)

        # 第 3 步：检查模型是否请求了工具调用
        if not getattr(message, "tool_calls", None):
            # 如果没有 tool_calls 属性或者为空，说明模型回答完毕，退出循环
            return
        
        # 第 4 步：如果模型要求使用工具，遍历回复中的 tool calls 并执行对应的工具
        for tool_call in message.tool_calls:
            if tool_call.function.name == "bash":
                # 解析模型传过来的 JSON 参数
                args = json.loads(tool_call.function.arguments)
                cmd = args["command"]
                
                print(f"\033[33m$ {cmd}\033[0m")
                output = run_bash(cmd)
                print(output[:200])
                
                # 第 5 步：将工具执行结果作为 'tool' 角色反馈给模型，触发下一轮循环
                # 注意 DeepSeek/OpenAI 需要你提供 tool_call_id
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": tool_call.function.name,
                    "content": output
                })


# ==========================================
# 4. 交互界面 (REPL)
# ==========================================
if __name__ == "__main__":
    history = []
    print("Agent is ready! Type 'q' or 'exit' to quit.")
    
    while True:
        # 第 1 步：获取用户输入
        # TODO: 使用 input() 接收输入，并处理退出逻辑
        try:
            query = input("\033[36ms01 >> \033[0m") 
        except(EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ["q", "exit", ""]:
            break    

        # 第 2 步：将用户的输入添加到 history 中
        history.append({"role": "user", "content": query})
        
        # 第 3 步：把 history 传给 agent_loop 进行处理
        agent_loop(history)
        
        # 第 4 步：从 history 中提取最后一条信息 (通常包含文本块) 进行打印展示
        last_message = history[-1]
        
        # 在 OpenAI SDK 中，assistant 的回复是一个对象；如果是 tool 或者是 user 则是一个字典
        if isinstance(last_message, dict):
            content = last_message.get("content")
        else:
            content = getattr(last_message, "content", None)
            
        if content:
            print(content)
        print()
