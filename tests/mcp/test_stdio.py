"""The real thing: the server as VS Code starts it (subprocess over stdio, config file, password file)."""

import asyncio
import json
import os
import sys

import pytest
from mcp import Client, StdioServerParameters

pytestmark = pytest.mark.mssql


def test_server_over_stdio(server_cfg, small_db, tmp_path):
    (tmp_path / "pw").write_text(os.environ["SCOUTOUT_RO_PASSWORD"], encoding="utf-8")
    (tmp_path / "config.yaml").write_text(f"""
connection:
  server: "{server_cfg.server}"
  user: scoutout_ro
  password_file: '{(tmp_path / "pw").as_posix()}'
  trust_server_certificate: true
mcp:
  databases: [ScoutOutSynthSmall]
""", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("SCOUTOUT_")} | {"SCOUTOUT_CONFIG": str(tmp_path / "config.yaml")}
    params = StdioServerParameters(command=sys.executable, args=["-m", "scoutout_mcp.server"], env=env)

    async def go():
        async with Client(params) as c:
            names = {t.name for t in (await c.list_tools()).tools}
            dbs = json.loads((await c.call_tool("list_databases", {})).content[0].text)
            desc = json.loads((await c.call_tool("describe_table", {"database": "ScoutOutSynthSmall",
                                                                    "table": "bank.Customer"})).content[0].text)
            return names, dbs, desc

    names, dbs, desc = asyncio.run(go())
    assert {"scan", "audit_masking", "mine_rules", "decide_rule"} <= names
    assert [d["database"] for d in dbs["databases"]] == ["ScoutOutSynthSmall"]
    assert "TCKN" in {c["column"] for c in desc["columns"]}
    assert (tmp_path / "data" / "scoutout.db").exists()  # data next to the config, outside the workspace
    assert not list(tmp_path.glob("**/pw*"))[1:]  # the password file was not copied anywhere
