# Gerçek kullanım simülasyonu

[SCENARIOS.md](SCENARIOS.md) dosyasındaki 31 senaryo, bir Copilot ajanı gibi davranan Claude Haiku alt ajanlarıyla çalıştırıldı.

**Simülasyonun kuralları:**
- Ajanlar yalnızca MCP araçlarını kullandı: araçları listeledi, kendisi seçti ve çağırdı.
- Veritabanına, kaynak koda ve sentetik veri dosyalarına erişmedi.
- Her grup kendi MCP sunucusunu (yerel HTTP) ve kendi verisini kullandı; üç grup aynı anda çalıştı.

## Sonuç

| Tur | A: Keşif ve tarama | B: Maskeleme denetimi | C: Kural öğretme ve üretme | Sızıntı kontrolü |
|---|---|---|---|---|
| 1 | 11 geçti, 1 kısmen | 9/9 | 10/10 | 37.751 gerçek değer arandı; ajan cevaplarında, store'da, logda ve raporlarda hiçbiri yok |
| 2 (düzeltmelerden sonra) | **12/12** | **9/9** | **10/10** | Aynı kontrol: hiçbiri yok |

Sızıntı kontrolü `scripts/sim/leak_check.py` ile yapıldı. Ajanların yazdığı raporlar da tarandı; gerçek değer içermiyorlar.

## Grup D — kurum LLM'i olarak bir ajan

Kurum LLM'inin (Qwen3, vLLM gateway) yerine ikinci bir Haiku ajanı kullanıldı. MCP sunucusu kural çıkarma isteğini gerçekte gateway'e nasıl gönderecekse aynen öyle gönderdi: aynı model adı, `temperature: 0` ve `enable_thinking: false`. `scripts/sim/relay_llm.py` isteği "LLM ajanı"na iletti. LLM ajanı gerçek örnek değerleri görüp kural önerdi; Copilot rolündeki ajan bu değerleri hiç görmedi.

| Senaryo | Sonuç |
|---|---|
| D1 Müşteri no kuralı (CustomerNo) | Geçti: `MN` + 8 rakam; duyarlılık 1,0, kesinlik 1,0 |
| D2 Kart referans kuralı (CardRef) | Geçti: `CR` + 10 rakam |
| D3 Serbest metinde müşteri no (CrmLog.Message) | 1. denemede kısmen geçti (kopya kontrolü hatası, aşağıda). Düzeltmeden sonra geçti: bağlam kelimeli ayrı kural; kesinlik 1,0, metinlerin %32'sinde bulundu |
| D4 Ölçüm tablosu | Geçti |
| D5 Etkinleştir ve yeniden tara | Geçti: CrmLog.Ref hassas (müşteri no), Message seyrek, Card.CardRef hassas (kart referansı) |
| D6 Aynı kurallar KartKopya'da | Geçti: sonuçlar aynı |
| D7 Benzer görünen DekontNo yanlış alarm veriyor mu? | Geçti: temiz; tesadüfi kontrol hanesi eşleşmeleri düşülmüş |
| D8 Kural raporu | Geçti |

LLM ajanı, serbest metin isteğinde yalnızca biçimin yetmediğini kendisi fark etti. Negatif örneklerdeki bir referans kolonu aynı biçimdeydi; bu yüzden kurala bağlam kelimesi ekledi ("müşteri"). Ajanın aldığı hiçbir cevapta ve store, log ve raporların hiçbirinde gerçek değer yok.

## Simülasyonun bulduğu ve düzeltilenler

| Bulgu | Düzeltme |
|---|---|
| Maskeleme listesi hiç okunmadıysa `describe_table` ve `list_databases` maskeleme bilgisini boş gösteriyordu | Liste ilk ihtiyaçta kendiliğinden okunuyor |
| Listede olup veritabanında olmayan kolon ya da tablo, sürekli "taranmamış" görünüyordu | Ayrı bir başlık eklendi: `not_in_database` |
| Taranmamış tablo varken denetim özeti "sorun yok" gibi okunabiliyordu | `complete=false` alanı ve açıklayıcı mesaj eklendi |
| Bir not kolonunda hem ad hem TCKN varken tür olarak yalnızca ad görünüyordu | Bütün türler `types_found` alanında listeleniyor; ana tür olarak kimlik numarası öncelikli |
| Çok yavaş çalışan bir desen ölçümü dakikalarca kilitleyebiliyordu | Ölçüme süre sınırı eklendi (20 sn); aşan kural "çok yavaş" gerekçesiyle reddediliyor |
| `checksum` küçük-büyük harfe duyarlıydı; araç açıklamalarında örnek yoktu | Büyük harf de kabul ediliyor; `teach_baseline`, `mine_rules` ve `propose_rule` açıklamalarına örnek ve tür listesi eklendi |
| Bir mesajda tırnaklı eylem adı maskelenmiş görünüyordu | Mesaj tırnaksız yazıldı |
| Kural emekliye ayrılınca taban değerleriyle yapılan eşleşmenin sürdüğü belli değildi | `decide_rule` açıklamasına not eklendi |
| Kural raporunda kurulumla gelen kurallar yoktu | Raporda ayrı bir sayfa olarak eklendi |
| Birkaç küçük tutarsızlık: rapor yolu, kalan süre, tekrarlanan önekler, çelişki metni | Düzeltildi |
| Serbest metin kolonundan kural çıkarılamıyordu: ölçüm değerlerin %90'ında eşleşme bekliyordu | Serbest metinde duyarlılık şartı yok; kesinlik ≥ 0,9 ve en az k eşleşme yeterli. LLM'e kolonun serbest metin olduğu bildiriliyor |
| Bağlam kelimeli bir kural, aynı desenli kuralın kopyası sayılıyordu | Kopya kontrolü desen, bağlam ve kontrol hanesinin hepsine bakıyor |
| Taranmamış bir tablo sorulunca boş liste dönüyordu | "Henüz taranmadı" mesajı dönüyor |

## Bilinen sınırlar

- **Kurum LLM'inin kendisi denenmedi.** A–C gruplarında sahte bir test sunucusu kullanıldı; bu sunucu bilerek gerçek değer içeren bir kural da öneriyor ve sistem o kuralı her seferinde reddetti. D grubunda LLM rolünü bir Haiku ajanı üstlendi. Gerçek gateway'deki Qwen3'ün kural kalitesi ayrıca denenmeli.
- **VS Code / Copilot istemcisinde denenmedi.** Simülasyon MCP protokolünü gerçek bir istemciyle kullandı, ama VS Code arayüzünde değil.
- **Bilinçli tasarım tercihleri:** `scan_missing` sadece maskeleme listesindeki tabloları tarar; listede olmayan tablolarda eksik maskelemeyi görmek için önce `scan` gerekir. Sadece adı çelişkili olan kolonlar da çelişki olarak işaretlenir.

## Yeniden çalıştırma

1. Ortamı kurun:
   ```powershell
   .venv\Scripts\python scripts\sim\env.py setup
   ```
2. Her grup için yapılandırma yazın (A, B ve C için ayrı ayrı):
   ```powershell
   .venv\Scripts\python scripts\sim\env.py config A
   ```
3. Sahte LLM'i başlatın:
   ```powershell
   .venv\Scripts\python scripts\sim\stub_llm.py 11500
   ```
4. Her grup için bir MCP sunucusunu yerel HTTP üzerinde başlatın (A → 8701, B → 8702, C → 8703):
   ```powershell
   $env:SCOUTOUT_CONFIG = "sim_out\A\config.yaml"; .venv\Scripts\python -m scoutout_mcp.server --http 8701
   ```
5. Ajan araçları şöyle çağırır:
   ```powershell
   .venv\Scripts\python scripts\sim\mcp_call.py 8701 list_tables '{"database": "ScoutOutSynthSmall"}'
   ```
6. Bitince sızıntı kontrolünü çalıştırın:
   ```powershell
   .venv\Scripts\python scripts\sim\leak_check.py
   ```

Ajanların raporları [simulation/](simulation/) klasöründe (2. tur).
