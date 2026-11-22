import pytest

from scoutout.connector.base import ColumnInfo
from scoutout.connector.mssql import connection_string
from scoutout.core.config import ConfigError, ConnectionConfig, load_config


def write(tmp_path, text):
    p = tmp_path / "scoutout.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_load_yaml_and_env_override(tmp_path, monkeypatch):
    p = write(tmp_path, "connection:\n  server: db1\n  database: Bank\n  user: reader\nsecurity:\n  on_write_permission: warn\n")
    monkeypatch.setenv("SCOUTOUT_DB_SERVER", "db2,1444")
    monkeypatch.setenv("SCOUTOUT_DB_TRUST_SERVER_CERTIFICATE", "true")
    cfg = load_config(p)
    assert cfg.connection.server == "db2,1444"
    assert cfg.connection.database == "Bank"
    assert cfg.connection.trust_server_certificate is True
    assert cfg.security.on_write_permission == "warn"


def test_default_policy_is_refuse(tmp_path):
    assert load_config(write(tmp_path, "connection:\n  database: Bank\n")).security.on_write_permission == "refuse"


@pytest.mark.parametrize(
    "text",
    [
        "connection:\n  database: Bank\n  colour: blue\n",  # unknown key
        "connection:\n  database: Bank\n  auth: kerberos\n",
        "connection:\n  database: Bank\nsecurity:\n  on_write_permission: maybe\n",
        "connection:\n  server: x\n",  # database missing
    ],
)
def test_invalid_config_rejected(tmp_path, text):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, text))


def test_missing_explicit_file_rejected(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.yaml")


def test_connection_string_sql_auth_escapes_password(monkeypatch):
    monkeypatch.setenv("SCOUTOUT_DB_PASSWORD", "p;w}d")
    cs = connection_string(ConnectionConfig(server="s", database="d", user="u", driver="X"))
    assert "UID=u" in cs and "PWD={p;w}}d}" in cs
    assert "ApplicationIntent=ReadOnly" in cs and "Encrypt=yes" in cs


def test_connection_string_windows_auth_has_no_password():
    cs = connection_string(ConnectionConfig(server="s", database="d", auth="windows", driver="X"))
    assert "Trusted_Connection=yes" in cs and "PWD" not in cs


@pytest.mark.parametrize(
    "col, label, lob",
    [
        (ColumnInfo("a", "nvarchar", 50, 0, 0, True), "nvarchar(50)", False),
        (ColumnInfo("a", "nvarchar", -1, 0, 0, True), "nvarchar(max)", True),
        (ColumnInfo("a", "xml", None, 0, 0, True), "xml", True),
        (ColumnInfo("a", "decimal", None, 18, 2, True), "decimal(18,2)", False),
    ],
)
def test_column_type_label(col, label, lob):
    assert col.type_label == label and col.is_lob is lob
