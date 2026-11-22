"""An OpenAI-compatible endpoint whose answers come from another agent (simulating the corporate LLM).

    python scripts/sim/relay_llm.py 11600          the endpoint (MCP server's llm.endpoint = http://127.0.0.1:11600/v1)
    python scripts/sim/relay_llm.py wait           (LLM agent) blocks until a request arrives, prints it; "STOP" = done
    python scripts/sim/relay_llm.py answer ID FILE (LLM agent) the answer for request ID (file with the reply text)
    python scripts/sim/relay_llm.py stop           ends the LLM agent's loop

Requests and answers live in sim_out/llm_relay/ (req_N.json, resp_N.txt).
"""

from __future__ import annotations

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DIR = Path(__file__).resolve().parents[2] / "sim_out" / "llm_relay"
WAIT_S = 900


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send({"data": [{"id": "/data/models--Qwen--Qwen3-235B-FP8"}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        n = len(list(DIR.glob("req_*.json"))) + 1
        (DIR / f"req_{n}.json").write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
        resp = DIR / f"resp_{n}.txt"
        deadline = time.monotonic() + WAIT_S
        while not resp.exists() and time.monotonic() < deadline:
            time.sleep(0.5)
        content = resp.read_text(encoding="utf-8") if resp.exists() else "zaman aşımı"
        self._send({"choices": [{"message": {"role": "assistant", "content": content}}]})


def wait() -> None:
    while True:
        if (DIR / "STOP").exists():
            print("STOP")
            return
        for req in sorted(DIR.glob("req_*.json"), key=lambda p: int(p.stem[4:])):
            n = req.stem[4:]
            if not (DIR / f"resp_{n}.txt").exists():
                body = json.loads(req.read_text(encoding="utf-8"))
                print(f"İSTEK {n}  (model={body.get('model')}, temperature={body.get('temperature')}, "
                      f"chat_template_kwargs={body.get('chat_template_kwargs')}, response_format={body.get('response_format')})")
                for m in body["messages"]:
                    print(f"\n===== {m['role'].upper()} =====\n{m['content']}")
                return
        time.sleep(1)


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8")
    DIR.mkdir(parents=True, exist_ok=True)
    cmd = sys.argv[1]
    if cmd == "wait":
        wait()
    elif cmd == "answer":
        text = Path(sys.argv[3]).read_text(encoding="utf-8")
        (DIR / f"resp_{sys.argv[2]}.txt").write_text(text, encoding="utf-8")
        print(f"cevap {sys.argv[2]} gönderildi ({len(text)} karakter)")
    elif cmd == "stop":
        (DIR / "STOP").write_text("", encoding="utf-8")
    else:
        ThreadingHTTPServer(("127.0.0.1", int(cmd)), Handler).serve_forever()
