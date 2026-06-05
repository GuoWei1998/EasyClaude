def normalize_messages(messages: list) -> list:
    """Clean messages before sending to the API."""
    cleaned = []
    for msg in messages:
        clean = {"role": msg["role"]}
        if isinstance(msg.get("content"), str):
            clean["content"] = msg["content"]
        elif isinstance(msg.get("content"), list):
            clean_blocks = []
            for block in msg["content"]:
                if isinstance(block, dict):
                    clean_blocks.append({k: v for k, v in block.items() if not k.startswith("_")})
                elif hasattr(block, "model_dump"):
                    clean_blocks.append(block.model_dump())
                elif hasattr(block, "dict"):
                    clean_blocks.append(block.dict())
            clean["content"] = clean_blocks
        else:
            clean["content"] = msg.get("content", "")
        cleaned.append(clean)

    existing_results = set()
    for msg in cleaned:
        if isinstance(msg.get("content"), list):
            for block in msg["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    existing_results.add(block.get("tool_use_id"))

    for msg in cleaned:
        if msg["role"] != "assistant" or not isinstance(msg.get("content"), list):
            continue
        for block in msg["content"]:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("id") not in existing_results:
                cleaned.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": block["id"],
                        "content": "(cancelled)",
                    }],
                })

    if not cleaned:
        return cleaned
    merged = [cleaned[0]]
    for msg in cleaned[1:]:
        if msg["role"] == merged[-1]["role"]:
            prev = merged[-1]
            prev_c = prev["content"] if isinstance(prev["content"], list) else [
                {"type": "text", "text": str(prev["content"])}
            ]
            curr_c = msg["content"] if isinstance(msg["content"], list) else [
                {"type": "text", "text": str(msg["content"])}
            ]
            prev["content"] = prev_c + curr_c
        else:
            merged.append(msg)
    return merged


def extract_text(content) -> str:
    if not isinstance(content, list):
        return ""
    texts = []
    for block in content:
        text = getattr(block, "text", None)
        if text:
            texts.append(text)
    return "\n".join(texts).strip()

