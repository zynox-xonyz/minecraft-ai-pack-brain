"""Streaming client for an OpenAI-compatible /chat/completions endpoint (llama-swap). Stdlib only."""
import json
import urllib.request

from . import config


def chat_stream(model, messages, tools=None, max_tokens=700, temperature=0.3, think=False, extra=None, timeout=180):
    """Yield {"type": "content", "text"} for each text delta, then one {"type": "end", "content", "tool_calls", "finish"}.

    Tool-call deltas arrive in fragments; they are assembled into complete OpenAI-style tool_calls here.
    Reasoning tokens (reasoning_content) are dropped. `extra` is merged into the request body (e.g. response_format).
    """
    body = {"model": model, "messages": messages, "max_tokens": max(max_tokens, 4000) if think else max_tokens,
            "temperature": temperature, "stream": True, "chat_template_kwargs": {"enable_thinking": think}}
    if tools:
        body["tools"] = tools
    if extra:
        body.update(extra)
    req = urllib.request.Request(config.get()["llm_base_url"] + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Accept": "text/event-stream"})
    content, calls, finish = [], {}, None
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                choice = json.loads(data)["choices"][0]
            except (ValueError, KeyError, IndexError):
                continue
            delta = choice.get("delta") or {}
            if delta.get("content"):
                content.append(delta["content"])
                yield {"type": "content", "text": delta["content"]}
            for tc in delta.get("tool_calls") or []:
                slot = calls.setdefault(tc.get("index", 0), {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["function"]["name"] += fn["name"]
                if fn.get("arguments"):
                    slot["function"]["arguments"] += fn["arguments"]
            finish = choice.get("finish_reason") or finish
    yield {"type": "end", "content": "".join(content), "tool_calls": [calls[i] for i in sorted(calls)], "finish": finish}
