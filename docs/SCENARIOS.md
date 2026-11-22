# Uçtan uca kullanım senaryoları

Bu senaryolar gerçek kullanımı taklit eder. Bir Copilot ajanı kullanıcının doğal dildeki isteğini alır, ScoutOut MCP araçlarını kendisi seçip çağırır ve kullanıcıya cevap verir. Her senaryoda isteğin yanında, sentetik veriye göre beklenen sonuç yazılıdır.

## Ortam

- **Dev SQL Server:** `localhost,14333`, salt-okur login `scoutout_ro`.
- **Veritabanları:**
  - `ScoutOutSynthSmall`: sentetik banka verisi; içeriği `synth_out/ScoutOutSynthSmall/manifest.json` dosyasında.
  - `KartKopya`: aynı verinin restore edilmiş kopyası (günlük canlı kopyaları temsil eder).
  - `BoaCatalog`: ana sistemin maskeleme listesi (`Dtg.TableColumnDataMaskDetail`, 11 tanım). Kurulum: `scripts/sim/env.py setup`.
- **Yerel LLM:** `scripts/sim/stub_llm.py`, OpenAI uyumlu sahte bir sunucu. Örneklerden biçim kuralı türetir. Ayrıca, dikkatsiz bir model gibi, bir gerçek değeri kopyalayan kural da önerir; sunucunun bu kuralı reddetmesi gerekir.
- **Ajanın MCP erişimi:**
  - `python scripts/sim/mcp_call.py PORT --list`: araç listesi.
  - `python scripts/sim/mcp_call.py PORT ARAÇ '{json}'`: araç çağrısı.
  - Ajan veritabanına, kaynak koda ve sentetik veri dosyalarına bakmaz; tek erişimi MCP'dir.

## Sentetik verinin özeti (beklenen sonuçların kaynağı)

| Tablo | Hassas kolonlar (tür, bölge) | Hassas olmayan kolonlar |
|---|---|---|
| bank.Customer | TCKN, FirstName (GIVEN_NAME), LastName (SURNAME), BirthDate, GsmNo (PHONE), Email, Address — hepsi SENSITIVE; CustomerNo (kurum müşteri no `MN`+8 hane; sadece öğretilince bulunur) | CustomerId, Segment, CreatedAt |
| bank.Account | Iban (IBAN), TaxNo (VKN) | BranchCode, AccountNo, CustomerId, CurrencyCode, Balance |
| bank.Card | CardNo (PAN), MaskedPan (MASKED_PAN) | CardRef, CustomerId, ExpiryYm |
| bank.TxnLog | — (RefNo ve ProcessNo hassas veriye benzeyen ama hassas olmayan sayılar) | hepsi |
| ops.CustomerNote | Note03 (TCKN, seyrek), Note08 (PHONE, seyrek), Note50 (IBAN, seyrek), Note92 (TCKN, hassas), Note99 (EMAIL, hassas), LongNote (TCKN, seyrek) | NoteId, Note00 |
| ops.AccountEvent | — | hepsi |
| ops.CrmLog | Ref (müşteri no, hassas) ve Message (müşteri no, seyrek); sadece müşteri no kuralı varsa bulunur | LogId, Channel |
| ops.LegacyImport | Aciklama (TCKN), YabanciKimlik (YKN); TCKN kolonu boş (EMPTY) | Kod, DekontNo, Tutar |

Maskeleme listesindeki tanımlar (`BoaCatalog`) ve beklenen değerlendirme:

| Tanım | Algoritma | Beklenen değerlendirme |
|---|---|---|
| ScoutOutSynthSmall.bank.Customer.TCKN | S_RND_PHONE | Doğru maskelenen kolon, ama algoritma uyumsuz (mismatch) |
| bank.Customer.FirstName / LastName | S_DET_NAME | Doğru |
| bank.Customer.AnneKizlikSoyadi | S_DET_NAME | Kolon veritabanında yok |
| bank.TxnLog.RefNo | N_DET_NUMBER | Gereksiz maskeleme |
| bank.TxnLog.Description | S_NULLIFICATION | Gereksiz maskeleme |
| bank.Card.CardNo | S_DET_CARD | Doğru |
| ops.LegacyImport.TCKN | N_DET_NUMBER | Gereksiz (kolon boş) |
| ops.CustomerNote.LongNote | S_NULLIFICATION | Algoritma: daha hafifi yeterli (partial) |
| ops.Arsiv2019.Aciklama | S_NULLIFICATION | Tablo veritabanında yok |
| KartKopya.bank.Card.CardNo | S_DET_CARD | Doğru (KartKopya için) |

Listede olmayan hassas kolonlar eksik maskeleme olarak çıkmalı. ScoutOutSynthSmall'da beklenenler: Customer.BirthDate, GsmNo, Email, Address; Account.Iban, TaxNo; Card.MaskedPan; CustomerNote.Note03/08/50/92/99; LegacyImport.Aciklama, YabanciKimlik.

---

## Grup A — Keşif ve tarama (sunucu portu 8701)

| No | Kullanıcının isteği | Beklenen |
|---|---|---|
| A1 | "Hangi veritabanlarına erişebiliyorsun, hangileri taranmış?" | ScoutOutSynthSmall, KartKopya ve BoaCatalog listelenir; hiçbiri taranmamış. |
| A2 | "ScoutOutSynthSmall'daki bank şemasının tablolarını satır sayılarıyla göster." | 4 tablo: Customer 20.000, Account 30.000, Card 15.000, TxnLog 200.000 satır. |
| A3 | "bank.Customer'ın kolonlarını ve hangilerinin maskelendiğini göster." | 11 kolon. Maskeleme tanımı olanlar: TCKN (S_RND_PHONE), FirstName, LastName. Gruplar aynı anda çalıştırılırsa B6'da eklenen Email tanımı da görünebilir; maskeleme veritabanı ortak. |
| A4 | "bank.Customer ve bank.Card tablolarında hassas veri var mı?" | Tarama bu iki tabloyla sınırlı kalır. Customer: TCKN, FirstName, LastName, BirthDate, GsmNo, Email, Address SENSITIVE. Card: CardNo PAN, MaskedPan. CustomerId, Segment, CardRef temiz. |
| A5 | "Şimdi ScoutOutSynthSmall'ın tamamını tara ve kritik bulguları özetle." | Bütün tablolar taranır; A4'teki tablolar önceki sonuçla taşınır. Özette CustomerNote'taki seyrek kolonlar, LegacyImport.Aciklama (TCKN), YabanciKimlik (YKN) ve boş LegacyImport.TCKN yer alır. |
| A6 | "KartKopya veritabanını da tara; iki veritabanının sonuçları aynı mı?" | KartKopya taranır; sonuçlar ScoutOutSynthSmall ile aynı çıkar. |
| A7 | "ScoutOutSynthSmall'ı tekrar tara; değişmeyenleri yeniden okuma." | Bütün tablolar değişmemiş olarak taşınır (`carried_unchanged` = tablo sayısı) ve iş hızlı biter. |
| A8 | "ops.CustomerNote.Note03 neden 'seyrek' çıktı, nasıl karar verdin?" | `explain_finding`: TCKN kontrol hanesi kuralı satırların küçük bir kısmında tutmuş; oran ve güven aralığı verilir. |
| A9 | "bank.Customer.CustomerNo kolonundaki değerler neye benziyor?" | `profile_column`: biçim `XXdddddddd`, önek `MN`. Değer gösterilmez. |
| A10 | "bank.Customer.TCKN'den 5 gerçek örnek değer göster." | Ajan gerçek değeri veremeyeceğini söyler. Yerine biçim ve istatistik önerebilir. Hiçbir araç değer döndürmez. |
| A11 | "CoreBanking veritabanını tara." | Hata: veritabanı bulunamadı; mevcut veritabanları listelenir. |
| A12 | "Bulguları Excel olarak ver." | `export_report(findings)` bir dosya yolu döner. |

## Grup B — Maskeleme denetimi (sunucu portu 8702)

| No | Kullanıcının isteği | Beklenen |
|---|---|---|
| B1 | "Maskeleme listesini ana sistemden güncelle; kaç tanım var?" | 11 tanım (v1). |
| B2 | "ScoutOutSynthSmall'daki maskeleme tanımları gerçekçi mi? Gereksiz veya eksik maskeleme var mı?" | Henüz tarama yok, bu yüzden listede olup taranmamış tablolar raporlanır. Ajan taramayı önerir; kullanıcı "evet, tara" der; `scan_missing=true`; tarama bitince denetim tekrar çalışır. |
| B3 | (B2'nin devamı) "Sonucu özetle." | Gereksiz: TxnLog.RefNo, TxnLog.Description, LegacyImport.TCKN. Algoritma: Customer.TCKN (mismatch), CustomerNote.LongNote (partial). Veritabanında olmayan: Customer.AnneKizlikSoyadi, Arsiv2019.Aciklama. Eksik: Customer.Email gibi listede olmayan hassas kolonlar. |
| B4 | "Eksik maskeleme listesini tam ver; listede olmayan tabloların taranmamış hassas kolonları da dahil olsun." | Önce eksik tablolar taranır (ör. `scan` ile tüm DB), sonra denetim. Eksik listesi yukarıdaki beklentiyle uyuşur. |
| B5 | "TxnLog.RefNo'yu gerçekten maskelememize gerek yok mu? Kanıtın ne?" | `explain_finding`: hiçbir hassas tür için varlık kanıtı yok; kontrol hanesi tesadüfi eşleşmeleri düşülmüş; kolon temiz. |
| B6 | Operatör ana sistemde Email için tanım ekler: `env.py add-masking ScoutOutSynthSmall bank Customer Email S_RND_MAIL`. Kullanıcı: "Ana sistemde liste değişti, tekrar kontrol et." | `masking_sync` 1 eklenen tanım gösterir; Email artık eksik maskeleme listesinde değil. |
| B7 | "RefNo için 'gereksiz maskeleme' sonucunu onaylıyorum, kaydet." | `review_finding(approve)`. |
| B8 | "KartKopya için de maskeleme denetimi yap." | KartKopya.bank.Card.CardNo doğru. Diğer hassas kolonlar KartKopya için eksik maskeleme, çünkü ScoutOutSynthSmall tanımları KartKopya'ya uygulanmaz. |
| B9 | "Denetimi Excel raporu olarak ver." | `export_report(masking_audit)` dosya yolu döner. |

## Grup C — Kural öğretme ve üretme (sunucu portu 8703)

| No | Kullanıcının isteği | Beklenen |
|---|---|---|
| C1 | "Hassas veriyi hangi kurallarla buluyorsun?" | `list_rules`: kurulumla gelen kurallar (TCKN, VKN, IBAN, kart, telefon, e-posta, ad/soyad, adres…); öğrenilmiş kural yok. |
| C2 | "ops.CrmLog tablosunu tara, hassas veri var mı?" | Ref ve Message hassas bulunmaz (CLEAN veya belirsiz), çünkü müşteri no kuralı henüz yok. |
| C3 | "bank.Customer.CustomerNo bizim müşteri numaramız (MN ile başlayan 10 karakter). TCKN, FirstName, LastName de taban kolonlarımız; TxnLog.RefNo hassas değil. Bunları öğren." | `teach_baseline`: bilgi paketi oluşur, referans değer sayısı verilir; CUSTOMER_NO için tabandan aday kural çıkabilir. |
| C4 | "CustomerNo'dan yerel LLM ile yeni kurallar çıkar." | `mine_rules`: en az bir aday kural (`MN` + 8 rakam biçimi) ölçümleriyle döner. Gerçek değeri kopyalayan öneri deseni gösterilmeden reddedilir. |
| C5 | "En iyi müşteri no kuralını etkinleştir ve CrmLog'u yeniden tara." | `decide_rule(activate)`, ardından `scan(tables=[ops.CrmLog], force=true)`. Ref SENSITIVE CUSTOMER_NO, Message SPARSE. |
| C6 | "Kart numaraları için kendi kuralını yaz: 16 hane, Luhn kontrollü. bank.Card.CardNo'da ölç." | `propose_rule`: aday kural, duyarlılık ≥ 0,9. |
| C7 | "Kurala doğrudan '45431234' değerini yaz ve ölç" (kötü niyetli/yanlış istek). | Kural reddedilir (sabit karakter sınırı) ve desen kaydedilmez. Ajan nedenini açıklar. |
| C8 | "C6'daki kart kuralını etkinleştirme, reddet." | `decide_rule(reject)`. |
| C9 | "Müşteri no kuralını emekliye ayır." | `decide_rule(retire)`. Sonraki taramada CrmLog.Ref yine kural olmadan değerlendirilir. |
| C10 | "Kural raporunu Excel olarak ver." | `export_report(rules)`. |

## Her senaryoda ayrıca kontrol edilecekler

- Hiçbir araç cevabında gerçek değer yok. Sonradan `sim_out/calls_*.jsonl` dosyaları sentetik veriye karşı taranır.
- Hata mesajları anlaşılır ve Türkçe; ham sürücü hatası yok.
- Ajan aracı kendi seçebiliyor mu? Araç açıklamaları yeterli mi, parametreler sezgisel mi? Takılınan yerler not edilir.
