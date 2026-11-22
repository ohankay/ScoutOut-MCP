"""A stand-in for the on-premise LLM (OpenAI-compatible): proposes a format rule per type from the examples it is shown,
plus - like a careless model - one rule that copies a real value (the server must reject it).

    python scripts/sim/stub_llm.py 11500
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from scoutout.metadata_detector import tokens  # noqa: E402
from scoutout.rules import shape_pattern  # noqa: E402

CALLS = Path(__file__).resolve().parents[2] / "sim_out" / "stub_llm_requests.jsonl"


def answer(user: dict) -> dict:
    rules = []
    for dtype, info in user.get("types", {}).items():
        pat = shape_pattern(info.get("examples", []), min_share=0.8)
        cols = sorted({"_".join(tokens(c.split(".")[-1])) for c in info.get("columns", [])})
        if pat:
            rules.append({"data_type": dtype, "pattern": pat, "column_names": cols, "context": []})
        if info.get("examples"):
            rules.append({"data_type": dtype, "pattern": info["examples"][0]})  # careless: a real value
    return {"rules": rules}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send({"data": [{"id": "stub-rule-miner"}]} if self.path.endswith("/models") else {})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        user = json.loads(body["messages"][-1]["content"])
        with CALLS.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"types": list(user.get("types", {})), "extra": body.get("chat_template_kwargs")}) + "\n")
        content = "<think>tamam</think>" + json.dumps(answer(user))
        self._send({"choices": [{"message": {"role": "assistant", "content": content}}]})


if __name__ == "__main__":
    CALLS.parent.mkdir(exist_ok=True)
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1] if len(sys.argv) > 1 else 11500)), Handler).serve_forever()
