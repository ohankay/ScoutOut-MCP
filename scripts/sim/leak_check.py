"""After a simulation: did any real value reach the agent (MCP answers), the stores, the logs or the reports?

    python scripts/sim/leak_check.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from scoutout.core.env import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")
from scoutout.synth.build import ServerConfig  # noqa: E402

OUT = ROOT / "sim_out"


def real_values() -> set[str]:
    manifest = json.loads((ROOT / "synth_out" / "ScoutOutSynthSmall" / "manifest.json").read_text(encoding="utf-8"))
    c = ServerConfig.from_env().connect("ScoutOutSynthSmall")
    values: set[str] = set()
    for t in manifest["tables"]:
        for col in t["columns"]:
            if col["expected_zone"] in ("SENSITIVE", "SPARSE") or col["expected_type"]:
                rows = c.execute(f"SELECT TOP 2000 CAST([{col['name']}] AS nvarchar(4000)) FROM [{t['schema']}].[{t['name']}] "
                                 f"WHERE [{col['name']}] IS NOT NULL").fetchall()
                for (v,) in rows:
                    v = str(v).strip()
                    if len(v) >= 6:
                        values.add(v)
                        values.update(w.strip(".,;:") for w in v.split() if len(w) >= 8 and any(ch.isdigit() for ch in w))
    c.close()
    return values


def texts() -> dict[str, str]:
    out = {}
    for f in OUT.glob("calls_*.jsonl"):
        out[f.name] = "\n".join(json.loads(line)["answer"] for line in f.read_text(encoding="utf-8").splitlines() if line)
    for db in OUT.glob("*/data/scoutout.db"):
        con = sqlite3.connect(db)
        out[str(db.relative_to(OUT))] = "\n".join(str(r) for t in ("decisions", "rules", "audit", "baseline_columns",
                                                                    "masking_lists", "settings")
                                                  for r in con.execute(f"SELECT * FROM {t}"))
        con.close()
    for log in OUT.glob("*/data/logs/*.log"):
        out[str(log.relative_to(OUT))] = log.read_text(encoding="utf-8", errors="replace")
    import openpyxl

    for x in OUT.glob("*/data/reports/*.xlsx"):
        cells = [str(c) for ws in openpyxl.load_workbook(x, read_only=True) for row in ws.iter_rows(values_only=True)
                 for c in row if c is not None]
        out[str(x.relative_to(OUT))] = "\n".join(cells)
    return out


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    values = real_values()
    found = 0
    for name, text in texts().items():
        hits = [v for v in values if v in text]
        found += len(hits)
        print(f"{'SIZINTI' if hits else 'temiz  '}  {name}  ({len(text):,} karakter){'  örn. ' + hits[0][:4] + '…' if hits else ''}")
    print(f"\n{len(values):,} gerçek değer kontrol edildi; {'HİÇBİRİ bulunmadı' if not found else f'{found} eşleşme'}")
    sys.exit(1 if found else 0)
