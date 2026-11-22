import json
from pathlib import Path

import pytest

from scoutout.core.env import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")


@pytest.fixture(scope="session")
def server_cfg():
    """Dev SQL Server (docker compose --profile dev up -d mssql). Skips if unreachable."""
    try:
        from scoutout.synth.build import ServerConfig

        cfg = ServerConfig.from_env()
        cfg.connect().close()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"SQL Server not available: {exc}")
    return cfg


@pytest.fixture(scope="session")
def small_db(server_cfg):
    """Connection to ScoutOutSynthSmall and its manifest (built once if missing)."""
    from scoutout.synth.build import build

    manifest_path = ROOT / "synth_out" / "ScoutOutSynthSmall" / "manifest.json"
    if not manifest_path.exists():
        build("small", 42, ROOT / "synth_out", server_cfg, log=lambda *_: None)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    conn = server_cfg.connect("ScoutOutSynthSmall")
    yield conn, manifest
    conn.close()
