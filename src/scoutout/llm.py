"""Local LLM client (OpenAI-compatible chat API, e.g. Ollama or vLLM). Refuses non-local hosts (spec §4.3)."""

from __future__ import annotations

import json
from urllib.parse import urlparse

import httpx


def check_endpoint(endpoint: str, allowed_hosts: list[str]) -> None:
    u = urlparse(endpoint)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("LLM adresi http(s)://sunucu:port/v1 biçiminde olmalı.")
    if u.hostname.lower() not in {h.lower() for h in allowed_hosts}:
        raise ValueError(f"LLM sunucusu '{u.hostname}' izin listesinde değil. Sadece yerel/izinli sunucular kullanılabilir.")


class LlmClient:
    def __init__(self, endpoint: str, model: str, allowed_hosts: list[str], timeout_s: int = 120, transport=None):
        check_endpoint(endpoint, allowed_hosts)
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.http = httpx.Client(timeout=timeout_s, transport=transport)

    def chat_json(self, system: str, user: str) -> dict:
        """One chat completion that must return a JSON object."""
        r = self.http.post(f"{self.endpoint}/chat/completions", json={
            "model": self.model, "temperature": 0, "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        })
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        start, end = content.find("{"), content.rfind("}")
        if start < 0 or end < start:
            raise ValueError("LLM JSON döndürmedi")
        return json.loads(content[start : end + 1])
