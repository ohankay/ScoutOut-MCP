# ScoutOut-Mcp — Tasarım ve Spesifikasyon (taslak v0.1)

> ScoutOut'un tespit çekirdeğini, VS Code'daki bir Copilot özel ajanının ("TDM ajanı") kullanabileceği yerel bir **MCP sunucusuna** dönüştürür.
> Bu belge yeni projenin başlangıç spesifikasyonudur. ScoutOut reposu olduğu gibi kalır; yeni proje oradan kopyalanan çekirdekle başlar.

---

## 1. Amaç

TDM ajanı canlı DB kopyalarında hassas verileri bulur, maskeleme tanımlarının gerçekçiliğini denetler ve yeni deterministik kurallar üretir. Bunu yaparken **gerçek veriyi hiç görmez**.

Kullanıcı ajana doğal dille şunları söyleyebilmeli:

- "CoreBanking ve Cards DB'lerinde `bank.*` tablolarını tara, hassas veri var mı?"
- "Maskeleme listesini kontrol et: gereksiz maskeleme, eksik maskeleme, yanlış algoritma var mı?"
- "`Customer.FirstName` ad, `Customer.TaxNo` VKN, `Address.Line1` adres. Bunlardan elimizde olmayan yeni kurallar çıkar."
- "`ops.CrmLog.Note` neden hassas çıktı? Hangi kural ne kadar tuttu?"
- "Bu bulguların raporunu Excel'e ver."

## 2. Temel ilke: ajan veriyi görmez, sadece sonuç görür

| Ajan görebilir | Ajan **asla** göremez |
|---|---|
| DB / şema / tablo / kolon adları, veri tipleri, satır sayıları | Hücre değerleri, örnek satırlar, parçalar |
| Kararlar: bölge (Hassas/Seyrek/Temiz/Boş/Belirsiz/Çelişki), tür, güven aralığı | Eşleşen değerler, "ilk 10 örnek" gibi listeler |
| Kural bazında isabet sayıları ve oranları (k-anonim eşiğin üstündeyse) | Eşiği altındaki küçük sayımlar (bunlar "<k" görünür) |
| Değerlerin **şekil profili**: `99999999999`, `AA99 9999 …`, uzunluk dağılımı | Serbest SQL çalıştırma, ham SELECT sonucu |
| Maskeleme listesi satırları (tanımlar meta veridir) | DB bağlantı bilgisi, parola |

Bu ilke istemle (prompt) değil **sunucunun kendisiyle** uygulanır. Ajan ne isterse istesin, sunucu ham değer dönen bir yol sunmaz.

## 3. Mimari

```
VS Code (Copilot Chat)
  └─ TDM ajanı (.github/agents/tdm.agent.md) — sadece scoutout araçları açık
        │  MCP (stdio)
        ▼
ScoutOut-Mcp sunucusu (yerel Python süreci)
  ├─ tools/        ince MCP araçları (doğrulama + guard)
  ├─ guard         her çıktının geçtiği tek kapı: redact + şema + k-anonimlik
  ├─ core          ScoutOut'tan: connector, sampler, detectors, decision_engine,
  │                knowledge (HMAC referans setleri), rules (kural kapısı), masking
  ├─ miner         yerel LLM ile kural madenciliği (ScoutOut llm.py + prompts)
  │     │  HTTP, sadece izinli yerel adres (ör. Ollama localhost:11434)
  │     ▼
  │   Yerel LLM (Ollama) — sadece sunucu konuşur, ajan doğrudan erişemez
  └─ store         SQLite (kararlar, kurallar, maskeleme sürümleri, audit)
        │  ODBC, salt-okur login
        ▼
SQL Server (izole sunucu): canlı DB kopyaları + BoaCatalog (Dtg.TableColumnDataMaskDetail)
```

- **Taşıma:** stdio. Sunucuyu VS Code başlatır, ağ portu açılmaz.
- **Tek kullanıcı, yerel.** Web arayüzü, oturum açma, rol yönetimi ve zamanlayıcı yoktur; arayüz ajandır.
- **DB erişimi** ScoutOut'taki gibidir:
  - salt-okur kontrolü bağlantıda yapılır, yazma yetkisi görülürse bağlantı reddedilir;
  - sunucu düzeyinde CONNECT ANY DATABASE / SELECT ALL USER SECURABLES / VIEW SERVER STATE yetkileri kullanılır (restore sonrası da geçerli kalır);
  - örnekleme index-seek ile yapılır ve plan kontrolünden geçer.
- **Uzun işler** (tarama) arka planda iş (job) olarak çalışır. Araç hemen bir `job_id` döner, ajan durumu sorar.

### ScoutOut'tan ne alınır, ne bırakılır

| Alınır (neredeyse değişmeden) | Bırakılır |
|---|---|
| `connector/`, `sampler/`, `profiler/`, `detectors/`, `metadata_detector.py`, `decision_engine/` | `web/` (FastAPI, şablonlar, auth) |
| `knowledge.py` (HMAC referans setleri, parmak izleri, LR), `rules.py` (kural kapısı) | `scheduler.py`, kaynak yönetimi ekranları |
| `masking.py` (SQL sync, takma ad eşleme, algoritma uyumu), `report.py` (Excel) | |
| `llm.py` + `prompts/rule_miner_v1.md` (yerel LLM ile kural madenciliği) | |
| `runner.py` (artımlı tarama, restore farkındalığı), `store.py`, `core/redact.py` | `autoapprove.py` (onayı kullanıcı sohbette verir) |
| `synth/` ve sentetik testler | |

## 4. MCP araçları

Araçlar az ve iş odaklı tutulur. Her aracın çıktısı **guard**'dan geçer (bkz. §5).

### 4.1 Keşif (meta veri)

| Araç | Girdi | Çıktı |
|---|---|---|
| `list_databases` | — | DB adları, durum, son restore zamanı, son tarama |
| `list_tables` | `database`, `filter?` (şema/ad deseni), `only?` (`never_scanned` / `sensitive` / `masked`) | tablo, satır, kolon sayısı, son tarama, özet sonuç |
| `describe_table` | `database`, `table` | kolonlar: tip, null oranı, maskeleme listesindeki tanımı (varsa), son karar |

### 4.2 Tarama ve bulgular

| Araç | Girdi | Çıktı |
|---|---|---|
| `scan` | `database` veya `databases[]`, `tables?[]` (yoksa hepsi), `force?` | `job_id`, kapsam (kaç tablo / kolon), tahmini süre |
| `scan_status` | `job_id` | ilerleme, biten / taşınan / hatalı tablo sayısı |
| `get_findings` | `database?`, `table?`, `zone?`, `type?`, `limit?` | kolon başına bölge, tür, düzeltilmiş oran ve güven aralığı, örneklem büyüklüğü, çelişki bayrağı |
| `explain_finding` | `database`, `table`, `column` | kural/dedektör bazında isabet, şans düzeltmesi, ad sinyali ve içerik sinyali, kararın gerekçesi. **Değer yok.** |
| `profile_column` | `database`, `table`, `column` | şekil profili (§5.3): uzunluk dağılımı, karakter sınıfı maskeleri, sık önekler; hepsi k-anonim |

### 4.3 Maskeleme denetimi

| Araç | Girdi | Çıktı |
|---|---|---|
| `masking_sync` | — | `BoaCatalog.Dtg.TableColumnDataMaskDetail` tablosunu okur; sürüm, satır sayısı, önceki sürüme göre fark |
| `masking_list` | `database?`, `table?` | maskeleme tanımları: kolon, DataMaskType, tip |
| `audit_masking` | `databases?[]`, `tables?[]`, `scan_missing?` | üç liste: **gereksiz maskeleme** (listede var, yüksek güvenle Temiz/Boş), **eksik maskeleme** (Hassas/Seyrek, listede yok), **algoritma uyumsuzluğu** (ör. TCKN kolonunda isim maskesi). Taranmamış kolonlar `scan_missing` açıksa önce taranır. |

### 4.4 Kural yönetimi (deterministik)

| Araç | Girdi | Çıktı |
|---|---|---|
| `list_rules` | `type?`, `status?` | kural kataloğu: generic / base türevi / ajan önerisi; durum, ölçüm sonuçları |
| `teach_baseline` | `entries[]`: `{database, table, column, type}` | Sunucu bu kolonlardan **kendi içinde** HMAC referans seti, şekil parmak izi ve ad desenlerini üretir. Ajana sadece "N değerlik set, M parmak izi" gibi özet döner. |
| `mine_rules` | `entries[]` (baseline kolonları), `hint?` (kullanıcının serbest açıklaması, ör. "müşteri no 10 hane, 1 ile başlar"), `max_rules?` | Sunucu içindeki **yerel LLM**, baseline kolonlarının gerçek örnek değerlerinden ve profillerinden (§6.1) alternatif kural adayları üretir. Her aday kural kapısından geçer. Ajana sadece kural tanımları (DSL) ve ölçüm sonuçları döner, durumları `PENDING`. |
| `propose_rule` | kural tanımı (§6 DSL), `positives[]` (kolonlar), `negatives?[]` | Kural kapısında doğrulanır ve ölçülür: duyarlılık, özgüllük, şansa göre düzeltilmiş isabet, ReDoS / genellik kontrolü. Durum `PENDING`. |
| `activate_rule` / `retire_rule` | `rule_id`, `reason` | Kural canlıya alınır veya emekliye ayrılır; audit'e yazılır. **Kullanıcı onayı zorunludur** (§7.2). |
| `review_finding` | `database`, `table`, `column`, `decision` (onay / düzelt), `final_zone?`, `final_type?`, `reason` | İnsan kararı kaydedilir. Ajan bunu ancak kullanıcı sohbette söylediğinde çağırır. |

### 4.5 Rapor

| Araç | Girdi | Çıktı |
|---|---|---|
| `export_report` | `kind` (`inventory` / `masking_audit` / `rules`), filtreler | Excel dosyasını yerel `reports/` klasörüne yazar ve **sadece dosya yolunu** döner |

## 5. Guard: sızıntı önleme

Tüm araç çıktıları tek bir `guard(result)` fonksiyonundan geçer. Bu fonksiyonu atlayan bir araç yazılamaz: araçlar bir dekoratörle kaydedilir ve çıktı tipi şemadan gelir.

### 5.1 Çıktı şeması

Her aracın çıktısı tipli bir modeldir (pydantic). İzin verilen alanlar şunlardır:

- sayı;
- enum (bölge, tür, durum);
- tanımlayıcı (DB/şema/tablo/kolon/kural adı);
- sabit metin şablonları.

Serbest metin alanı yoktur. Hata mesajları da şablondan üretilir; sürücü hatası ham olarak dönmez, çünkü SQL hataları değer içerebilir.

### 5.2 k-anonimlik

- Bir sayım `k`'nın altındaysa "<k" olarak döner. Varsayılan `k = 20`.
- Bu eşik "şu regex tam olarak 1 satırda tutuyor mu?" gibi soru tekniğiyle tek tek değer çıkarmayı engeller. Ajan sadece dağılım öğrenebilir, birey öğrenemez.
- Kolon ve kural başına günlük ölçüm bütçesi vardır (ör. 200 `propose_rule` ölçümü). Bütçe aşılırsa araç reddeder.

### 5.3 Şekil profili

- Değerler karakter sınıfına indirgenir: rakam `9`, harf `A`, diğerleri olduğu gibi. Örnek: `TR12 0006 …` → `AA99 9999 …`.
- Profil sadece en az `k` kez görülen maskeleri ve uzunlukları içerir. Uzun değerlerde sadece önek gösterilir (ör. ilk 12 karakterin maskesi).
- Gerçek karakter ancak şu koşulda gösterilir: sık bir önekin (ör. `TR`, banka kodu) ilk 2–4 karakteri; en az `k` farklı değerde ve örneklemin en az %5'inde görülüyorsa.

### 5.4 redact ve log

- ScoutOut'taki `redact()` olduğu gibi korunur. Bütün çıktılar ve loglar ondan geçer.
- MCP stdio kanalına sadece JSON-RPC yazılır. Loglar redakte edilmiş olarak dosyaya gider.

### 5.5 Zorunlu test

Sentetik veri üretilir. Ardından bütün araçlar tüm parametre kombinasyonlarıyla çağrılır. Çıktılarda, loglarda, store'da ve raporlarda manifestteki hassas değerlerin **hiçbiri** bulunmamalıdır. Bu ScoutOut'taki kuralın MCP sürümüdür.

## 6. Kural DSL'i (ajanın yazabileceği tek "kod")

Ajan Python yazmaz, SQL yazmaz. Kurallar bildirimsel JSON'dur ve sunucu yorumlar:

```json
{
  "name": "kurum_musteri_no",
  "type": "CUSTOMER_NO",
  "column_name": { "any": ["(?i)musteri.?no", "(?i)cust(omer)?_?(no|id)"] },
  "value": {
    "length": [8, 10],
    "charset": "digits",
    "regex": "^[1-9][0-9]{7,9}$",
    "checksum": null,
    "prefix_in": null
  },
  "min_hit_rate": 0.9,
  "notes": "Ajan önerisi; Customer.CustomerNo baseline'ından"
}
```

- `checksum` sadece sunucudaki doğrulanmış algoritmalardan biri olabilir: `tckn`, `ykn`, `vkn`, `iban`, `luhn`. Ajan yeni algoritma tanımlayamaz.
- `regex` ReDoS kontrolünden geçer, zaman sınırıyla çalışır ve **genellik kuralına** uyar: en fazla 4 ardışık sabit karakter içerebilir, alternation yoluyla değer listesi kuramaz. Böylece kural, değer listesine dönüşerek sızıntı aracı olamaz.
- `prefix_in` sadece `profile_column` çıktısında görünen (k-anonim) öneklerden oluşabilir.
- `refset` alanı ajan tarafından yazılamaz. Referans setleri sadece `teach_baseline` ile sunucu içinde oluşur ve kurala adlarıyla bağlanır.

### 6.1 Yerel LLM ile kural madenciliği

Kural üretmenin iki yolu var ve ikisi de aynı kural kapısından geçer:

- **Ajan önerir** (`propose_rule`): Copilot ajanı şekil profillerine bakıp kuralı kendisi yazar.
- **Yerel LLM önerir** (`mine_rules`): base tablolardan alternatif kurallar çıkarmanın asıl yolu budur. Sunucu baseline kolonlarını okur ve yerel LLM'e verir. LLM kural adaylarını DSL olarak döner, sunucu her adayı ölçer.

Yerel LLM için kurallar:

- **Sadece yerel ve izinli adres:** config'deki allowlist (ör. `http://localhost:11434`). Başka bir adres verilirse sunucu açılmaz. ScoutOut'taki kontrol aynen korunur.
- **Tam bağlam serbest:** Model makineden çıkmadığı için yerel LLM'e her şey gidebilir. ScoutOut'taki "sadece redakte bağlam" kısıtı burada kaldırılır.
  - Baseline kolonlarından gerçek örnek değerler gider (ör. kolon başına 200 değer).
  - Negatif örnekler gider: aynı tablodaki hassas olmayan kolonlardan değerler. Böylece LLM ayırt edici özellikleri görür.
  - Bunlara ek olarak kolon adları ve tipleri, profiller, checksum geçiş oranları ve kullanıcının `hint` metni de gider.
- **Asıl sınır LLM'in çıkışındadır.** LLM ham değer gördüğü için yanıtı değer içerebilir. Bu yüzden:
  - **Serbest metin ajana gitmez.** LLM'in açıklaması ve gerekçesi atılır. Ajana sadece DSL alanları gider, onlar da şemaya göre doğrulanmış olarak.
  - **Sızıntı kontrolü zorunludur.** Kuralın regex'inde, sabitlerinde ve önek listesinde LLM'e gösterilen değerlerden biri ya da bunların 5 karakterden uzun bir parçası geçiyorsa kural reddedilir. Bu kontrol §6'daki genellik kuralına ek olarak çalışır.
  - **Ham değerler kalıcı yazılmaz.** İstem ve yanıt sadece bellekte tutulur. Store'a ve loga yalnızca kural tanımı, ölçüm sonucu ve "N değer gönderildi" bilgisi yazılır.
- **Çıktı yapılandırılmış olmalı:** LLM yanıtı JSON şemasına uymazsa atılır. Model çağrısı sınırlı sayıda tekrar dener (ör. 2).
- **Model seçimi config'de:** `llm.model` (ör. `qwen2.5:14b-instruct`), `llm.url`, `llm.timeout_s`. LLM yoksa ya da kapalıysa `mine_rules` "yerel LLM'e ulaşılamadı" der; diğer bütün araçlar çalışmaya devam eder.
- **Test:** LLM'in yerine sabit yanıt veren sahte bir model kullanılır. Gerçek Ollama'yla yapılan test `-m llm` işaretiyle çalışır, Ollama yoksa atlanır.

Ayrım şöyledir:
- **Yerel LLM:** kural adaylarını sunucunun içinde üretir.
- **Copilot ajanı:** akışı yönetir; hangi baseline'la çalışılacağına karar verir, sonuçları kullanıcıya açıklar, kullanıcı onay verince kuralı etkinleştirir.

### Kural kapısı (ScoutOut `rules.py`'den)

Bir öneri `PENDING` olarak kaydedilmeden önce dört kontrolden geçer:

1. **Sözdizimi ve güvenlik:** şema kontrolü, ReDoS, genellik kuralı.
2. **Sızıntı kontrolü:** kuralın içinde baseline değerlerinden biri geçmemeli. Değerler HMAC ile karşılaştırılır.
3. **Ölçüm:** pozitif kolonlarda duyarlılık, negatif ve rastgele kolonlarda özgüllük ölçülür; şansa göre düzeltilmiş isabet hesaplanır. Generic kurallarla örtüşme de raporlanır ("bu kural mevcut X kuralının %95'ini zaten kapsıyor").
4. **Karar:** eşikleri geçen kural `PENDING` olur ve `activate_rule` ile canlıya alınabilir. Geçemeyen kural nedeniyle birlikte reddedilir; ajan kuralı düzeltip yeniden deneyebilir.

## 7. VS Code tarafı

### 7.1 MCP sunucu tanımı: `.vscode/mcp.json`

```json
{
  "inputs": [
    { "id": "db_password", "type": "promptString", "description": "ScoutOut salt-okur DB parolası", "password": true }
  ],
  "servers": {
    "scoutout": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--directory", "D:/works/ScoutOut-Mcp", "scoutout-mcp"],
      "env": {
        "SCOUTOUT_CONFIG": "${env:APPDATA}/ScoutOut-Mcp/config.yaml",
        "SCOUTOUT_DB_PASSWORD": "${input:db_password}"
      }
    }
  }
}
```

- Parolayı VS Code ilk çalıştırmada bir kez sorar ve güvenli depoda saklar. Parola repoya ve dosyaya yazılmaz; ajan okuyamaz.
- Config dosyası (sunucu adı, login adı, DB listesi, k, bütçeler) workspace dışında tutulur.

### 7.2 Özel ajan: `.github/agents/tdm.agent.md`

```markdown
---
description: TDM ajanı — hassas veri keşfi ve maskeleme denetimi (veri görmeden)
tools: ['scoutout/*']
---
Sen bir bankanın test veri yönetimi ajanısın. Tek veri kaynağın scoutout MCP araçlarıdır.
- Gerçek veri isteme, tahmin etme, üretme. Değer gerekiyormuş gibi görünürse profile_column ve explain_finding kullan.
- Tarama isteklerinde önce kapsamı (DB, tablo sayısı, tahmini süre) söyle, sonra scan başlat; scan_status ile izle.
- Maskeleme denetiminde önce masking_sync, sonra audit_masking. Sonucu üç başlıkta ver:
  gereksiz maskeleme, eksik maskeleme, algoritma uyumsuzluğu. Her madde: DB.şema.tablo.kolon, sonuç, güven, neden.
- Kural üretirken: teach_baseline → mine_rules (yerel LLM adayları). Gerekirse profile_column ile kendi
  önerini propose_rule'la ekle. Ölçümleri kısa bir tabloda kullanıcıya göster.
  activate_rule'u ancak kullanıcı açıkça "etkinleştir" dediğinde çağır.
- review_finding'i ancak kullanıcı bir kararı açıkça onayladığında çağır.
- Kısa ve net yaz. Belirsiz sonuçları "belirsiz" diye bildir, kesinmiş gibi sunma.
```

- `tools` listesinde **sadece** scoutout araçları vardır. Terminal, dosya okuma/yazma ve web araçları kapalıdır. Bu önemli: terminali olan bir ajan `sqlcmd` ile DB'ye doğrudan gidebilir.
- VS Code'un araç onayı ayarında `activate_rule`, `retire_rule` ve `review_finding` "her zaman sor" olarak bırakılır, otomatik onaylanmaz.
- Not: VS Code'da özel ajan dosya adı ve `tools` söz dizimi sürüme göre değişti (eski adı "chat mode", `.chatmode.md`). Kurulumda kullanılan VS Code sürümüne göre doğrulanmalı.

## 8. Tipik akışlar

**Maskeleme denetimi**

1. `masking_sync`: liste okunur, fark varsa söylenir.
2. `audit_masking(databases=[…])`: taranmamış listeli kolonlar varsa önce taranır.
3. Ajan sonucu raporlar. Örnek: "`Cards.bank.Card.CardNo` listede, içerik %99 Luhn geçerli 16 hane; maskeleme gerekli. `CoreBanking.ops.CrmLog.Channel` listede, yüksek güvenle temiz; büyük olasılıkla gereksiz maskeleme."
4. İsteğe bağlı: `export_report(kind="masking_audit")` ile Excel üretilir.

**Kural üretme**

1. Kullanıcı base kolonları ve türlerini söyler; ajan `teach_baseline` çağırır.
2. Ajan `mine_rules` çağırır. Yerel LLM gerçek örnek değerlerden aday kurallar üretir. Örnek aday: "10 hane, 1–9 ile başlıyor, adında 'musteri' geçiyor".
3. Sunucu her adayı ölçer. Örnek sonuç: "duyarlılık 0,98, özgüllük 0,999, generic kurallarla örtüşme %0". Ajan adayları bir tabloda gösterir; isterse `profile_column` + `propose_rule` ile kendi önerisini ekler.
4. Kullanıcı "2 ve 3'ü etkinleştir" der; ajan `activate_rule` çağırır. Sonraki taramalar bu kuralları kullanır.

**Serbest soru:** "Şu tablolarda hassas veri var mı?" → `scan(tables=[…])` → `scan_status` → `get_findings` → gerekirse `explain_finding`.

## 9. Güvenlik notları ve açık kararlar

1. **İki model var:**
   - **Yerel LLM:** kural adaylarını üretir, gerçek veriyi görür, veri makineden çıkmaz.
   - **Copilot modeli:** ajanı çalıştırır, gerçek veriyi asla görmez.
   **Copilot modelinin konumu:** Ajanın gördüğü her şey (tablo/kolon adları, sayımlar, şekil profilleri) Copilot'un model sağlayıcısına gider. Değer gitmez ama meta veri gider. Banka güvenlik ekibine sorulmalı:
   - Kolon adları ve agregalar dışarı çıkabilir mi?
   - Çıkamıyorsa VS Code'da yerel model (ör. Ollama) seçilebilir; MCP tarafı değişmez.
2. **Sunucunun çalıştığı yer:** Varsayılan olarak MCP sunucusu kullanıcının makinesinde çalışır ve izole SQL sunucusuna ODBC ile bağlanır. Makinenin oraya ağ erişimi yoksa, sunucu izole ağdaki bir makinede çalıştırılıp VS Code'a HTTP + token ile bağlanabilir. Bu ikinci seçenek ancak gerekirse yapılmalı.
3. **Tam izolasyon:** Ajan sadece MCP araçlarıyla sınırlansa da kullanıcının kendisi DB'ye erişebilir; bu beklenen bir durumdur. Ajanın atlatamaması için §7.2'deki araç kısıtı yeterlidir. Daha sıkı bir ayrım istenirse MCP sunucusu ayrı bir Windows kullanıcısıyla çalıştırılabilir.
4. ScoutOut'tan devreden açık soru: `Dtg.TableColumnDataMaskDetail` tablosundaki `Production` / `Development` kolonları ne anlama geliyor, sorgu ortama göre filtrelenmeli mi?

## 10. Kilometre taşları

| Taş | İçerik | Bitti sayılır |
|---|---|---|
| **M0 İskelet** | Yeni repo; ScoutOut çekirdeği kopyalanır, web/zamanlayıcı/LLM çıkarılır. MCP sunucusu (Python `mcp` SDK, stdio), guard, `list_databases` / `list_tables` / `describe_table`. `mcp.json` ve ajan dosyası. | VS Code'da TDM ajanı sentetik DB'nin tablolarını listeler; MCP Inspector'da araçlar görünür |
| **M1 Tarama** | `scan` (job), `scan_status`, `get_findings`, `explain_finding`, `profile_column` (k-anonim) | Sentetik e2e: ajan akışıyla tarama sonuçları ScoutOut'la aynı |
| **M2 Maskeleme** | `masking_sync`, `masking_list`, `audit_masking`, `export_report` | Sentetik maskeleme listesiyle gereksiz / eksik / uyumsuz listeleri doğru |
| **M3 Kurallar** | Kural DSL'i, `teach_baseline`, yerel LLM bağlantısı ve `mine_rules` (allowlist, tam bağlam, şema zorunlu çıktı, çıkış sızıntı kontrolü, sahte model testleri; sahte model bilerek değer sızdıran kural döndürür ve bu kural reddedilmeli), `propose_rule` (kapı + ölçüm), `activate_rule` / `retire_rule`, `list_rules`, `review_finding` | Sentetik özel tür (ör. CustomerNo) için ajanın önerdiği kural ölçülür, etkinleştirilir, sonraki taramada yakalanır |
| **M4 Sertleştirme** | Sızıntı testi (bütün araçlar × manifest), soru tekniği testleri (regex ile tek değer çıkarma denemeleri "<k"da kalmalı), bütçe, hata metni temizliği, kurulum belgesi | Bütün testler yeşil; gerçek izole sunucuya bağlanıp salt-okur kontrolü geçer |

Çalışma kuralları ScoutOut'la aynıdır:

- yalınlık önce;
- kod İngilizce, kullanıcı metinleri Türkçe;
- algoritmalar resmî tanıma göre ve test vektörleriyle doğrulanır;
- her taş ayrı branch'te geliştirilir;
- testsiz kod birleştirilmez.
