"""Transparency: a readable catalog of every detection rule, the decision logic and what was learned."""

from __future__ import annotations

import json
from pathlib import Path

from scoutout.store import Store

#: plain-language description of each built-in detector: (name, kind of check, how it works)
GENERIC = {
    "tckn": ("T.C. kimlik no", "Kontrol hanesi",
             "11 hane, ilk hane 0 değil. 10. hane = ((1.+3.+5.+7.+9. hane) × 7 − (2.+4.+6.+8. hane)) mod 10; "
             "11. hane = ilk 10 hanenin toplamı mod 10. 99 ile başlayanlar YKN sayılır."),
    "ykn": ("Yabancı kimlik no", "Kontrol hanesi", "99 ile başlayan ve T.C. kimlik no algoritmasına uyan 11 hane."),
    "vkn": ("Vergi no", "Kontrol hanesi", "10 hane; Gelir İdaresi'nin vergi kimlik numarası kontrol hanesi algoritması."),
    "pan": ("Kart no", "Kontrol hanesi",
            "13–19 hane; Visa, Mastercard, Troy, Amex, Discover, JCB, UnionPay veya Maestro numara aralığında olmalı "
            "ve Luhn kontrolünü geçmeli."),
    "iban": ("IBAN", "Kontrol hanesi",
             "Ülke kodu + 2 kontrol hanesi; uzunluk ülkeye göre (TR: 26). ISO 13616 mod-97 kontrolü. Boşluklu yazım da tanınır."),
    "phone": ("Telefon", "Biçim",
              "Türkiye cep (5xx) ve sabit (2xx–4xx) numaraları; +90, 0090 veya 0 önekli, boşluk/tire/nokta ayraçlı ya da "
              "bitişik. Diğer ülkeler için + ile başlayan uluslararası biçim."),
    "email": ("E-posta", "Biçim", "ad@alan.uzantı biçimi."),
    "masked_pan": ("Maskeli kart no", "Biçim", "1234-XXXX-XXXX-5678 veya 123456******1234 gibi kısmen maskeli kart numarası."),
    "passport": ("Pasaport no", "Biçim + bağlam",
                 "Türk pasaportu biçimi (U, Z veya S + 8 rakam). Hücrenin tamamıysa ya da yakınında 'pasaport' geçiyorsa sayılır."),
    "plate": ("Plaka", "Biçim + bağlam",
              "İl kodu (01–81) + 1–3 harf + 2–4 rakam. Hücrenin tamamıysa ya da yakınında 'plaka/araç' geçiyorsa sayılır."),
    "swift": ("SWIFT/BIC", "Biçim + bağlam",
              "4 harf banka + 2 harf ülke + 2 konum (+3 şube). Hücrenin tamamıysa ya da yakınında 'swift/bic' geçiyorsa sayılır."),
    "ip": ("IP adresi", "Biçim", "Geçerli IPv4 veya IPv6 adresi (0.0.0.0 ve 127.x hariç)."),
    "mac": ("MAC adresi", "Biçim", "AA:BB:CC:DD:EE:FF biçimi."),
    "address_tr": ("Adres", "Sözlük + kalıp",
                   "Mahalle/cadde/sokak/bulvar kelimelerinden biri, yanında kapı no, daire/kat/blok veya il-ilçe adı olmalı."),
    "name_tr": ("Ad / soyad", "Sözlük",
                "Hücre tek kelimeyse ad ve soyad sözlüğünde aranır. Metin içinde büyük harfle başlayan bir ad ile soyadın "
                "yan yana geçmesi aranır. Sözlük yaygın Türkçe ad ve soyadlardan oluşur; kurumun kendi listesi eklenebilir."),
    "birth_date": ("Doğum tarihi", "Kolon bağlamı",
                   "Sadece tarih tipindeki ve adında doğum/birth/dob geçen kolonlarda; tarih değeri tek başına kişisel veri sayılmaz."),
}
SOURCE_TR = {"generic": "Kurulumla gelen", "base-derived": "Tabandan türetilen", "llm-derived": "Dil modeli önerisi"}


def rule_label(det_id: str) -> str:
    """Short human label of a detector/rule id as it appears in a column's evidence."""
    if det_id in GENERIC:
        name, kind, _ = GENERIC[det_id]
        return f"{kind} ({name})"
    if det_id == "refset" or det_id.startswith("refset_"):
        return "Taban değer eşleşmesi"
    if det_id.startswith("rule"):
        return f"Onaylı kural #{det_id[4:]}"
    return det_id


def catalog(store: Store, cfg) -> dict:
    from dataclasses import asdict

    import numpy as np

    from scoutout.decision_engine.engine import Thresholds
    from scoutout.detectors import generic_detectors
    from scoutout.detectors.normalize import dictionaries
    from scoutout.knowledge import load_active_pack
    from scoutout.metadata_detector import KEYWORDS

    th = store.get_setting("thresholds") or asdict(Thresholds())
    generic = []
    seen = set()
    for d in generic_detectors():
        m = d.meta
        if m.id in seen:
            continue
        seen.add(m.id)
        name, kind, how = GENERIC.get(m.id, (m.data_type, "", ""))
        if m.chance is not None:
            error = (f"Rastgele bir sayı bu kontrolü yaklaşık %{m.chance * 100:.0f} olasılıkla tesadüfen geçer; bu tesadüfi "
                     "eşleşmeler her kolonda ayrıca hesaplanıp sonuçtan düşülür.")
        else:
            error = (f"Varsayılan hata payı: gerçek değerlerin %{m.se * 100:.0f}'ini bulur, hassas olmayan değerlerin "
                     f"%{(1 - m.sp) * 100:.1f}'ini yanlışlıkla işaretleyebilir. Taban ölçümü varsa ölçülen değer kullanılır.")
        generic.append({"id": m.id, "name": name, "kind": kind, "how": how, "error": error, "version": m.version})
    d = dictionaries()

    pack = load_active_pack(cfg, store)
    learned = None
    if pack is not None:
        refsets = {}
        if pack.ref_dir is not None:
            for f in sorted(pack.ref_dir.glob("*.npy")):
                refsets["Ad + soyad çifti" if f.stem == "NAME_PAIR" else f.stem] = int(len(np.load(f, mmap_mode="r")))
        manifest = pack.pack_dir / "manifest.json"
        learned = {
            "version": pack.version,
            "refsets": refsets,
            "naming": {t: sorted(w) for t, w in pack.learned_names.items()},
            "overrides": pack.overrides,
            "classifier": bool(pack.classifier),
            "stats": json.loads(manifest.read_text(encoding="utf-8")).get("stats", {}) if manifest.exists() else {},
        }
    rules = [dict(r) | {"body": json.loads(r["body_json"]), "metrics": json.loads(r["metrics_json"] or "{}"),
                        "source_tr": SOURCE_TR.get(r["source"], r["source"])}
             for r in store.q("SELECT * FROM rules ORDER BY CASE status WHEN 'ACTIVE' THEN 0 WHEN 'CANDIDATE' THEN 1 ELSE 2 END, id")]
    return {
        "thresholds": th, "looks": cfg.scan.looks, "generic": generic,
        "dictionary": {"given": len(d["given"]), "surname": len(d["surname"]), "place": len(d["place"])},
        "learned": learned, "rules": rules, "keywords": KEYWORDS,
    }


DECISION_TEXT = [
    "1. Her tablodan tekrarlanabilir bir örneklem alınır (büyük tablolar baştan sona okunmaz).",
    "2. Her satırda kurallar çalışır; bir veri tipi için en az bir kural bulursa satır o tip için 'isabet' sayılır.",
    "3. İsabet oranı kuralların hata payına göre düzeltilir (Rogan–Gladen); kontrol haneli tiplerde tesadüfi eşleşmeler düşülür.",
    "4. Düzeltilmiş oranın güven aralığı hesaplanır (Wilson).",
    "5. Bölge: alt sınır hassas eşiğinin üstündeyse HASSAS; üst sınır temiz eşiğinin altındaysa ve hiç hassas veri kanıtı "
    "yoksa TEMİZ; hassas veri kanıtlı ama eşiğin altındaysa SEYREK; aksi hâlde örneklem büyütülür, en sonda BELİRSİZ.",
    "6. Kolon adı, öğrenilen sınıflandırıcı ve benzerlik sadece önceliklendirme ve çelişki uyarısı içindir; bölgeyi belirlemez.",
    "7. Son karar gözden geçirenindir: raporlara sadece onaylanmış kararlar girer.",
]


def export_catalog(store: Store, cfg, path: Path) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    c = catalog(store, cfg)
    th = c["thresholds"]
    wb = Workbook()
    ws = wb.active
    ws.title = "Karar mantığı"
    for line in DECISION_TEXT:
        ws.append([line])
    ws.append([])
    ws.append(["Eşik", "Değer"])
    ws.append(["Hassas: oranın alt sınırı en az", th["p_sensitive"]])
    ws.append(["Hassas: güven düzeyi", th["conf_sensitive"]])
    ws.append(["Temiz: oranın üst sınırı en fazla", th["p_clean"]])
    ws.append(["Temiz: güven düzeyi", th["conf_clean"]])
    ws.append(["Örneklem adımları (satır)", ", ".join(map(str, c["looks"]))])
    ws.column_dimensions["A"].width = 110

    g = wb.create_sheet("Kurulumla gelen kurallar")
    g.append(["Kod", "Veri tipi", "Doğrulama", "Nasıl çalışır", "Hata payı", "Sürüm"])
    for r in c["generic"]:
        g.append([r["id"], r["name"], r["kind"], r["how"], r["error"], r["version"]])
    g.append([])
    g.append(["Sözlükler", f"{c['dictionary']['given']} ad, {c['dictionary']['surname']} soyad, {c['dictionary']['place']} il/ilçe"])

    r_ = wb.create_sheet("Öğrenilen kurallar")
    r_.append(["No", "Veri tipi", "Kaynak", "Durum", "Desen", "Bağlam kelimeleri", "Duyarlılık", "Kesinlik", "Karar veren",
               "Tarih", "Gerekçe"])
    for r in c["rules"]:
        m = r["metrics"]
        r_.append([r["id"], r["data_type"], r["source_tr"], r["status"], r["body"].get("pattern"),
                   ", ".join(r["body"].get("context", [])), m.get("sensitivity"), m.get("precision"), r["decided_by"],
                   r["decided_at"], r["reason"]])
    if c["learned"]:
        r_.append([])
        r_.append([f"Etkin bilgi paketi: {c['learned']['version']}"])
        for t, n in c["learned"]["refsets"].items():
            r_.append(["Referans kümesi", t, f"{n:,} değer özeti (değerlerin kendisi saklanmaz)"])

    k = wb.create_sheet("Kolon adı sinyalleri")
    k.append(["Veri tipi", "Anahtar kelimeler (sadece önceliklendirme ve çelişki uyarısı için)"])
    for t, words in c["keywords"].items():
        k.append([t, ", ".join(words)])
    if c["learned"]:
        for t, words in c["learned"]["naming"].items():
            k.append([t, "Kurumdan öğrenilen: " + ", ".join(words)])
    for sheet in (g, r_, k):
        for cell in sheet[1]:
            cell.font = Font(bold=True)
        for col in "ABCDEFGHIJK":
            sheet.column_dimensions[col].width = 18
    g.column_dimensions["D"].width = 90
    g.column_dimensions["E"].width = 70
    r_.column_dimensions["E"].width = 50
    k.column_dimensions["B"].width = 120
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path
