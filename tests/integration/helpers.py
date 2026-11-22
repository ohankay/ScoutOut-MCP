import os

from scoutout.connector.mssql import MsSqlAdapter
from scoutout.core.config import ConnectionConfig


def adapter(server_cfg, database: str, user: str = "sa") -> MsSqlAdapter:
    env = {"sa": "MSSQL_SA_PASSWORD", "scoutout_ro": "SCOUTOUT_RO_PASSWORD", "scoutout_rw": "SCOUTOUT_RW_PASSWORD"}[user]
    return MsSqlAdapter(
        ConnectionConfig(server=server_cfg.server, database=database, user=user, password_env=env,
                         trust_server_certificate=True)
    )


class Recorder:
    """Adapter wrapper that records every SQL statement the sampler sends."""

    def __init__(self, inner):
        self.inner = inner
        self.sql: list[str] = []
        self.queries_executed = 0

    def query(self, sql, *params):
        self.sql.append(sql)
        self.queries_executed += 1
        return self.inner.query(sql, *params)

    def __getattr__(self, name):
        return getattr(self.inner, name)


def env_present(name: str) -> bool:
    return bool(os.environ.get(name))
