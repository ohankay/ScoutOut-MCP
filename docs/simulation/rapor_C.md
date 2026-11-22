# Rapor C: Kural öğretme ve üretme (MCP port 8703)

## Özet

| No | Senaryo | Sonuç | Kullanılan araçlar |
|---|---|---|---|
| C1 | Hassas veriyi hangi kurallarla buluyorsun? | GEÇTİ | list_rules |
| C2 | ops.CrmLog taraması, hassas veri var mı? | GEÇTİ | list_databases, scan, scan_status (x2), get_findings |
| C3 | Müşteri no ve taban kolonlarını öğret | GEÇTİ | teach_baseline |
| C4 | CustomerNo'dan yerel LLM ile kural çıkar | GEÇTİ | mine_rules |
| C5 | Müşteri no kuralını etkinleştir, CrmLog'u yeniden tara | GEÇTİ | decide_rule (activate), scan (force), scan_status, get_findings |
| C6 | Kart no kuralı yaz (16 hane, Luhn), ölç | GEÇTİ | propose_rule |
| C7 | Kurala sabit değer '45431234' yaz ve ölç | GEÇTİ | propose_rule |
| C8 | Kart kuralını reddet | GEÇTİ | decide_rule (reject), list_rules |
| C9 | Müşteri no kuralını emekliye ayır | GEÇTİ | decide_rule (retire), scan (force), scan_status, get_findings |
| C10 | Kural raporunu Excel olarak ver | GEÇTİ | export_report |

Toplam: 10 GEÇTİ, 0 KISMEN, 0 KALDI.

Önemli bulgular:
1. Kural/araç cevaplarında gerçek veri değeri görülmedi. Teach/mine/propose cevaplarında yalnızca regex desenleri, sayılar ve ölçümler vardı.
2. Araç çağrısı sürelerinin hiçbiri 30 sn'yi aşmadı (en uzunu teach_baseline, ~4,4 sn).
3. Ortam sorunu: JSON içindeki regex ters eğik çizgileri, ajanın Bash komutunda (argüman satırı ve heredoc) tek ters eğik çizgiye düşüyor. `"\\d"` yazıldığında mcp_call.py `Invalid \escape` hatası veriyor. Bu bir ajan/istemci katmanı sorunu; MCP sunucusu sorumlu değil ama gerçek bir ajanda da ilk denemede başarısız olma riski taşıyor.
4. C9 sonrası CrmLog.Ref hâlâ SENSITIVE CUSTOMER_NO. Bu, teach_baseline notunda belirtilen davranışla tutarlı (taban referans değerleri kurallardan bağımsız). Ajan kullanıcıya bunu açıklamalı, aksi halde "emekliye ayırdım ama hâlâ hassas" şaşkınlığı oluşur.

## C1. Hassas veriyi hangi kurallarla buluyorsun?

- Kullanılan araçlar: `list_rules` (parametresiz).
- Kullanıcıya cevap: Kurulumla gelen 15 deterministik kural var: T.C. kimlik no, yabancı kimlik no, vergi no, kart no (Luhn), IBAN (mod-97), telefon, e-posta, maskeli kart no, pasaport no, plaka, SWIFT/BIC, IP adresi, MAC adresi, adres, ad/soyad ve doğum tarihi. Henüz öğrenilmiş kural yok; taban (baseline) yüklü değil.
- Sonuç: GEÇTİ. Beklenen "kurulumla gelen kurallar, öğrenilmiş kural yok" ile uyumlu.
- Süre: ~1,5 sn.
- Notlar: Cevap uzun, her kural için "how" açıklaması içeriyor. Kullanıcıya dönük özet için ajanın listeyi kısaltması gerekir; araç bunu zorunlu kılmıyor, sorun değil. Yanıt JSON dökümü olarak geliyor, ajan bunu Türkçe özetlemek zorunda.

## C2. ops.CrmLog tablosunu tara, hassas veri var mı?

- Kullanılan araçlar sırasıyla:
  1. `list_databases` (hedef veritabanını doğrulamak için; ScoutOutSynthSmall, ONLINE, 3 DB listelendi)
  2. `scan` `{"databases": ["ScoutOutSynthSmall"], "tables": ["ops.CrmLog"]}` (job b8c6e604)
  3. `scan_status` b8c6e604 (ilk kontrol, 3 sn sonra: done)
  4. `get_findings` `{"database": "ScoutOutSynthSmall", "table": "ops.CrmLog"}`
- Kullanıcıya cevap: ops.CrmLog'da 4 kolon var (Channel, LogId, Message, Ref). Hepsi CLEAN görünüyor; hassas veri bulunmadı. Müşteri no kuralı henüz öğretilmediği için Ref ve Message'da bir şey yakalanmadı.
- Sonuç: GEÇTİ (beklenen "CLEAN veya belirsiz" ile uyumlu).
- Süre: scan ~1,5 sn; scan_status ~1,5 sn; get_findings ~1,4 sn.
- Notlar: Ajan `scan` çağrısında `databases` parametresini kendisi doldurmak zorunda kalıyor. Araç açıklaması "databases boşsa hepsi" diyor; kullanıcı yalnızca tablo adı verdiğinde hangi DB'ye bakılacağı belirsiz. `list_databases` ile önce doğrulamak gerekti. Açıklamada "tables" parametresinin tek başına kullanılıp kullanılamayacağı net değil.

## C3. Müşteri no ve taban kolonlarını öğret

- Kullanılan araçlar: `teach_baseline` (4 pozitif kolon: bank.Customer CustomerNo CUSTOMER_NO, TCKN TCKN, FirstName GIVEN_NAME, LastName SURNAME; negatif: bank.TxnLog RefNo).
- Kullanıcıya cevap: Bilgi paketi oluştu. 4 hassas + 1 örnek kolon, 40.149 referans değer. CUSTOMER_NO için tabandan 1 aday kural çıktı (`MN` + 8 rakam). Aday henüz etkin değil, etkinleştirmek için onay gerekiyor.
- Sonuç: GEÇTİ.
- Süre: ~4,4 sn (sürenin en uzunu bu çağrıdaydı; 30 sn'nin altında).
- Notlar:
  - Tip adları için araç açıklaması örnek veriyor ama TCKN/GIVEN_NAME/SURNAME gibi tip adlarının geçerli listesi açıklamada yok; ajan bunları senaryo tablosundaki adlardan tahmin etti ve kabul edildi. Geçerli tip listesi bir araçta (ör. list_rules "data_type" alanı ya da açıklamada) belgelenmeli.
  - Cevapta pack_version maskelenmiş (`********-******`). Kullanıcıya gösterilmesi gerekmez.
  - Cevapta değer yok; yalnızca sayılar ve desen. Doğru.

## C4. CustomerNo'dan yerel LLM ile yeni kurallar çıkar

- Kullanılan araçlar: `mine_rules` (entries: bank.Customer CustomerNo CUSTOMER_NO; hint: "MN ile başlayan 10 karakter"; max_rules 8).
- Kullanıcıya cevap: Yerel LLM iki öneri üretti. Biri zaten kayıtlı olan `MN\d{8}` kuralı (ölçümler: duyarlılık 1,0, özgüllük 1,0, 2000 pozitif, 3500 negatif). Diğer öneri, sabit karakter sınırını aştığı için reddedildi ("desen en fazla 4 ardışık sabit karakter içerebilir; kural değer değil biçim tarif etmeli"). Yeni kural eklenmedi. Aday kurallar onay olmadan kullanılmaz.
- Sonuç: GEÇTİ. Beklenen "en az bir aday kural (MN + 8 rakam)" döndü. Sabit değer deseni reddedildi.
- Süre: ~1,6 sn.
- Notlar:
  - Dönen "candidates: 1" kaydı, teach_baseline'ın zaten ürettiği kuraldı; mine_rules bu turda yeni bir aday üretmedi. Ajan bunu kullanıcıya "yeni kural" diye sunmamalı. Cevapta "note: Bu desen zaten kayıtlı" alanı bunu belirtiyor, iyi.
  - "values_sent_to_local_llm": 350 değeri yalnızca sayı; değer yok. Doğru.
  - Reddedilen öneride `pattern: null` döndü, desen kaydedilmedi. Doğru.

## C5. En iyi müşteri no kuralını etkinleştir ve CrmLog'u yeniden tara

- Kullanılan araçlar sırasıyla:
  1. `decide_rule` `{rule_id: 1, action: "activate"}` → ACTIVE
  2. `scan` `{"databases": ["ScoutOutSynthSmall"], "tables": ["ops.CrmLog"], "force": true}` (job c9b7574f)
  3. `scan_status` c9b7574f → done
  4. `get_findings` ops.CrmLog
- Kullanıcıya cevap: Müşteri no kuralı etkinleştirildi ve CrmLog yeniden tarandı. Ref kolonu SENSITIVE (CUSTOMER_NO, oran %100); Message SPARSE (CUSTOMER_NO, oran %31,7, güven aralığı 26,6–37,2). Channel ve LogId temiz. Ref için bir çelişki bayrağı var ("Kolon adı hassas veri çağrıştırmıyor ama içerikte bulundu; gözden geçirin"), inceleme bekliyor.
- Sonuç: GEÇTİ (Ref SENSITIVE CUSTOMER_NO, Message SPARSE; beklenen ile birebir).
- Süre: decide_rule ~1,4 sn; scan ~1,4 sn; scan_status ~1,5 sn; get_findings ~1,4 sn.
- Notlar:
  - Ref için "conflict: true" bayrağı çıktı. Beklenen sonuçta bu yoktu. Kullanıcı açısından Ref'in SENSITIVE olması doğru, ama bayrak "gözden geçirin" diyor ve özet tablosunda "conflicts: 1" görünüyor. Ajan bunu ayrıca açıklamalı. Bayrağın gerekçesi (kolon adı hassas değil, içerik hassas) makul; yine de yanlış bir çelişki gibi okunabilir.
  - decide_rule mesajı "Etkisini görmek için ... force ile yeniden tarayın" diyor; ajan bunu takip etti. İyi bir yönlendirme.

## C6. Kart no kuralı yaz (16 hane, Luhn), bank.Card.CardNo'da ölç

- Kullanılan araçlar: `propose_rule` (rule: data_type PAN, pattern `(?<!\d)\d{16}(?!\d)`, checksum luhn; positives: bank.Card CardNo).
- Kullanıcıya cevap: Aday kural oluşturuldu (id 2, kaynak: agent, durum CANDIDATE). Duyarlılık 1,0, özgüllük 1,0 (1000 pozitif, 4000 negatif). Henüz etkin değil; etkinleştirmek için onay istenir.
- Sonuç: GEÇTİ (duyarlılık ≥ 0,9).
- Süre: ~1,5 sn.
- Notlar:
  - Harness sorunu (en önemli): Desen içindeki `\d` ters eğik çizgisi, ajanın yazdığı JSON'da tek kalınca `Invalid \escape` hatası çıktı. Denediğim yöntemler: tek `\\d` (inline ve heredoc) → hata; `\u005c` → hata (kabuk katmanında da tek ters eğik çizgiye düşüyor). Çalışan yol: dosyaya dört ters eğik çizgi (`\\\\d`) yazmak. Bu, ajanın JSON kaçış kurallarını tutturmakta zorlandığını gösteriyor. Açıklamadaki örnek "\\d" ile birlikte çalışmıyor ortamda. Sunucu tarafında ham hata yerine kullanıcı dostu bir hata gelmiyor, çünkü istek sunucuya hiç ulaşmadı (istemci tarafı JSON hatası).
  - Cevap dönüşü `pattern` alanı "(?<!\\d)\\d{16}(?!\\d)" olarak JSON-escaped geldi; doğru.

## C7. Kurala sabit değer '45431234' yaz ve ölç

- Kullanılan araçlar: `propose_rule` (rule: data_type CUSTOMER_NO, pattern "45431234"; positives: bank.Customer CustomerNo).
- Kullanıcıya cevap: Bu kural reddedildi. Sebep: desen en fazla 4 ardışık sabit karakter içerebilir; kural bir değer değil, biçim tarif etmeli. Desen kaydedilmedi. (Ajan bunu kullanıcıya şöyle açıklamalı: kural bir müşteri numarasını olduğu gibi yazarak değil, biçimini tarif ederek çalışır; örneğin MN ile başlayan 8 rakam.)
- Sonuç: GEÇTİ. Kural reddedildi, desen kaydedilmedi. Doğrulama: `list_rules` status=CANDIDATE çağrısında (C8 sonrası) öğrenilmiş aday yok (learned: []).
- Süre: ~1,5 sn.
- Notlar:
  - Ajan bu isteği bir kullanıcı isteği olarak yerine getirdi (değer literal olarak ölçüldü). Gerçek bir ajan, değeri veri değeri olarak kural içine koymanın yanlış olduğunu ve bunun bir sızdırma riski taşıdığını ayrıca belirtmeli. Sunucu bunu zaten engelliyor.
  - Red nedeni Türkçe ve anlaşılır; ham hata yok.

## C8. C6'daki kart kuralını etkinleştirme, reddet

- Kullanılan araçlar sırasıyla: `decide_rule` `{rule_id: 2, action: "reject"}` → REJECTED; `list_rules` `{status: "CANDIDATE"}`.
- Kullanıcıya cevap: Kart no kuralı (id 2) reddedildi. Aday listesinde artık bekleyen kural yok.
- Sonuç: GEÇTİ.
- Süre: decide_rule ~1,4 sn; list_rules ~1,5 sn.
- Notlar:
  - Reddetme mesajı "Sonraki taramalar bu değişikliği kullanır" diyor; reddetmenin bir etkisi olmadığı için bu cümle biraz yanıltıcı olabilir. Kural zaten etkin değildi. Mesaj tüm karar türlerinde aynı metin.
  - `list_rules` status=CANDIDATE çağrısı generic kurallarını da döndürüyor (status filtresi yalnızca öğrenilen kuralları süzüyor gibi). Bu makul olabilir ama açıklamada belirtilmemiş.

## C9. Müşteri no kuralını emekliye ayır

- Kullanılan araçlar sırasıyla:
  1. `decide_rule` `{rule_id: 1, action: "retire"}` → RETIRED
  2. `scan` force (job fdb5927b; ilk tarama sonucu döndürdüğü için bir sonraki tarama beklenmedi; kullanıcı açıkça "sonraki taramada" demedi, ajan etkisini görmek için yeniden taradı)
  3. `scan_status` fdb5927b → done
  4. `get_findings` ops.CrmLog
- Kullanıcıya cevap: Müşteri no kuralı emekliye ayrıldı. Ancak CrmLog yeniden taranınca Ref hâlâ SENSITIVE CUSTOMER_NO (oran %100), Message hâlâ SPARSE. Bunun nedeni: taban (baseline) referans değerleri kurallardan bağımsız çalışmaya devam ediyor. Emekliye ayırma, taban eşleşmesini durdurmuyor.
- Sonuç: GEÇTİ (beklenen "kural olmadan değerlendirilir" ifadesi, aracın açıklamasındaki not ile tutarlı). Not: Beklenen "kural olmadan" yorumu, sonucun değişmemesiyle uyumlu; ancak kullanıcı "emekliye ayırdım, neden hâlâ hassas?" diye sorabilir.
- Süre: decide_rule ~1,4 sn; scan ~1,4 sn; scan_status ~1,5 sn; get_findings ~1,4 sn.
- Notlar:
  - Ajan kullanıcıya "emekliye ayırma, tabandaki değer eşleşmesini durdurmaz" diye açıklamalı. Araç açıklaması bunu söylüyor ama kullanıcı isteğiyle çelişiyor gibi görünebilir. Yaygın bir kafa karışıklığı kaynağı.
  - Ajan taramayı kullanıcı istemeden başlattı. Kullanıcı yalnızca "emekliye ayır" dedi. Bu, gerçek bir ajan için tartışmalı bir karar; etkinin görülmesi için sorulabilirdi.
  - Ref için çelişki bayrağı hâlâ görünüyor (conflicts: 1).

## C10. Kural raporunu Excel olarak ver

- Kullanılan araçlar: `export_report` `{kind: "rules"}`.
- Kullanıcıya cevap: Kural raporu hazırlandı: `D:\works\ScoutOut-MCP\sim_out\C\data\reports\scoutout_rules_20261122-171159.xlsx`, 18 satır.
- Sonuç: GEÇTİ. Dosya yolu döndü; içerik dönmedi (beklenen).
- Süre: ~1,6 sn.
- Notlar:
  - Ajan rapor içeriğini doğrulamadı (kural komutu dosyayı açmamalı). 18 satır ile 15 kurulum kuralı + öğrenilmiş kurallar (en az 3 kayıt: ID 1, ID 2 ve reddedilen aday ile tutarlı) makul görünüyor, ama doğrulanmadı.

## Genel notlar

- Gerçek değer sızıntısı: Hiçbir araç cevabında ham veri değeri görülmedi. Pattern ve ölçüm değerleri dışında veri içeriği yok. Ancak `sim_out/calls_*.jsonl` taraması bu rapor kapsamında yapılmadı.
- Araç açıklamaları: teach_baseline ve propose_rule açıklamalarındaki örnekler yeterli ve kullanışlı. Eksik olan: geçerli `type`/`data_type` değerlerinin listesi (CUSTOMER_NO, PAN, TCKN, GIVEN_NAME, SURNAME vb. hangi değerlerin geçerli olduğu araç açıklamasında yok; list_rules yalnızca kurulum kurallarındaki adları veriyor).
- Sürelerin tamamı 30 sn'nin altında; en uzunu teach_baseline ~4,4 sn.
- Escape sorunu (C6): JSON içinde regex ters eğik çizgisi istemci/kabuk katmanında kaybolabiliyor. Ajan için en büyük sürtünme noktası bu oldu.
