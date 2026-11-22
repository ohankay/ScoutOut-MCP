"""ScoutOut-Mcp: the MCP tools the TDM agent uses. Transport: stdio (started by VS Code).

Every tool answer goes through ``guard``. Nothing here returns cell values.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from .guard import tool_result

INSTRUCTIONS = """ScoutOut: bankacılık veritabanlarında hassas veri keşfi ve maskeleme denetimi.
Bu sunucu gerçek değerleri asla döndürmez; sadece meta veri, kararlar, oranlar ve biçim istatistikleri verir.
Tipik akış: list_databases → list_tables → scan → scan_status → get_findings → explain_finding.
Maskeleme denetimi: audit_masking (listeyi kendisi günceller). Kural üretme: teach_baseline → mine_rules → decide_rule."""

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)


class Column(BaseModel):
    database: str = Field(description="Veritabanı adı (canlıdaki adıyla)")
    table: str = Field(description="şema.tablo (ör. bank.Customer) veya tek anlamlıysa sadece tablo adı")
    column: str = Field(description="Kolon adı")


class TypedColumn(Column):
    type: str = Field(description="Hassas veri türü, büyük harf: TCKN, VKN, IBAN, PAN, PHONE, EMAIL, ADDRESS, GIVEN_NAME, "
                                  "SURNAME, PERSON_NAME, BIRTH_DATE, CUSTOMER_NO, ACCOUNT_NO veya kurumun kendi türü")


class Rule(BaseModel):
    data_type: str = Field(description="Kuralın bulduğu tür (büyük harf)")
    pattern: str = Field(description="Değerin biçimini tarif eden düzenli ifade; en fazla 4 ardışık sabit karakter")
    context: list[str] = Field(default_factory=list, description="Yakında geçmesi gereken kelimeler (isteğe bağlı)")
    checksum: Literal["tckn", "vkn", "luhn", "iban"] | None = Field(default=None, description="Kontrol hanesi algoritması")
    column_names: list[str] = Field(default_factory=list, description="Bu türü gösteren kolon adı parçaları")
    ignore_case: bool = False


def build_server(service: Callable) -> MCPServer:
    """``service`` returns the (lazily created) Service; tests pass their own."""
    mcp = MCPServer("scoutout", instructions=INSTRUCTIONS)

    # --- discovery -------------------------------------------------------------------------------
    @mcp.tool(annotations=READ)
    @tool_result
    def list_databases() -> dict:
        """Sunucudaki veritabanları: durum, son restore zamanı, son tarama ve hassas kolon sayıları. Maskeleme listesi sürümü."""
        return service().list_databases()

    @mcp.tool(annotations=READ)
    @tool_result
    def list_tables(database: str, filter: str = "", only: Literal["", "never_scanned", "sensitive", "masked"] = "",
                    limit: int = 200) -> dict:
        """Bir veritabanının tabloları: satır, kolon, son tarama, hassas/seyrek kolon ve maskeleme listesindeki kolon sayısı.
        filter: ad parçası veya joker (ör. 'bank.*'). only: sadece hiç taranmamış / hassas veri bulunan / maskelenen tablolar."""
        return service().list_tables(database, filter, only, limit)

    @mcp.tool(annotations=READ)
    @tool_result
    def describe_table(database: str, table: str) -> dict:
        """Tablonun kolonları: tip, maskeleme tanımı (algoritma, iş terimi) ve son tarama sonucu."""
        return service().describe_table(database, table)

    # --- scanning --------------------------------------------------------------------------------
    @mcp.tool(annotations=WRITE)
    @tool_result
    def scan(databases: list[str] | None = None, tables: list[str] | None = None, force: bool = False) -> dict:
        """Deterministik kurallarla hassas veri taraması başlatır (arka planda). databases boşsa hepsi; tables verilirse
        sadece o tablolar (şema.tablo). Değişmemiş tablolar önceki sonuçla taşınır; force=True hepsini yeniden tarar.
        Hemen job_id döner; ilerleme için scan_status."""
        return service().scan(databases, tables, force)

    @mcp.tool(annotations=READ)
    @tool_result
    def scan_status(job_id: str = "") -> dict:
        """Tarama işinin ilerlemesi (job_id boşsa son iş): tablo sayıları, taşınan, hatalı, kalan süre."""
        return service().scan_status(job_id or None)

    @mcp.tool(annotations=READ)
    @tool_result
    def get_findings(database: str = "", table: str = "",
                     zone: Literal["", "SENSITIVE", "SPARSE", "UNDECIDED", "CLEAN", "EMPTY", "CONFLICT"] = "",
                     sensitive_type: str = "", limit: int = 200) -> dict:
        """Kolon bazında son sonuçlar: bölge (SENSITIVE hassas, SPARSE seyrek hassas, CLEAN temiz, EMPTY boş, UNDECIDED belirsiz),
        tür, düzeltilmiş oran ve güven aralığı, çelişki, maskeleme tanımı. En kritikler önce."""
        return service().get_findings(database or None, table or None, zone or None, sensitive_type or None, limit)

    @mcp.tool(annotations=READ)
    @tool_result
    def explain_finding(database: str, table: str, column: str) -> dict:
        """Bir kolonun sonucunun gerekçesi: hangi kural kaç satırda tuttu, şans düzeltmesi, kolon adı sinyali, örneklem,
        biçim profili. Değer içermez."""
        return service().explain_finding(database, table, column)

    @mcp.tool(annotations=READ)
    @tool_result
    def profile_column(database: str, table: str, column: str) -> dict:
        """Kolon değerlerinin biçim istatistiği (değer değil): uzunluk dağılımı, karakter sınıfı maskeleri (d/X/x), sık önekler,
        kontrol hanesi geçiş oranları. Az görülen biçimler gizlenir. Kural yazmadan önce kullanın."""
        return service().profile_column(database, table, column)

    # --- masking -------------------------------------------------------------------------------
    @mcp.tool(annotations=WRITE)
    @tool_result
    def masking_sync() -> dict:
        """Maskeleme tanımlarını ana sistemden (BoaCatalog.Dtg.TableColumnDataMaskDetail) yeniden okur; değişiklik varsa yeni sürüm."""
        return service().masking_sync()

    @mcp.tool(annotations=WRITE)
    @tool_result
    def audit_masking(databases: list[str] | None = None, tables: list[str] | None = None, scan_missing: bool = False) -> dict:
        """Maskeleme tanımlarını tarama sonuçlarıyla karşılaştırır: gereksiz maskeleme (listede, yüksek güvenle temiz/boş),
        eksik maskeleme (hassas ama listede yok), algoritma uyumsuzluğu, belirsizler ve listede olup taranmamış tablolar.
        scan_missing=True taranmamış tabloları taramaya başlar."""
        return service().audit_masking(databases, tables, scan_missing)

    @mcp.tool(annotations=WRITE)
    @tool_result
    def review_finding(database: str, table: str, column: str, decision: Literal["approve", "override", "reject"], reason: str,
                       final_zone: str = "", final_type: str = "") -> dict:
        """Kullanıcının bir kolon için verdiği kararı kaydeder. Sadece kullanıcı sohbette açıkça karar verdiğinde çağırın."""
        return service().review_finding(database, table, column, decision, reason, final_zone or None, final_type or None)

    @mcp.tool(annotations=WRITE)
    @tool_result
    def export_report(kind: Literal["findings", "masking_audit", "rules"] = "findings", database: str = "") -> dict:
        """Excel raporu üretir ve yerel dosya yolunu döner (içerik dönmez)."""
        return service().export_report(kind, database or None)

    # --- rules ---------------------------------------------------------------------------------
    @mcp.tool(annotations=READ)
    @tool_result
    def list_rules(data_type: str = "", status: Literal["", "ACTIVE", "CANDIDATE", "REJECTED", "RETIRED"] = "") -> dict:
        """Kural kataloğu: kurulumla gelen deterministik kurallar ve öğrenilen kurallar (taban, yerel LLM, ajan) ölçümleriyle."""
        return service().list_rules(data_type or None, status or None)

    @mcp.tool(annotations=WRITE)
    @tool_result
    def teach_baseline(entries: list[TypedColumn], negatives: list[Column] | None = None) -> dict:
        """Kurumun bildiği hassas kolonları (taban) öğretir: sunucu bu kolonlardan kendi içinde referans setleri, kolon adı
        desenleri ve biçim kuralı adayları üretir. negatives: hassas olmadığı bilinen benzer kolonlar."""
        return service().teach_baseline([e.model_dump() for e in entries], [n.model_dump() for n in negatives or []])

    @mcp.tool(annotations=WRITE)
    @tool_result
    def mine_rules(entries: list[TypedColumn], hint: str = "", max_rules: int = 8) -> dict:
        """Yerel LLM ile yeni deterministik kural adayları çıkarır: sunucu bu kolonlardan örnek değerleri SADECE yerel LLM'e
        gösterir, önerileri doğrular ve ölçer. Size kural tanımları ve ölçümler döner. hint: verinin sözlü tarifi."""
        return service().mine_rules([e.model_dump() for e in entries], hint, max_rules)

    @mcp.tool(annotations=WRITE)
    @tool_result
    def propose_rule(rule: Rule, positives: list[Column], negatives: list[Column] | None = None) -> dict:
        """Kendi yazdığınız bir kuralı ölçtürür: positives kolonlarında bulmalı, negatives (boşsa aynı tabloların diğer
        kolonları) kolonlarında bulmamalı. Sonuç aday kural ve ölçümleri."""
        return service().propose_rule(rule.model_dump(), [p.model_dump() for p in positives],
                                      [n.model_dump() for n in negatives or []])

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False))
    @tool_result
    def decide_rule(rule_id: int, action: Literal["activate", "reject", "retire"], reason: str) -> dict:
        """Bir kuralı etkinleştirir, reddeder veya emekliye ayırır. Sadece kullanıcı açıkça istediğinde çağırın."""
        return service().decide_rule(rule_id, action, reason)

    return mcp


_service = None


def _get_service():
    global _service
    if _service is None:
        from .service import Service
        from .settings import load

        cfg, settings = load()
        _service = Service(cfg, settings)
    return _service


def main() -> None:
    from scoutout.core.log import setup_logging

    try:
        from .settings import load

        cfg, _ = load()
        setup_logging(cfg.paths.logs)
    except Exception as e:  # noqa: BLE001 - tools report the configuration error to the agent
        logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
        logging.getLogger("scoutout").warning("configuration: %s", e)
    build_server(_get_service).run("stdio")


if __name__ == "__main__":
    main()
