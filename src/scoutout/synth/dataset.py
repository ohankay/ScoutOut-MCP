"""Synthetic banking dataset: tables, column generators and ground truth.

Each column declares what it really contains (``truth``) — that is what the manifest records and
what end-to-end tests compare the system's decisions against.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from .sqlexpr import GIVEN_NAMES, PLACES, SURNAMES, Expr, Gen, ascii_fold

BANK_CODES = ["00010", "00012", "00015", "00046", "00062", "00064", "00067", "00111", "00205"]
PAN_BINS = ["411111", "454360", "540061", "552879", "979200", "979210"]  # Visa, MC, Troy style
GSM_PREFIXES = ["505", "530", "532", "533", "535", "536", "538", "541", "542", "544", "545", "551", "553", "555"]
CLEAN_SENTENCES = [
    "Müşteri şubeyi aradı, bilgi verildi.",
    "Kredi kartı limit artışı talebi alındı.",
    "Tutar 1250,00 TL iade edildi.",
    "Hesap özeti e-posta ile gönderildi.",
    "İşlem 3 iş günü içinde tamamlanacak.",
    "Randevu 14:30 olarak planlandı.",
    "Talep 2025/118 numaralı kayıtla açıldı.",
    "Müşteri kampanya hakkında bilgi istedi.",
    "Şube 245 üzerinden işlem yapıldı.",
    "Belgeler eksiksiz teslim alındı.",
]


@dataclass
class Truth:
    """Ground truth for a column. ``data_type`` None = not sensitive."""

    data_type: str | None = None
    rate: float = 0.0  # share of non-NULL rows containing data_type
    role: str = ""  # e.g. hard_negative, misleading_name
    requires_baseline: bool = False  # institution-specific: generic detectors cannot know it


@dataclass
class Column:
    name: str
    sql_type: str
    gen: Callable[[Gen], Expr | str]
    truth: Truth = field(default_factory=Truth)
    null_rate: float = 0.0
    nullable: bool = True
    #: SQL predicate (given the Gen) marking rows that carry the sensitive value; used to measure
    #: the exact realised rate. None -> every non-NULL row if truth.data_type else none.
    dirty: Callable[[Gen], str] | None = None


@dataclass
class Table:
    schema: str
    name: str
    columns: list[Column]
    rows: dict[str, int]  # scale -> row count
    clustered: list[str] = field(default_factory=list)  # empty -> heap
    primary_key: bool = True
    partition_by_month: str | None = None  # column name for monthly partitioning
    shape: str = ""

    @property
    def full_name(self) -> str:
        return f"[{self.schema}].[{self.name}]"


# --- helpers --------------------------------------------------------------------------

def _name_idx(g: Gen, salt: str, names: list[str]) -> str:
    return g.below(salt, len(names))


def given_name(salt: str) -> Callable[[Gen], str]:
    return lambda g: g.choose_idx(_name_idx(g, salt, GIVEN_NAMES), GIVEN_NAMES)


def surname(salt: str) -> Callable[[Gen], str]:
    return lambda g: g.choose_idx(_name_idx(g, salt, SURNAMES), SURNAMES)


def full_name_sql(g: Gen, salt: str) -> str:
    return f"CONCAT({given_name(salt + 'g')(g)}, N' ', {surname(salt + 's')(g)})"


def phone_any(salt: str) -> Callable[[Gen], str]:
    def build(g: Gen) -> str:
        pre = g.choose(salt + ":p", GSM_PREFIXES)
        a, b, c = g.digits(salt + ":a", 3), g.digits(salt + ":b", 2), g.digits(salt + ":c", 2)
        fmt = g.below(salt + ":f", 4)
        return (
            f"CASE {fmt} WHEN 0 THEN CONCAT('0', {pre}, ' ', {a}, ' ', {b}, ' ', {c})"
            f" WHEN 1 THEN CONCAT('+90 ', {pre}, ' ', {a}, {b}, {c})"
            f" WHEN 2 THEN CONCAT('0', {pre}, {a}, {b}, {c})"
            f" ELSE CONCAT('(0', {pre}, ') ', {a}, '-', {b}, '-', {c}) END"
        )
    return build


def email(salt: str) -> Callable[[Gen], str]:
    def build(g: Gen) -> str:
        gi, si = _name_idx(g, salt + "g", GIVEN_NAMES), _name_idx(g, salt + "s", SURNAMES)
        gn = g.choose_idx(gi, [ascii_fold(n).lower() for n in GIVEN_NAMES])
        sn = g.choose_idx(si, [ascii_fold(n).lower() for n in SURNAMES])
        dom = g.choose(salt + ":d", ["gmail.com", "hotmail.com", "yahoo.com", "outlook.com", "firma.com.tr"])
        return f"CONCAT({gn}, '.', {sn}, {g.below(salt + ':n', 100)}, '@', {dom})"
    return build


def address(salt: str) -> Callable[[Gen], str]:
    def build(g: Gen) -> str:
        return (
            f"CONCAT({g.choose(salt + ':m', PLACES['neighborhood'])}, N' Mah. ',"
            f" {g.choose(salt + ':c', PLACES['street'])}, N' Cad. No:', 1 + {g.below(salt + ':no', 180)},"
            f" N' D:', 1 + {g.below(salt + ':d', 40)}, N' ', {g.choose(salt + ':i', PLACES['district'])}, N'/',"
            f" {g.choose(salt + ':s', PLACES['city'])})"
        )
    return build


def tckn(salt: str, prefix: str = "") -> Callable[[Gen], Expr]:
    return lambda g: g.tckn(salt, prefix)


def clean_sentence(g: Gen, salt: str) -> str:
    return g.choose(salt, CLEAN_SENTENCES)


def free_text(salt: str, rate: float, kind: str) -> tuple[Callable[[Gen], Expr], Callable[[Gen], str]]:
    """Free-text column where ~rate of rows embed one value of ``kind`` (TCKN/PHONE/IBAN/EMAIL)."""

    def dirty(g: Gen) -> str:
        return g.flag(salt + ":dirty", rate)

    def build(g: Gen) -> Expr:
        applies: list[str] = []
        if kind == "TCKN":
            e = g.tckn(salt + ":v")
            applies, value = e.applies, e.sql
            ctx = g.choose(salt + ":ctx", ["TC ", "TCKN: ", "kimlik no ", "T.C. ", ""])
        elif kind == "PHONE":
            value = phone_any(salt + ":v")(g)
            ctx = g.choose(salt + ":ctx", ["tel ", "GSM: ", "cep ", "telefon ", ""])
        elif kind == "IBAN":
            e = g.iban_tr(salt + ":v", BANK_CODES)
            applies, value = e.applies, e.sql
            ctx = g.choose(salt + ":ctx", ["IBAN ", "hesap ", "IBAN: ", ""])
        elif kind == "EMAIL":
            value = email(salt + ":v")(g)
            ctx = g.choose(salt + ":ctx", ["e-posta ", "mail: ", ""])
        else:
            raise ValueError(kind)
        who = full_name_sql(g, salt + ":who") if kind != "EMAIL" else "N'Müşteri'"
        sql = (
            f"CASE WHEN {dirty(g)} THEN CONCAT({clean_sentence(g, salt + ':s1')}, N' ', {who}, N' ', {ctx},"
            f" {value}, N' ile kayıt güncellendi.') ELSE CONCAT({clean_sentence(g, salt + ':s1')}, N' ',"
            f" {clean_sentence(g, salt + ':s2')}) END"
        )
        return Expr(sql, applies)

    return build, dirty


def customer_no(salt: str) -> Callable[[Gen], str]:
    """Institution-specific customer number 'MN' + 8 digits of a customer id within the first 20,000 customers."""
    return lambda g: f"CONCAT('MN', RIGHT('00000000' + CAST(1 + {g.below(salt, 20_000)} AS varchar(8)), 8))"


def _ft(name: str, rate: float, kind: str, sql_type: str = "nvarchar(400)", pad_chars: int = 0) -> Column:
    build, dirty = free_text(name, rate, kind)
    if pad_chars:
        inner = build

        def build(g: Gen) -> Expr:  # noqa: F811 - LOB: sensitive part first, then long filler
            e = inner(g)
            filler = f"REPLICATE(CAST(N' Ek açıklama satırı.' AS nvarchar(max)), {pad_chars // 20})"
            return Expr(f"CONCAT(CAST({e.sql} AS nvarchar(max)), {filler})", e.applies)

    return Column(name, sql_type, build, Truth(kind if rate > 0 else None, rate), dirty=dirty)


# --- the dataset ----------------------------------------------------------------------

def tables() -> list[Table]:
    return [
        Table(
            "bank", "Customer", shape="clustered_int",
            clustered=["CustomerId"],
            rows={"small": 20_000, "large": 1_000_000},
            columns=[
                Column("CustomerId", "int", lambda g: "t.n", nullable=False),
                Column("TCKN", "char(11)", tckn("cust.tckn"), Truth("TCKN", 1.0)),
                Column("FirstName", "nvarchar(50)", given_name("cust.fn"), Truth("GIVEN_NAME", 1.0)),
                Column("LastName", "nvarchar(50)", surname("cust.ln"), Truth("SURNAME", 1.0)),
                Column("BirthDate", "date", lambda g: f"DATEADD(day, {g.below('cust.bd', 25000)}, '1940-01-01')",
                       Truth("BIRTH_DATE", 1.0)),
                Column("GsmNo", "varchar(20)", phone_any("cust.gsm"), Truth("PHONE", 1.0), null_rate=0.1),
                Column("Email", "varchar(100)", email("cust.mail"), Truth("EMAIL", 1.0), null_rate=0.3),
                Column("Address", "nvarchar(200)", address("cust.addr"), Truth("ADDRESS", 1.0)),
                Column("Segment", "varchar(10)", lambda g: g.choose("cust.seg", ["BIREYSEL", "KOBI", "TICARI",
                                                                                "PRIVATE"])),
                Column("CustomerNo", "char(10)", lambda g: "CONCAT('MN', RIGHT('00000000' + CAST(t.n AS varchar(8)), 8))",
                       Truth("CUSTOMER_NO", 1.0, requires_baseline=True)),
                Column("CreatedAt", "datetime2(0)",
                       lambda g: f"DATEADD(minute, {g.below('cust.ca', 5_000_000)}, '2012-01-01')"),
            ],
        ),
        Table(
            "bank", "Account", shape="clustered_composite",
            clustered=["BranchCode", "AccountNo"],
            rows={"small": 30_000, "large": 100_000_000},
            columns=[
                Column("BranchCode", "smallint", lambda g: "CAST(1 + (t.n - 1) * 500 / t.total AS smallint)",
                       nullable=False),
                Column("AccountNo", "bigint", lambda g: "t.n", nullable=False),
                Column("CustomerId", "int", lambda g: f"1 + {g.below('acc.cid', 1_000_000)}"),
                Column("Iban", "char(26)", lambda g: g.iban_tr("acc.iban", BANK_CODES), Truth("IBAN", 1.0)),
                Column("TaxNo", "char(10)", lambda g: g.vkn("acc.vkn"), Truth("VKN", 1.0), null_rate=0.7),
                Column("CurrencyCode", "char(3)", lambda g: g.choose("acc.cur", ["TRY", "USD", "EUR", "GBP"])),
                Column("Balance", "decimal(18,2)", lambda g: f"CAST({g.below('acc.bal', 100_000_000)} AS decimal(18,2)) / 100"),
            ],
        ),
        Table(
            "bank", "Card", shape="clustered_text",
            clustered=["CardRef"],
            rows={"small": 15_000, "large": 100_000},
            columns=[
                Column("CardRef", "varchar(20)", lambda g: "CONCAT('CR', RIGHT('0000000000' + CAST(t.n AS varchar(10)), 10))",
                       nullable=False),
                Column("CardNo", "varchar(19)", lambda g: g.luhn_pan("card.pan", PAN_BINS), Truth("PAN", 1.0)),
                Column("MaskedPan", "varchar(19)",
                       lambda g: f"CONCAT({g.choose('card.mb', [b[:4] for b in PAN_BINS])}, '-XXXX-XXXX-', {g.digits('card.ml', 4)})",
                       Truth("MASKED_PAN", 1.0)),
                Column("CustomerId", "int", lambda g: f"1 + {g.below('card.cid', 20_000)}"),
                Column("ExpiryYm", "char(4)", lambda g: f"CONCAT(26 + {g.below('card.ey', 6)}, RIGHT('0' + CAST(1 + {g.below('card.em', 12)} AS varchar(2)), 2))"),
            ],
        ),
        Table(
            "bank", "TxnLog", shape="partitioned_datetime",
            clustered=["TxnTime", "TxnId"], partition_by_month="TxnTime",
            rows={"small": 200_000, "large": 100_000_000},
            columns=[
                Column("TxnTime", "datetime2(0)",
                       lambda g: "DATEADD(second, CAST((t.n - 1) * 63072000.0 / t.total AS int), '2024-11-01')",
                       nullable=False),
                Column("TxnId", "bigint", lambda g: "t.n", nullable=False),
                Column("Amount", "decimal(18,2)", lambda g: f"CAST({g.below('txn.amt', 5_000_000)} AS decimal(18,2)) / 100"),
                Column("ChannelCode", "char(3)", lambda g: g.choose("txn.ch", ["ATM", "INT", "MOB", "SUB", "POS"])),
                Column("RefNo", "char(11)",
                       lambda g: f"CONCAT(CAST(1 + {g.below('txn.ref:f', 9)} AS varchar(1)), {g.digits('txn.ref', 10)})",
                       Truth(role="hard_negative")),
                Column("ProcessNo", "char(16)", lambda g: g.digits("txn.proc", 16), Truth(role="hard_negative")),
                Column("Description", "nvarchar(200)", lambda g: clean_sentence(g, "txn.desc")),
            ],
        ),
        Table(
            "ops", "CustomerNote", shape="clustered_bigint",
            clustered=["NoteId"],
            rows={"small": 20_000, "large": 100_000},
            columns=[
                Column("NoteId", "bigint", lambda g: "t.n", nullable=False),
                _ft("Note00", 0.0, "TCKN"),
                _ft("Note03", 0.03, "TCKN"),
                _ft("Note08", 0.08, "PHONE"),
                _ft("Note50", 0.50, "IBAN"),
                _ft("Note92", 0.92, "TCKN"),
                _ft("Note99", 0.99, "EMAIL"),
                _ft("LongNote", 0.50, "TCKN", sql_type="nvarchar(max)", pad_chars=5000),
            ],
        ),
        Table(
            "ops", "AccountEvent", shape="partitioned_nonaligned",
            clustered=["AccountId", "EventDate", "EventId"], partition_by_month="EventDate",
            rows={"small": 50_000, "large": 200_000},
            columns=[
                Column("AccountId", "int", lambda g: "CAST(1 + (t.n - 1) / 10 AS int)", nullable=False),
                Column("EventDate", "datetime2(0)",
                       lambda g: f"DATEADD(second, {g.below('evt.dt', 63072000)}, '2024-11-01')", nullable=False),
                Column("EventId", "bigint", lambda g: "t.n", nullable=False),
                Column("EventType", "varchar(20)", lambda g: g.choose("evt.type", ["LOGIN", "TRANSFER", "PAYMENT", "UPDATE"])),
                Column("Detail", "nvarchar(200)", lambda g: clean_sentence(g, "evt.det")),
            ],
        ),
        Table(
            "ops", "CrmLog", shape="clustered_bigint",
            clustered=["LogId"],
            rows={"small": 20_000, "large": 100_000},
            columns=[
                Column("LogId", "bigint", lambda g: "t.n", nullable=False),
                Column("Ref", "varchar(12)", customer_no("crm.ref"), Truth("CUSTOMER_NO", 1.0, requires_baseline=True)),
                Column("Message", "nvarchar(300)",
                       lambda g: f"CASE WHEN {g.flag('crm.msg:dirty', 0.3)} THEN CONCAT({clean_sentence(g, 'crm.s1')}, "
                                 f"N' müşteri no ', {customer_no('crm.msgv')(g)}, N' ile kayıt güncellendi.') "
                                 f"ELSE CONCAT({clean_sentence(g, 'crm.s1')}, N' ', {clean_sentence(g, 'crm.s2')}) END",
                       Truth("CUSTOMER_NO", 0.3, requires_baseline=True),
                       dirty=lambda g: g.flag("crm.msg:dirty", 0.3)),
                Column("Channel", "varchar(10)", lambda g: g.choose("crm.ch", ["CALL", "CHAT", "BRANCH", "MAIL"])),
            ],
        ),
        Table(
            "ops", "LegacyImport", shape="heap",
            clustered=[], primary_key=False,
            rows={"small": 10_000, "large": 50_000_000},
            columns=[
                Column("Aciklama", "varchar(50)", tckn("leg.acik"), Truth("TCKN", 1.0, role="misleading_name")),
                Column("TCKN", "char(11)", lambda g: "NULL", Truth(role="misleading_name"), null_rate=1.0),
                Column("Kod", "varchar(10)", lambda g: f"CONCAT('K-', RIGHT('0000' + CAST({g.below('leg.kod', 300)} AS varchar(4)), 4))"),
                Column("DekontNo", "char(11)",
                       lambda g: f"CONCAT(CAST(1 + {g.below('leg.dek:f', 9)} AS varchar(1)), {g.digits('leg.dek', 10)})",
                       Truth(role="hard_negative")),
                Column("YabanciKimlik", "char(11)", tckn("leg.ykn", prefix="99"), Truth("YKN", 1.0), null_rate=0.95),
                Column("Tutar", "decimal(18,2)", lambda g: f"CAST({g.below('leg.tut', 10_000_000)} AS decimal(18,2)) / 100"),
            ],
        ),
    ]


def expected_zone(col: Column, realised_rate: float | None, all_null: bool) -> str:
    if all_null:
        return "EMPTY"
    if col.truth.data_type is None or not realised_rate:
        return "CLEAN"
    if realised_rate >= 0.90:
        return "SENSITIVE"
    return "SPARSE"
