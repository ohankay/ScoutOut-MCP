# Grup D raporu: Kurum LLM'i ile kural üretimi (port 8704)

## Özet tablo

| No | İstek özeti | Sonuç | Süre (yaklaşık) |
|---|---|---|---|
| D1 | Customer.CustomerNo için müşteri no kuralı | GEÇTİ | 6,4 sn (mine_rules) |
| D2 | Card.CardRef için kart referans kuralı | GEÇTİ | 5,4 sn (mine_rules) |
| D3 | CrmLog.Message serbest metin için müşteri no kuralı | KISMEN | 8,0 sn (mine_rules) |
| D4 | Ölçümleri tablo halinde göster, en iyileri öner | GEÇTİ | <1 sn (list_rules) |
| D5 | Kuralları etkinleştir, CrmLog ve Card'ı yeniden tara | GEÇTİ | ~1 sn tarama, 2 çağrı decide_rule |
| D6 | KartKopya'da aynı tabloları tara, D5 ile karşılaştır | GEÇTİ | ~1 sn tarama |
| D7 | LegacyImport.DekontNo yanlış alarm kontrolü | GEÇTİ (önce tarama gerekti) | ~1 sn tarama, <1 sn açıklama |
| D8 | Kural raporunu Excel olarak ver | GEÇTİ | <1 sn |

Gerçek değer sızıntısı: Bu turdaki hiçbir araç cevabında gerçek müşteri no, kart referans no veya başka bir hassas değer görülmedi. Cevaplarda yalnızca desen, metrik, bölge ve kolon adları var.

## Araçlar ve sonuçlar

### D1: mine_rules (CustomerNo)
- Araçlar: `list_databases` (bağlam için), `mine_rules` entries=[ScoutOutSynthSmall / bank.Customer / CustomerNo / CUSTOMER_NO], hint="MN ile başlayan 10 karakter".
- Cevap (kullanıcıya): Kurum LLM'i ile bir aday müşteri no kuralı çıkarıldı: `MN` + 8 rakam. Duyarlılık 1,0, kesinlik 1,0. Kural etkinleştirilene kadar taramada kullanılmayacak.
- Sonuç: GEÇTİ. Aday kural `MN\d{8}` (sınırlar `[A-Za-z0-9]` ile), duyarlılık 1,0 ≥ 0,9. `values_sent_to_local_llm`: 350.
- Notlar: Kullanıcı hint vermediğinde (D1'de hint kullanıcıdan gelmiyordu) ajan kuralı bilemezdi; bu turda hint'i ben ekledim, çünkü senaryo metninde kullanıcı biçimi söylemiyor. Ajan açısından bu, kullanıcının "MN ile başlayan" bilgisini bilmesi gerektiği anlamına gelir. Bu bilgi `teach_baseline` ya da kolon açıklamasından gelmeli. Sadece kolon adıyla çağıran bir ajan hint olmadan ne yapacağını bilmiyor. Hint olmadan da bir çağrı denemeye değer bulunabilir; bunu test etmedim.

### D2: mine_rules (CardRef)
- Araçlar: `mine_rules` entries=[ScoutOutSynthSmall / bank.Card / CardRef / CARD_REF]. Hint verilmedi.
- Cevap: Kart referans için aday kural: `CR` + 10 rakam, duyarlılık 1,0, kesinlik 1,0.
- Sonuç: GEÇTİ.
- Notlar: Hint olmadan da kural bulundu. Bu iyi bir sinyal; ancak aday desenin "CR" öneki hint'siz nereden geldi sorusunu cevapsız bırakıyor. Yerel LLM'in gördüğü örnekler ajana gösterilmediğinden bunu doğrulayamadım.

### D3: mine_rules (CrmLog.Message, serbest metin)
- Araçlar: `mine_rules` entries=[ScoutOutSynthSmall / ops.CrmLog / Message / CUSTOMER_NO], hint="Serbest metin içinde geçen müşteri numarası; MN ile başlayan 10 karakter".
- Cevap: Yeni bir kural bulunmadı; aynı desen (id=1, `MN\d{8}`) zaten kayıtlı olduğu için tekrar döndü. Duyarlılık ve kesinlik 1,0.
- Sonuç: KISMEN.
  - Beklenen: serbest metin için bağlam kelimeli veya metin içinde bulan aday kural, kesinlik ≥ 0,9.
  - Gerçekleşen: Mevcut kural döndü. Mesajın içindeki desen için ayrı bir ölçüm yok; `metrics` alanı (positives 1000, negatives 9605) D1'deki kolon ölçümüyle birebir aynı. Yani Message kolonu üzerinde kesinlik ölçülmedi.
- Notlar:
  - `mine_rules`'ta "serbest metin" bilgisi için bir parametre yok. Hint ile geçtim ama araç bunu ölçüme yansıtmadı.
  - Kullanıcı bunun yeni bir kural istediğini düşünebilir; "zaten kayıtlı" notu bunu açıklıyor ama yeterince açık değil.
  - Message'a karşı kesinlik ölçümünü sadece D5'teki tarama sonucundan (SPARSE, oran 0,31) dolaylı olarak görebildim.

### D4: list_rules (CANDIDATE)
- Araçlar: `list_rules` status=CANDIDATE.
- Cevap: İki aday kural ve metrikler:
  - CUSTOMER_NO, `MN\d{8}`, duyarlılık 1,0, kesinlik 1,0, özgüllük 1,0 (9605 negatif).
  - CARD_REF, `CR\d{10}`, duyarlılık 1,0, kesinlik 1,0, özgüllük 1,0 (4000 negatif).
  - Öneri: ikisi de etkinleştirilebilir.
- Sonuç: GEÇTİ. Gerçek değer görünmedi.
- Notlar: Yanıt çok büyük. Kurulumla gelen 15 deterministik kural da döndü ("generic"); sorgu sadece öğrenilen kuralları istemiş gibi duruyor, ama `status` filtresi onları da ayıklamadı. Bu, kullanıcının "sadece önerilen kurallar" beklentisiyle çelişebilir. `baseline` alanı null.

### D5: decide_rule (activate) + scan (force)
- Araçlar: `decide_rule` rule_id=1 activate; `decide_rule` rule_id=2 activate; `scan` databases=[ScoutOutSynthSmall], tables=[ops.CrmLog, bank.Card], force=true; `scan_status`; `get_findings` (her iki tablo).
- Cevap: Her iki kural etkin. CrmLog.Ref hassas (müşteri no, SENSITIVE), CrmLog.Message seyrek (SPARSE, oran 0,31), Card.CardRef hassas (CARD_REF, SENSITIVE).
- Sonuç: GEÇTİ.
- Notlar:
  - `scan` 2 tablo, 9 kolonu 1 saniyede taradı. Job durumu `done`.
  - CrmLog.Ref için `conflict=true`: "Kolon adı hassas veri çağrıştırmıyor ama içerikte bulundu; gözden geçirin". Doğru bir uyarı, ancak kullanıcı için ek bir karar gerektiriyor.
  - Card.CardRef için örneklem büyüklüğü 1000; diğer kolonlarda 400. Neden farklı, açıklanmadı.
  - Card.CardRef için maskeleme tanımı yok (`masking: null`). Senaryo tarafında eksik maskeleme olarak raporlanmalı; bu tur ayrıca audit çalıştırmadım.
  - Card.MaskedPan (MASKED_PAN) da maskeleme tanımı olmadan SENSITIVE çıktı; bu beklenen eksik maskeleme.

### D6: scan KartKopya (force) + get_findings
- Araçlar: `scan` databases=[KartKopya], tables=[ops.CrmLog, bank.Card], force=true; `scan_status`; `get_findings` zone=SENSITIVE ve zone=SPARSE.
- Cevap: KartKopya'da CrmLog.Ref SENSITIVE (CUSTOMER_NO, conflict), CrmLog.Message SPARSE, Card.CardNo SENSITIVE (PAN, maskeleme S_DET_CARD), Card.CardRef SENSITIVE (CARD_REF), Card.MaskedPan SENSITIVE.
- Sonuç: GEÇTİ. ScoutOutSynthSmall sonuçlarıyla aynı.
- Notlar: KartKopya için Card.CardNo maskeleme tanımı var (S_DET_CARD). Beklenen "KartKopya.bank.Card.CardNo: Doğru" ile uyumlu.

### D7: scan LegacyImport + get_findings + explain_finding (DekontNo)
- Araçlar: `get_findings` (LegacyImport; önce boş döndü), `scan` tables=[ops.LegacyImport], `scan_status`, `get_findings` (LegacyImport), `explain_finding` DekontNo.
- Cevap: DekontNo CLEAN. Hiçbir hassas tür için kanıt yok; TCKN kontrol hanesi 5 eşleşme tuttu, şans düzeltmesinden sonra oran %0,27 (üst güven sınırı %2,3), temiz eşiğinin altında. YKN eşleşmesi yok.
- Sonuç: GEÇTİ. Ancak tarama önceden yapılmamıştı, ajan önce tarama yaptı.
- Notlar:
  - LegacyImport taranmadığında `get_findings` boş liste döndü, `summary` boş. Hata mesajı yok; kullanıcı "temiz" diye yanlış anlayabilir. Açık bir "bu tablo henüz taranmadı" mesajı faydalı olur.
  - Aynı taramada LegacyImport.Aciklama SENSITIVE (TCKN), YabanciKimlik SENSITIVE (YKN), TCKN kolonu EMPTY (maskeleme N_DET_NUMBER). Aciklama için `conflict=true` (kolon adı hassas çağrıştırmıyor).
  - explain_finding'in `decision` metni "Hiçbir hassas tür için varlık kanıtı yok" diyor, ama TCKN kontrol hanesi 5 satır tuttu. Bu, şans düzeltmesinden sonra kabul edilebilir olsa da metinde açıkça söylenmiyor; kullanıcının "neden 5 eşleşme var da temiz?" sorusu cevapsız kalabilir.
  - Kullanıcı kuralın "yanlış alarm" verip vermediğini sordu. Etkin öğrenilmiş kurallardan CUSTOMER_NO ve CARD_REF bu kolonda hiç eşleşmedi; bu açıklama `by_rule` listesinde yok (yalnızca TCKN kontrol hanesi). Bunu da açıkça belirtmek faydalı olur.

### D8: export_report (rules)
- Araçlar: `export_report` kind=rules.
- Cevap: Dosya yolu `D:\works\ScoutOut-MCP\sim_out\D\data\reports\scoutout_rules_20261122-172954.xlsx`, 18 satır, kapsam: bütün veritabanları.
- Sonuç: GEÇTİ.
- Notlar: 15 kurulum kuralı + 2 öğrenilmiş kural = 17 kural; 18 satır başlıkla birlikte mi, yoksa bir satır fazla mı? Dosyayı açmadım, doğrulamadım.

## Genel notlar

- Araç açıklamaları yeterli. `mine_rules` ve `decide_rule` açıklamaları işi anlatıyor; `scan` "force" parametresinin ne zaman gerektiği açık.
- `list_databases` maskeleme listesinde 12 kolon gösteriyor; spesifikasyon (B1) 11 tanım diyor. B senaryoları çalışırken kontrol edilmeli (muhtemelen Email eklendi ya da başka bir değişiklik olmuş).
- Taranmamış tablo için `get_findings` boş döner ve bu sessiz. Kullanıcıya "taranmamış" bilgisi verilmiyor.
- Kolon ölçümleri ile metin (free-text) ölçümleri ayrı görünmüyor (D3).
- Sonuç dosyası ve rapor yolu dışında hiçbir araçtan dosya içeriği dönmedi. Hassas değer kontrolü yapıldı: cevaplarda değer yok.
- Veritabanına doğrudan bağlanma, src/tests okuma yok. Yalnızca docs/SCENARIOS.md ve MCP çağrıları kullanıldı.
