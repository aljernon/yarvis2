"""Token counting with on-disk caching."""

import hashlib
import json
import os

import anthropic

from yarvis_ptb.complex_chat import DEFAULT_AGENT_CONFIG

anthropic_client = anthropic.Anthropic()
TOKEN_COUNT_MODEL = DEFAULT_AGENT_CONFIG.sampling.resolve_model_name()

TOKEN_CACHE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".token_cache"
)
os.makedirs(TOKEN_CACHE_DIR, exist_ok=True)


def _cache_key(data: str) -> str:
    return hashlib.sha256(data.encode()).hexdigest()


def _cache_get(key: str) -> int | None:
    path = os.path.join(TOKEN_CACHE_DIR, key)
    if os.path.exists(path):
        return int(open(path).read())
    return None


def _cache_set(key: str, tokens: int):
    path = os.path.join(TOKEN_CACHE_DIR, key)
    with open(path, "w") as f:
        f.write(str(tokens))


def count_tokens_cached(
    *, system: str | None = None, messages: list[dict], tools: list[dict] | None = None
) -> int:
    """Count tokens with on-disk caching keyed by content hash."""
    cache_data = json.dumps(
        {
            "model": TOKEN_COUNT_MODEL,
            "system": system,
            "messages": messages,
            "tools": tools,
        },
        sort_keys=True,
        default=str,
    )
    key = _cache_key(cache_data)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    kwargs = {"model": TOKEN_COUNT_MODEL, "messages": messages}
    if system is not None:
        kwargs["system"] = system
    if tools is not None:
        kwargs["tools"] = tools
    resp = anthropic_client.messages.count_tokens(**kwargs)
    _cache_set(key, resp.input_tokens)
    return resp.input_tokens


def strip_thinking_blocks(messages: list[dict]) -> list[dict]:
    """Remove thinking/redacted_thinking blocks and empty text blocks from messages."""
    cleaned = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            filtered = [
                b
                for b in content
                if not (
                    isinstance(b, dict)
                    and (
                        b.get("type") in ("thinking", "redacted_thinking")
                        or (b.get("type") == "text" and not b.get("text", "").strip())
                    )
                )
            ]
            if filtered:
                cleaned.append({**msg, "content": filtered})
        elif isinstance(content, str) and not content.strip():
            continue
        else:
            cleaned.append(msg)
    return cleaned


def has_tool_use(msg: dict) -> bool:
    content = msg.get("content", [])
    if isinstance(content, str):
        return False
    return any(isinstance(b, dict) and b.get("type") == "tool_use" for b in content)


def msg_to_text(msg: dict) -> str:
    content = msg.get("content", [])
    if isinstance(content, str):
        return content
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            parts.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            parts.append(json.dumps(block.get("input", {})))
        elif block.get("type") == "tool_result":
            rc = block.get("content", "")
            if isinstance(rc, list):
                parts.append(
                    " ".join(b.get("text", "") for b in rc if isinstance(b, dict))
                )
            else:
                parts.append(str(rc))
        elif block.get("type") in ("thinking", "redacted_thinking"):
            pass
    return "\n".join(parts)
