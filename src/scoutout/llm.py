"""On-premise LLM client (OpenAI-compatible chat API: Ollama, vLLM, an internal gateway). Refuses hosts that are not
allow-listed, never goes through a proxy, trusts the operating system's certificate store (corporate CA)."""

from __future__ import annotations

import json
import re
import ssl
from urllib.parse import urlparse

import httpx

_THINK = re.compile(r"<think>.*?</think>", re.S)


def check_endpoint(endpoint: str, allowed_hosts: list[str]) -> None:
    u = urlparse(endpoint)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("LLM adresi http(s)://sunucu[:port]/.../v1 biçiminde olmalı.")
    if u.hostname.lower() not in {h.lower() for h in allowed_hosts}:
        raise ValueError(f"LLM sunucusu '{u.hostname}' izin listesinde değil (llm.allowed_hosts). "
                         "Sadece kurum içi sunucular kullanılabilir.")


def _verify(ca_bundle: str):
    if ca_bundle:
        return ssl.create_default_context(cafile=ca_bundle)
    try:
        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)  # Windows / macOS system store: corporate root CA
    except ImportError:  # pragma: no cover
        return True


class LlmClient:
    def __init__(self, endpoint: str, model: str, allowed_hosts: list[str], timeout_s: int = 120, transport=None,
                 temperature: float = 0.0, max_tokens: int | None = None, extra_body: dict | None = None,
                 api_key: str = "", ca_bundle: str = "", json_mode: bool = True):
        check_endpoint(endpoint, allowed_hosts)
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.temperature, self.max_tokens = temperature, max_tokens
        self.extra_body = dict(extra_body or {})
        self.json_mode = json_mode
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        # trust_env=False: no HTTP(S)_PROXY — internal servers are always reached directly
        self.http = httpx.Client(timeout=timeout_s, transport=transport, headers=headers, trust_env=False,
                                 verify=_verify(ca_bundle) if transport is None else True)

    @classmethod
    def from_config(cls, c, transport=None) -> LlmClient:
        return cls(c.endpoint, c.model, c.allowed_hosts, c.timeout_s, transport, c.temperature, c.max_tokens,
                   c.extra_body, c.api_key, c.ca_bundle, c.json_mode)

    def _post(self, body: dict) -> httpx.Response:
        return self.http.post(f"{self.endpoint}/chat/completions", json=body)

    def chat_json(self, system: str, user: str) -> dict:
        """One chat completion that must return a JSON object."""
        body = {"model": self.model, "temperature": self.temperature,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]} | self.extra_body
        if self.max_tokens:
            body["max_tokens"] = self.max_tokens
        r = self._post(body | ({"response_format": {"type": "json_object"}} if self.json_mode else {}))
        if self.json_mode and r.status_code == 400:  # some gateways / servers do not accept response_format
            r = self._post(body)
        r.raise_for_status()
        content = _THINK.sub("", r.json()["choices"][0]["message"]["content"] or "")
        start, end = content.find("{"), content.rfind("}")
        if start < 0 or end < start:
            raise ValueError("LLM JSON döndürmedi")
        return json.loads(content[start : end + 1])

    def models(self) -> list[str]:
        r = self.http.get(f"{self.endpoint}/models")
        r.raise_for_status()
        return [m.get("id") for m in r.json().get("data", [])]
