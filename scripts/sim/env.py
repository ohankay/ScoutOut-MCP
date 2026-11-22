"""Simulation environment on the dev SQL Server (sa from .env):

    python scripts/sim/env.py setup          BoaCatalog (masking list) + KartKopya (restored copy of the synthetic DB)
    python scripts/sim/env.py add-masking DB SCHEMA TABLE COLUMN ALGORITHM [TERM]   the main system changes its list
    python scripts/sim/env.py config NAME [LLM_PORT]   writes sim_out/NAME/config.yaml for one MCP server
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from scoutout.core.env import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")
from scoutout.synth.build import ServerConfig, build  # noqa: E402

SYNTH, CATALOG, COPY = "ScoutOutSynthSmall", "BoaCatalog", "KartKopya"
ENTRIES = [  # (database, schema, table, column, algorithm, term)
    (SYNTH, "bank", "Customer", "TCKN", "S_RND_PHONE", "Kimlik No"),           # sensitive, wrong algorithm
    (SYNTH, "bank", "Customer", "FirstName", "S_DET_NAME", "Ad"),              # correct
    (SYNTH, "bank", "Customer", "LastName", "S_DET_NAME", "Soyad"),            # correct
    (SYNTH, "bank", "Customer", "AnneKizlikSoyadi", "S_DET_NAME", "Anne kızlık soyadı"),  # column not in the database
    (SYNTH, "bank", "TxnLog", "RefNo", "N_DET_NUMBER", "Referans"),           # look-alike numbers: unnecessary
    (SYNTH, "bank", "TxnLog", "Description", "S_NULLIFICATION", "Açıklama"),  # clean free text: unnecessary
    (SYNTH, "bank", "Card", "CardNo", "S_DET_CARD", "Kart No"),               # correct
    (SYNTH, "ops", "LegacyImport", "TCKN", "N_DET_NUMBER", "Kimlik No"),      # empty column: unnecessary
    (SYNTH, "ops", "CustomerNote", "LongNote", "S_NULLIFICATION", "Not"),     # sparse: lighter masking is enough
    (SYNTH, "ops", "Arsiv2019", "Aciklama", "S_NULLIFICATION", "Not"),        # table not in the database
    (COPY, "bank", "Card", "CardNo", "S_DET_CARD", "Kart No"),                # the copy has its own entry
]


def setup() -> None:
    cfg = ServerConfig.from_env()
    if not (ROOT / "synth_out" / SYNTH / "manifest.json").exists():
        build("small", 42, ROOT / "synth_out", cfg, log=print)
    m = cfg.connect()
    for db in (CATALOG, COPY):
        m.execute(f"IF DB_ID('{db}') IS NOT NULL BEGIN ALTER DATABASE [{db}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE; "
                  f"DROP DATABASE [{db}]; END")
    m.execute(f"CREATE DATABASE [{CATALOG}]")
    c = cfg.connect(CATALOG)
    c.execute("CREATE SCHEMA Dtg")
    c.execute("CREATE TABLE Dtg.TableColumnDataMaskDetail (TableColumnDataMaskDetailId int IDENTITY PRIMARY KEY, "
              "TermName nvarchar(100), DatabaseName sysname, SchemaName sysname, TableName sysname, ColumnName sysname, "
              "DataType varchar(30), DataMaskTypeId int, DataMaskType varchar(50), Production bit, Development bit)")
    for e in ENTRIES:
        c.execute("INSERT INTO Dtg.TableColumnDataMaskDetail (DatabaseName, SchemaName, TableName, ColumnName, DataMaskType, "
                  "TermName, Production, Development) VALUES (?,?,?,?,?,?,1,1)", *e)
    c.execute("CREATE USER scoutout_ro FOR LOGIN scoutout_ro")
    c.execute("ALTER ROLE db_datareader ADD MEMBER scoutout_ro")
    c.close()
    # a restored copy, like the daily copies of the live databases
    files = m.execute(f"SELECT name, type_desc FROM sys.master_files WHERE database_id = DB_ID('{SYNTH}')").fetchall()
    cur = m.cursor()
    cur.execute(f"BACKUP DATABASE [{SYNTH}] TO DISK = '/var/opt/mssql/data/sim_copy.bak' WITH INIT, COPY_ONLY")
    while cur.nextset():
        pass
    moves = ", ".join(f"MOVE '{n}' TO '/var/opt/mssql/data/{COPY}_{i}.{'ldf' if t == 'LOG' else 'mdf'}'"
                      for i, (n, t) in enumerate(files))
    cur.execute(f"RESTORE DATABASE [{COPY}] FROM DISK = '/var/opt/mssql/data/sim_copy.bak' WITH {moves}, REPLACE")
    while cur.nextset():
        pass
    m.close()
    print(f"hazır: {CATALOG} ({len(ENTRIES)} maskeleme tanımı), {COPY} (restore edilmiş kopya)")


def add_masking(db, schema, table, column, algorithm, term="") -> None:
    c = ServerConfig.from_env().connect(CATALOG)
    c.execute("INSERT INTO Dtg.TableColumnDataMaskDetail (DatabaseName, SchemaName, TableName, ColumnName, DataMaskType, TermName, "
              "Production, Development) VALUES (?,?,?,?,?,?,1,1)", db, schema, table, column, algorithm, term)
    c.close()
    print(f"ana sistemde eklendi: {db}.{schema}.{table}.{column} ({algorithm})")


def config(name: str, llm_port: str = "11500") -> None:
    out = ROOT / "sim_out" / name
    out.mkdir(parents=True, exist_ok=True)
    (out / "pw").write_text(os.environ["SCOUTOUT_RO_PASSWORD"], encoding="utf-8")
    (out / "config.yaml").write_text(f"""connection:
  server: "localhost,14333"
  user: scoutout_ro
  password_file: '{(out / "pw").as_posix()}'
  trust_server_certificate: true
mcp:
  databases: [{SYNTH}, {COPY}]
  masking_database: {CATALOG}
llm:
  endpoint: http://127.0.0.1:{llm_port}/v1
  model: stub-rule-miner
  allowed_hosts: [127.0.0.1]
  extra_body:
    chat_template_kwargs: {{enable_thinking: false}}
""", encoding="utf-8")
    print(out / "config.yaml")


if __name__ == "__main__":
    cmd, *args = sys.argv[1:]
    {"setup": setup, "add-masking": add_masking, "config": config}[cmd](*args)
