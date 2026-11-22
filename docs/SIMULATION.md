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

## Bilinen sınırlar

- **Kural üretimi gerçek LLM'le denenmedi.** Simülasyonda yerel LLM yerine sahte bir test sunucusu kullanıldı. Bu sunucu örneklerden biçim kuralı türetiyor ve bilerek gerçek bir değer içeren kural da öneriyor; sistem o kuralı her seferinde reddetti. Gerçek gateway'deki Qwen3'ün kural kalitesi ayrıca denenmeli.
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
