from __future__ import annotations

import json
import os
import math
from typing import Protocol
import urllib.request



class PlannerModel(Protocol):
    def complete(self, prompt: str) -> str: ...


def reviewer_model_from_env():
    """Optional independent endpoint; partial configuration fails closed."""
    base = os.getenv('SQL_AGENT_REVIEWER_BASE_URL', '').strip()
    model = os.getenv('SQL_AGENT_REVIEWER_MODEL', '').strip()
    if not base and not model:
        return None
    if not base or not model:
        raise ValueError('reviewer requires both base URL and model')
    timeout = float(os.getenv('SQL_AGENT_REVIEWER_TIMEOUT_SECONDS', '60'))
    if not math.isfinite(timeout) or not 0 < timeout <= 300:
        raise ValueError('invalid reviewer timeout')
    provider = os.getenv('SQL_AGENT_REVIEWER_PROVIDER', 'ollama')
    if provider == 'ollama':
        return OllamaPlannerModel(base, model, timeout=timeout,
            max_tokens=int(os.getenv('SQL_AGENT_REVIEWER_MAX_TOKENS', '512')),
            keep_alive_seconds=(int(os.environ['SQL_AGENT_REVIEWER_KEEP_ALIVE_SECONDS'])
                if 'SQL_AGENT_REVIEWER_KEEP_ALIVE_SECONDS' in os.environ else None))
    if provider == 'openai_compatible':
        return OpenAICompatiblePlannerModel(base, model,
            api_key=os.getenv('SQL_AGENT_REVIEWER_API_KEY', ''), timeout=timeout)
    raise ValueError('unsupported reviewer provider')


class OpenAICompatiblePlannerModel:
    """Small provider adapter; compatible with local vLLM and hosted chat APIs."""

    def __init__(self, base_url: str, model: str, api_key: str = "", timeout: float = 10.0) -> None:
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    def complete(self, prompt: str) -> str:
        return self.complete_with_metadata(prompt)[0]

    def complete_with_metadata(self, prompt: str) -> tuple[str, dict]:
        body = json.dumps({
            "model": self.model,
            "temperature": 0,
            "max_tokens": 256,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
        }).encode()
        request = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json"})
        if self.api_key:
            request.add_header("Authorization", f"Bearer {self.api_key}")
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            value = json.loads(response.read())
        usage = value.get('usage') or {}
        return str(value["choices"][0]["message"]["content"]), {
            'prompt_tokens': usage.get('prompt_tokens'),
            'completion_tokens': usage.get('completion_tokens'),
        }


class OllamaPlannerModel:
    """Ollama adapter with an explicit, bounded generation profile."""

    def __init__(self, base_url: str, model: str, timeout: float = 30.0,
                 *, max_tokens: int = 256, json_mode: bool = True, thinking: bool = False, seed: int = 917,
                 keep_alive_seconds: int | None = None) -> None:
        if type(max_tokens) is not int or not 1 <= max_tokens <= 8192:
            raise ValueError('invalid generation token budget')
        self.url = base_url.rstrip("/") + "/api/chat"
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens
        if thinking and json_mode:
            raise ValueError("thinking requires SQL text output")
        self.json_mode = json_mode
        self.thinking = thinking
        self.seed = seed
        if keep_alive_seconds is not None and (type(keep_alive_seconds) is not int or not 0 <= keep_alive_seconds <= 600):
            raise ValueError('model residency must be 0..600 seconds or unset')
        self.keep_alive_seconds = keep_alive_seconds

    def complete(self, prompt: str) -> str:
        return self.complete_with_metadata(prompt)[0]

    def complete_with_metadata(self, prompt: str) -> tuple[str, dict]:
        payload = {
            "model": self.model,
            "stream": False,
            "think": self.thinking,
            "options": {"temperature": 0, "num_predict": self.max_tokens},
            "messages": [{"role": "user", "content": prompt}],
        }
        if self.thinking:
            payload['options'].update(temperature=0.6, top_p=0.95, top_k=20, min_p=0, num_ctx=16384, seed=self.seed)
        if self.json_mode:
            payload['format'] = 'json'
        if self.keep_alive_seconds is not None:
            payload['keep_alive'] = self.keep_alive_seconds
        body = json.dumps(payload).encode()
        request = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            value = json.loads(response.read())
        metadata = {
            "prompt_tokens": value.get("prompt_eval_count"),
            "completion_tokens": value.get("eval_count"),
            "total_duration_ns": value.get("total_duration"),
            "load_duration_ns": value.get("load_duration"),
            "prompt_eval_duration_ns": value.get("prompt_eval_duration"),
            "eval_duration_ns": value.get("eval_duration"),
            "done_reason": value.get("done_reason"),
        }
        return str(value["message"]["content"]), metadata
