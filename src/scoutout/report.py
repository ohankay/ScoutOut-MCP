"""Reports (spec §5.11). Only reviewed decisions are reported.

* inventory — the sensitive data inventory of every scanned database (versioned, with a diff to the previous one)
* masking   — test data management: which columns need masking (and how), which do not, and — when the current
              masking list is uploaded — which columns are masked unnecessarily or are missing from it.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path

from scoutout.store import Store, now

ZONE_TR = {"SENSITIVE": "Hassas", "SPARSE": "Seyrek hassas", "CLEAN": "Temiz", "EMPTY": "Boş", "UNDECIDED": "Belirsiz"}
TYPE_TR = {
    "TCKN": "T.C. kimlik no", "YKN": "Yabancı kimlik no", "VKN": "Vergi no", "IBAN": "IBAN", "PAN": "Kart no",
    "MASKED_PAN": "Maskeli kart no", "PHONE": "Telefon", "EMAIL": "E-posta", "ADDRESS": "Adres",
    "PERSON_NAME": "Ad soyad", "GIVEN_NAME": "Ad", "SURNAME": "Soyad", "BIRTH_DATE": "Doğum tarihi",
    "PASSPORT": "Pasaport no", "PLATE": "Plaka", "SWIFT": "SWIFT/BIC", "IP": "IP adresi", "MAC": "MAC adresi",
    "CUSTOMER_NO": "Müşteri no", "ACCOUNT_NO": "Hesap no", "INTERNAL_ID": "Kurum içi numara",
}
SENSITIVE = ("SENSITIVE", "SPARSE")

INVENTORY_COLUMNS = [
    ("source", "Veritabanı"), ("schema", "Şema"), ("table", "Tablo"), ("column", "Kolon"), ("sql_type", "Veri tipi"),
    ("zone", "Sonuç"), ("sensitive_type", "Hassas veri tipi"), ("review", "Karar"),
    ("reviewer", "Onaylayan"), ("reviewed_at", "Tarih"), ("reason", "Gerekçe"), ("evidence", "Kanıt özeti"),
    ("run_id", "Koşu"),
]

#: suggested test-data masking method per data type (identifiers: deterministic, format-preserving)
MASK_METHODS = {
    "TCKN": "Kontrol haneleri geçerli sentetik TCKN; tutarlı (aynı değer → aynı maske)",
    "YKN": "99 ile başlayan, kontrol haneleri geçerli sentetik YKN; tutarlı",
    "VKN": "Kontrol hanesi geçerli sentetik VKN; tutarlı",
    "IBAN": "Banka kodu korunarak kontrol haneleri geçerli sentetik IBAN; tutarlı",
    "PAN": "BIN (ilk 6) korunarak Luhn-geçerli sentetik kart no; tutarlı",
    "MASKED_PAN": "Zaten maskeli; son 4 hane de rastgeleleştirilebilir",
    "PHONE": "Operatör öneki korunarak rastgele numara",
    "EMAIL": "Sentetik e-posta (alan adı korunabilir)",
    "PERSON_NAME": "Sözlükten rastgele ad soyad (cinsiyet korunabilir)",
    "GIVEN_NAME": "Sözlükten rastgele ad",
    "SURNAME": "Sözlükten rastgele soyad",
    "ADDRESS": "Sentetik adres (il/ilçe korunabilir)",
    "BIRTH_DATE": "Tarih kaydırma (yaş grubu korunur)",
    "PASSPORT": "Biçim korunarak rastgele değer",
    "PLATE": "İl kodu korunarak rastgele plaka",
    "SWIFT": "Genelde maskeleme gerekmez (kurum kodu); gerekirse sabit test kodu",
    "IP": "Aynı alt ağda rastgele adres",
    "MAC": "Rastgele MAC",
    "CUSTOMER_NO": "Kurum biçiminde tutarlı eşleme (ilişkiler korunur)",
    "ACCOUNT_NO": "Kurum biçiminde tutarlı eşleme (ilişkiler korunur)",
    "INTERNAL_ID": "Kurum biçiminde tutarlı eşleme (ilişkiler korunur)",
}
FREE_TEXT_METHOD = "Metin içi maskeleme: sadece bulunan parçalar değiştirilir, metin korunur"


def approved_rows(store: Store, source: str | None = None) -> list[dict]:
    rows = []
    where, params = "r.status IN ('APPROVED', 'OVERRIDDEN')", []
    if source:
        where += " AND d.source = ?"
        params.append(source)
    for d in store.latest_decisions(where, *params):
        rec = json.loads(d["record_json"])
        ev = f"p={rec['p_corrected']:.3f} [{rec['ci_low']:.3f}–{rec['ci_high']:.3f}], n={rec['n']}"
        rows.append({
            "source": d["source"] or "", "schema": d["schema_name"], "table": d["table_name"], "column": d["column_name"],
            "sql_type": d["sql_type"], "zone": d["final_zone"], "sensitive_type": d["final_type"] or "",
            "review": ("Otomatik onay" if d["reviewer"] == "otomatik" else "Onaylandı") if d["review_status"] == "APPROVED"
            else "Düzeltildi",
            "reviewer": d["reviewer"], "reviewed_at": d["reviewed_at"], "reason": d["reason"], "evidence": ev,
            "run_id": d["run_id"], "system_zone": d["zone"], "rows_estimate": rec.get("row_count_estimate") or 0,
            "free_text": bool(rec.get("free_text")),
        })
    return rows


def _key(r: dict) -> str:
    return f"{r.get('source', '')}:{r['schema']}.{r['table']}.{r['column']}"


def diff(prev_rows: list[dict], rows: list[dict]) -> dict:
    before = {_key(r): r for r in prev_rows}
    after = {_key(r): r for r in rows}
    out = {"new_sensitive": [], "now_clean": [], "changed": [], "added": [], "removed": sorted(set(before) - set(after))}
    for k, r in after.items():
        b = before.get(k)
        if b is None:
            out["added"].append(k)
            if r["zone"] in SENSITIVE:
                out["new_sensitive"].append(k)
            continue
        if b["zone"] != r["zone"] or b["sensitive_type"] != r["sensitive_type"]:
            out["changed"].append({"column": k, "from": b["zone"], "to": r["zone"]})
            if r["zone"] in SENSITIVE and b["zone"] not in SENSITIVE:
                out["new_sensitive"].append(k)
            if r["zone"] == "CLEAN" and b["zone"] in SENSITIVE:
                out["now_clean"].append(k)
    return out


def _count(rows, key="zone"):
    out: dict[str, int] = {}
    for r in rows:
        out[r[key]] = out.get(r[key], 0) + 1
    return out


def _save_version(store: Store, kind: str, version: int, user: str, files: dict, summary: dict, rows: list) -> int:
    content = json.dumps(rows, sort_keys=True, ensure_ascii=False)
    with store.tx() as c:
        c.execute("INSERT INTO reports (version, created_at, created_by, files_json, summary_json, rows_json, content_hash, kind) "
                  "VALUES (?,?,?,?,?,?,?,?)", (version, now(), user, json.dumps(files), json.dumps(summary), content,
                                               hashlib.sha256(content.encode()).hexdigest(), kind))
    store.audit(user, f"report.{kind}", f"v{version}", **{k: v for k, v in summary.items() if k != "files"})
    return version


def _next_version(store: Store) -> int:
    return (store.one("SELECT COALESCE(MAX(version), 0) v FROM reports")["v"] or 0) + 1


# --- inventory --------------------------------------------------------------------------------------

def export(store: Store, out_dir: Path, user: str, formats=("xlsx", "csv", "json"), source: str | None = None) -> dict:
    rows = approved_rows(store, source)
    if not rows:
        raise ValueError("Raporlanacak onaylı karar yok. Önce adayları gözden geçirip onaylayın.")
    prev = store.one("SELECT rows_json FROM reports WHERE COALESCE(kind, 'inventory') = 'inventory' ORDER BY version DESC LIMIT 1")
    changes = diff(json.loads(prev["rows_json"]) if prev else [], rows)
    version = _next_version(store)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = out_dir / f"scoutout_envanter_v{version}"
    files = {}
    table = [[h for _, h in INVENTORY_COLUMNS]] + [
        [ZONE_TR.get(r[k], r[k]) if k == "zone" else r[k] for k, _ in INVENTORY_COLUMNS] for r in rows]
    if "csv" in formats:
        files["csv"] = str(_csv(table, stem.with_suffix(".csv")))
    if "json" in formats:
        files["json"] = str(_json({"kind": "inventory", "version": version, "created_at": now(), "rows": rows,
                                   "diff": changes}, stem.with_suffix(".json")))
    if "xlsx" in formats:
        changes_sheet = [[f"Rapor v{version} — önceki envantere göre"]]
        for title, key in (("Yeni hassas", "new_sensitive"), ("Temize çıkan", "now_clean"), ("Kaldırılan", "removed")):
            changes_sheet += [[], [f"{title} ({len(changes[key])})"]] + [[k] for k in changes[key]]
        files["xlsx"] = str(_xlsx([("Kolonlar", table, 5), ("Değişiklikler", changes_sheet, None)], stem.with_suffix(".xlsx")))
    summary = {"columns": len(rows), "by_zone": _count(rows), "diff": {k: len(v) for k, v in changes.items()}}
    _save_version(store, "inventory", version, user, files, summary, rows)
    return {"version": version, "files": files, "summary": summary, "diff": changes}


# --- test data masking report ---------------------------------------------------------------------------

def parse_masking_list(data: bytes, filename: str) -> list[tuple[str | None, str, str, str]]:
    """A masking list file as (database or None, schema, table, column). CSV/TXT or XLSX.

    Accepted layouts: columns named veritabani/database, sema/schema, tablo/table, kolon/column (any case/accents),
    or a single column with 'schema.table.column' or 'database.schema.table.column' per line.
    """
    from scoutout.masking import entries_from_rows

    if filename.lower().endswith(".xlsx"):
        from openpyxl import load_workbook

        ws = load_workbook(io.BytesIO(data), read_only=True).active
        lines = [list(row) for row in ws.iter_rows(values_only=True)]
    else:
        text = data.decode("utf-8-sig", errors="replace")
        dialect = csv.Sniffer().sniff(text[:2000], delimiters=";,\t") if any(d in text[:2000] for d in ";,\t") else csv.excel
        lines = [[c.strip() for c in row] for row in csv.reader(io.StringIO(text), dialect)]
    lines = [ln for ln in lines if any(v not in (None, "") for v in ln)]
    if not lines:
        return []
    return entries_from_rows([("" if v is None else str(v)) for v in lines[0]], lines[1:])


def masking_rows(store: Store, source: str | None = None) -> list[dict]:
    rows = []
    for r in approved_rows(store, source):
        mask = r["zone"] in SENSITIVE
        if not mask:
            method = ""
        elif r["zone"] == "SPARSE" or r["free_text"]:
            method = FREE_TEXT_METHOD
        else:
            method = MASK_METHODS.get(r["sensitive_type"], "Biçim korunarak rastgele değer")
        rows.append(r | {"mask": mask, "method": method})
    return rows


def compare(rows: list[dict], masking_list: list[tuple], sources: dict | None = None) -> dict:
    """Unnecessary / missing / correct masking against the current list.

    ``sources`` maps ScoutOut source names to real database names, so a list that names the real database matches.
    """
    from scoutout.masking import Index, algo_fit

    idx = Index(masking_list, sources or {})
    out = {"unnecessary": [], "correct": [], "not_reviewed": [], "missing": [], "review_algorithm": []}
    for r in rows:
        info = idx.lookup(r["source"], r["schema"], r["table"], r["column"])
        r["current_algorithm"], r["term"] = (info["algorithm"], info["term"]) if info else (None, None)
        if info:
            out["correct" if r["mask"] else "unnecessary"].append(r)
            fit = algo_fit(info["algorithm"], r["zone"], r["sensitive_type"])
            r["algorithm_note"] = fit[1] if fit else ""
            if fit:
                out["review_algorithm"].append(r)
        elif r["mask"]:
            out["missing"].append(r)
    seen = {(r["schema"].lower(), r["table"].lower(), r["column"].lower()) for r in rows}
    for e in masking_list:
        if (e[1].lower(), e[2].lower(), e[3].lower()) not in seen:
            out["not_reviewed"].append(".".join(x for x in e[:4] if x))
    return out


def masking_report(store: Store, out_dir: Path, user: str, masking_list: list[tuple] | None = None,
                   source: str | None = None, formats=("xlsx", "csv", "json"), use_stored: bool = True) -> dict:
    """``masking_list`` given -> compare with it; otherwise the stored (regularly synchronised) list is used if any."""
    from scoutout.masking import current, staleness
    from scoutout.sources import list_sources

    rows = masking_rows(store, source)
    if not rows:
        raise ValueError("Raporlanacak onaylı karar yok. Önce adayları gözden geçirip onaylayın.")
    list_info = "dosyadan yüklenen liste" if masking_list is not None else ""
    if masking_list is None and use_stored:
        row, entries = current(store)
        if row is not None:
            masking_list = entries
            list_info = f"saklı liste v{row['version']} ({row['origin']}, {row['entry_count']} kolon)"
            warn = staleness(store)
            if warn:
                list_info += " — UYARI: " + warn
    pending = len(store.latest_decisions("r.id IS NULL" + (" AND d.source = ?" if source else ""), *([source] if source else [])))
    cmp = compare(rows, masking_list, {x.name: x.names for x in list_sources(store)}) if masking_list is not None else None
    version = _next_version(store)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = out_dir / f"scoutout_maskeleme_v{version}"

    head = ["Veritabanı", "Şema", "Tablo", "Kolon", "Veri tipi", "Maskeleme", "Önerilen yöntem", "Sonuç", "Hassas veri tipi",
            "Tahmini satır", "Mevcut algoritma", "İş terimi", "Algoritma notu", "Onaylayan", "Gerekçe"]

    def line(r):
        return [r["source"], r["schema"], r["table"], r["column"], r["sql_type"], "Maskele" if r["mask"] else "Gerekmez",
                r["method"], ZONE_TR.get(r["zone"], r["zone"]), TYPE_TR.get(r["sensitive_type"], r["sensitive_type"]),
                r["rows_estimate"], r.get("current_algorithm") or "", r.get("term") or "", r.get("algorithm_note", ""),
                r["reviewer"], r["reason"]]

    plan = [head] + [line(r) for r in sorted(rows, key=lambda r: (not r["mask"], r["source"], r["schema"], r["table"], r["column"]))]
    n_mask = sum(r["mask"] for r in rows)
    summary = {"columns": len(rows), "mask": n_mask, "no_mask": len(rows) - n_mask, "pending_review": pending}
    from datetime import datetime

    from scoutout.core.config import tz

    overview = [["Test verisi maskeleme raporu", f"v{version}"], ["Oluşturma", datetime.now(tz()).strftime("%d.%m.%Y %H:%M")],
                ["Kapsam", source or "Bütün veritabanları"], [],
                ["Onaylı kolon", len(rows)], ["Maskelenmesi gereken", n_mask], ["Maskeleme gerekmeyen", len(rows) - n_mask],
                ["Henüz gözden geçirilmemiş kolon (raporda yok)", pending]]
    sheets = []
    if cmp is not None:
        wasted = sum(r["rows_estimate"] for r in cmp["unnecessary"])
        summary |= {"unnecessary": len(cmp["unnecessary"]), "missing": len(cmp["missing"]), "correct": len(cmp["correct"]),
                    "not_reviewed": len(cmp["not_reviewed"]), "unnecessary_rows": wasted,
                    "review_algorithm": len(cmp["review_algorithm"])}
        summary["list"] = list_info
        overview += [[], ["Mevcut maskeleme listesiyle karşılaştırma", list_info],
                     ["Gereksiz maskelenen kolon", len(cmp["unnecessary"])],
                     ["  bu kolonlardaki tahmini satır (gereksiz iş)", wasted],
                     ["Eksik (listede olmayan hassas) kolon", len(cmp["missing"])],
                     ["Doğru maskelenen kolon", len(cmp["correct"])],
                     ["  algoritması gözden geçirilmeli", len(cmp["review_algorithm"])],
                     ["Listede olup sonucu henüz onaylanmamış/bulunamayan", len(cmp["not_reviewed"])]]
        sheets += [("Gereksiz maskeleme", [head] + [line(r) for r in cmp["unnecessary"]], 5),
                   ("Eksik maskeleme", [head] + [line(r) for r in cmp["missing"]], 5),
                   ("Doğru", [head] + [line(r) for r in cmp["correct"]], 5),
                   ("Algoritma gözden geçir", [head] + [line(r) for r in cmp["review_algorithm"]], 5),
                   ("Listede onaysız", [["Kolon"]] + [[k] for k in cmp["not_reviewed"]], None)]
    files = {}
    if "xlsx" in formats:
        files["xlsx"] = str(_xlsx([("Özet", overview, None), ("Maskeleme planı", plan, 5)] + sheets, stem.with_suffix(".xlsx")))
    if "csv" in formats:
        files["csv"] = str(_csv(plan, stem.with_suffix(".csv")))
    if "json" in formats:
        doc = {"kind": "masking", "version": version, "created_at": now(), "summary": summary,
               "plan": [{k: r[k] for k in ("source", "schema", "table", "column", "sql_type", "mask", "method", "zone",
                                            "sensitive_type", "rows_estimate")} for r in rows]}
        if cmp is not None:
            doc["comparison"] = {k: ([_key(r) for r in v] if k != "not_reviewed" else v) for k, v in cmp.items()}
        files["json"] = str(_json(doc, stem.with_suffix(".json")))
    _save_version(store, "masking", version, user, files, summary, [{k: r[k] for k in ("source", "schema", "table", "column", "mask")}
                                                                      for r in rows])
    return {"version": version, "files": files, "summary": summary, "comparison": cmp}


# --- writers --------------------------------------------------------------------------------------------

def _csv(table: list[list], path: Path) -> Path:
    with path.open("w", newline="", encoding="utf-8-sig") as f:  # BOM: Excel opens Turkish characters correctly
        csv.writer(f, delimiter=";").writerows(table)
    return path


def _json(obj, path: Path) -> Path:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _xlsx(sheets: list[tuple[str, list[list], int | None]], path: Path) -> Path:
    """sheets: (title, rows, 0-based index of the column coloured by its value, or None for a plain sheet)."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    fills = {"Hassas": "F8D7DA", "Seyrek hassas": "FFF3CD", "Temiz": "D1E7DD", "Maskele": "F8D7DA", "Gerekmez": "D1E7DD"}
    wb = Workbook()
    wb.remove(wb.active)
    for title, rows, colour_col in sheets:
        ws = wb.create_sheet(title[:31])
        for r in rows:
            ws.append(r)
        if colour_col is not None and rows:
            for c in ws[1]:
                c.font = Font(bold=True, color="FFFFFF")
                c.fill = PatternFill("solid", fgColor="2F4858")
            for i in range(2, ws.max_row + 1):
                cell = ws.cell(i, colour_col + 1)
                if cell.value in fills:
                    cell.fill = PatternFill("solid", fgColor=fills[cell.value])
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = ws.dimensions
        widths = [min(60, max((len(str(r[i])) for r in rows if i < len(r)), default=8) + 2) for i in range(max(map(len, rows), default=0))]
        for i, w in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(i)].width = max(10, w)
        if colour_col is None and rows:
            ws["A1"].font = Font(bold=True)
    wb.save(path)
    return path
