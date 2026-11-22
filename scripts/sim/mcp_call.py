"""What a Copilot agent does over MCP, from the command line (for simulations):

    python scripts/sim/mcp_call.py PORT --list              tools and their descriptions
    python scripts/sim/mcp_call.py PORT TOOL '{"arg": 1}'   call one tool, print its answer
    python scripts/sim/mcp_call.py PORT TOOL @args.json     arguments from a file

Every call and answer is appended to sim_out/calls_PORT.jsonl (reviewed afterwards for value leaks).
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from mcp import Client

OUT = Path(__file__).resolve().parents[2] / "sim_out"


async def main(port: int, tool: str, args: dict) -> str:
    async with Client(f"http://127.0.0.1:{port}/mcp") as c:
        if tool == "--list":
            tools = (await c.list_tools()).tools
            return "\n\n".join(f"## {t.name}\n{t.description}\nparametreler: "
                               f"{json.dumps(t.input_schema.get('properties', {}), ensure_ascii=False)}" for t in tools)
        r = await c.call_tool(tool, args, read_timeout_seconds=600)
        return "".join(getattr(x, "text", "") for x in r.content)


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8")
    port, tool = int(sys.argv[1]), sys.argv[2]
    raw = sys.argv[3] if len(sys.argv) > 3 else "{}"
    args = json.loads(Path(raw[1:]).read_text(encoding="utf-8") if raw.startswith("@") else raw)
    text = asyncio.run(main(port, tool, args))
    OUT.mkdir(exist_ok=True)
    with (OUT / f"calls_{port}.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"tool": tool, "args": args, "answer": text}, ensure_ascii=False) + "\n")
    print(text)
