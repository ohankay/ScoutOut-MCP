# İnternet erişimi kısıtlı bir bilgisayara kurulum

Bu belge, ScoutOut-MCP'nin internetten paket indiremeyen bir Windows bilgisayara baştan sona nasıl kurulacağını anlatır. Kurulum için gereken her şey repoda:

| Klasör | İçerik |
|---|---|
| `offline/wheels/` | Bütün Python paketleri (Windows 64 bit, Python 3.12 için) ve ScoutOut-MCP'nin kendi paketi |
| `offline/python/` | Python 3.12.10 kurulum dosyası (python.org'dan alınmış, Python Software Foundation imzalı) |
| `scripts/install-offline.ps1` | Hiçbir şey indirmeden kuran betik |

Kurulumdan sonra bilgisayarın erişmesi gereken yerler sadece şunlar:
- Kurum ağındaki **SQL Server** (canlı kopyaların açıldığı sunucu).
- Kural üretimi kullanılacaksa **kurum LLM gateway'i**.

---

## 1. Repoyu bilgisayara taşıyın

**İnternetli bir bilgisayarda** iki yoldan birini kullanın:
- GitHub'da https://github.com/ohankay/ScoutOut-MCP → **Code → Download ZIP**.
- Ya da: `git clone https://github.com/ohankay/ScoutOut-MCP.git`

ZIP yaklaşık 100 MB'tır. Kurumun onaylı aktarım yöntemiyle (dosya aktarım sunucusu, onaylı USB vb.) hedef bilgisayara taşıyın.

**Hedef bilgisayarda:**
1. ZIP'i yolunda boşluk olmayan kalıcı bir klasöre açın, ör. `D:\araclar\ScoutOut-MCP`. VS Code bu klasördeki programı çalıştıracak; kurulumdan sonra klasörü taşımayın.
2. ZIP internetten indirildiği için Windows dosyaları engelleyebilir. Açtıktan sonra bir kez şunu çalıştırın:

```powershell
Get-ChildItem -Recurse D:\araclar\ScoutOut-MCP | Unblock-File
```

## 2. Ön koşullar

| Gereken | Nasıl kontrol edilir | Yoksa |
|---|---|---|
| Windows 10/11, 64 bit | — | — |
| **Python 3.12** | `py -3.12 --version` | Kurulum betiği repodaki `offline\python\python-3.12.10-amd64.exe` dosyasını sizin kullanıcınıza kendiliğinden kurar; yönetici yetkisi gerekmez |
| **Microsoft ODBC Driver 17 veya 18 for SQL Server** | `Get-OdbcDriver -Name "ODBC Driver*SQL Server*"` | Kurumun yazılım merkezinden kurun. Yoksa `msodbcsql.msi` dosyasını internetli bir bilgisayardan Microsoft'un sitesinden alıp aynı yoldan taşıyın. Repoya eklenmedi: kurumlar genelde kendi dağıtımını kullanır |
| **VS Code ve GitHub Copilot uzantısı** | VS Code → Uzantılar | Uzantıyı `.vsix` dosyası olarak taşıyıp **Install from VSIX** ile kurun |
| **SQL Server'da salt-okur login** | — | Aşağıdaki SQL'i DBA'ya çalıştırtın |
| SQL Server'a ağ erişimi | `Test-NetConnection SUNUCU -Port 1433` | Ağ/güvenlik ekibi |

> **Copilot hakkında önemli not:** GitHub Copilot sohbeti varsayılan olarak GitHub'ın model servisine bağlanır. Bilgisayarın internet erişimi kısıtlıysa Copilot'un adreslerine izin verilmiş olmalı (kurumunuzun Copilot politikası). Buna izin yoksa VS Code'da kurum içi bir modeli sohbet modeli olarak tanımlamak gerekir. ScoutOut-MCP'nin kendisi internete hiç çıkmaz.

### Salt-okur SQL login (DBA çalıştırır)

Yetkiler sunucu düzeyinde verilir. Böylece her gün restore edilen kopyalarda da yetkiler kendiliğinden geçerli kalır; veritabanı bazında kullanıcı açmak gerekmez.

```sql
CREATE LOGIN scoutout_ro WITH PASSWORD = '<güçlü parola>', CHECK_POLICY = ON;
GRANT CONNECT ANY DATABASE TO scoutout_ro;        -- bütün veritabanlarına bağlanabilir
GRANT SELECT ALL USER SECURABLES TO scoutout_ro;  -- sadece okur (BoaCatalog dahil)
GRANT VIEW SERVER STATE TO scoutout_ro;           -- değişen tabloları anlamak için (önerilir)
```

ScoutOut-MCP bağlanırken yetkileri kontrol eder. Login'in yazma yetkisi varsa bağlanmayı reddeder.

## 3. Kurulum

Kurulum klasöründe PowerShell açın.

**Kural üretimini sonra ayarlamak isterseniz** temel kurulum yeterli:

```powershell
cd D:\araclar\ScoutOut-MCP
.\scripts\install-offline.ps1 -Server "SQLSUNUCU,1433" -Workspace D:\calisma\veri-kesif
```

**Kurum LLM'ini de şimdi tanımlamak isterseniz:**

```powershell
.\scripts\install-offline.ps1 -Server "SQLSUNUCU,1433" -Workspace D:\calisma\veri-kesif -LlmEndpoint "https://LLM-GATEWAY/vllm-qwen3-235b-fp8/v1" -LlmModel "/data/models--Qwen--Qwen3-235B-FP8" -LlmApiKey
```

Parametreler:

| Parametre | Açıklama |
|---|---|
| `-Server` | SQL Server adresi ve portu (zorunlu) |
| `-User` | Login adı (varsayılan `scoutout_ro`) |
| `-Workspace` | VS Code'da açacağınız çalışma klasörü. MCP tanımı ve örnek ajan buraya yazılır. Boşsa kurulum klasörü kullanılır |
| `-TrustServerCertificate` | SQL Server sertifikası kurumsal CA ile imzalı değilse (test sunucuları) |
| `-LlmEndpoint`, `-LlmModel` | Kurum LLM'i (OpenAI uyumlu, `/v1` ile biter). İzin listesine gateway adresi otomatik yazılır |
| `-LlmApiKey` | Gateway anahtar istiyorsa: anahtarı sorar ve dosyaya yazar |
| `-Python` | Python 3.12'nin yolu. Otomatik bulunamazsa verin |
| `-Force` | Mevcut yapılandırmanın ve parolanın üzerine yazar |

**Betik şunları yapar (hiçbir şey indirmez):**
1. Python 3.12'yi bulur; yoksa repodaki kurulum dosyasından kurar.
2. Kurulum klasöründe `.venv` sanal ortamını oluşturur ve bütün paketleri `offline\wheels` klasöründen kurar (`pip --no-index`).
3. ODBC sürücüsünü kontrol eder.
4. DB parolasını (ve isteğe bağlı LLM anahtarını) sorar. Bunları `%APPDATA%\ScoutOut-Mcp\` altında, sadece sizin Windows kullanıcınızın okuyabildiği dosyalara yazar. Parola repoya ve VS Code ayarlarına girmez.
5. `%APPDATA%\ScoutOut-Mcp\config.yaml` dosyasını yazar.
6. Çalışma klasörüne üç dosya yazar: `.mcp.json` (Copilot), `.vscode\mcp.json` (eski VS Code biçimi) ve `.github\agents\scoutout.agent.md` (örnek ajan).
7. `scoutout-mcp --check` ile her şeyi doğrular.

Beklenen kontrol çıktısı:

```
[ OK ] Yapılandırma: C:\Users\...\AppData\Roaming\ScoutOut-Mcp\config.yaml
[ OK ] Parola bulundu
[ OK ] Sunucu SQLSUNUCU,1433: 37 veritabanı (...)
[ OK ] Salt-okur kontrolü geçti (...)
[ OK ] Maskeleme listesi okundu: 1250 kolon (v1)
[ OK ] LLM https://.../v1: /data/models--Qwen--Qwen3-235B-FP8 hazır
```

## 4. Ayarlar (`%APPDATA%\ScoutOut-Mcp\config.yaml`)

Betik temel ayarları doldurur. Sık değiştirilenler:

```yaml
mcp:
  databases: []              # boş = login'in açabildiği bütün veritabanları; ör. [CoreBanking, Kart]
  exclude_databases: []
  masking_database: BoaCatalog
  masking_query: >-          # Dtg.TableColumnDataMaskDetail'den okunacak kolonlar
    SELECT DatabaseName, SchemaName, TableName, ColumnName, DataMaskType, TermName
    FROM Dtg.TableColumnDataMaskDetail
llm:
  endpoint: https://LLM-GATEWAY/vllm-qwen3-235b-fp8/v1
  model: /data/models--Qwen--Qwen3-235B-FP8
  allowed_hosts: [LLM-GATEWAY]
  extra_body:
    chat_template_kwargs: {enable_thinking: false}
  ca_bundle: ""              # boş = Windows sertifika deposu (kurumsal CA)
scan:
  maintenance_window: ""     # ör. "22:00-06:00": taramalar sadece bu saatlerde
  concurrency: 4
```

Ayar değişikliğinden sonra VS Code'da MCP sunucusunu yeniden başlatın: Komut Paleti → **MCP: List Servers** → scoutout → Restart.

## 5. VS Code'da kullanım

1. VS Code'da **çalışma klasörünü** açın (`-Workspace` ile verdiğiniz klasör).
2. VS Code `.mcp.json` dosyasındaki **scoutout** sunucusunu bulur. İlk açılışta sunucuya güvenmeniz istenebilir; onaylayın. Gerekirse sunucuyu Komut Paleti → **MCP: List Servers** → scoutout → **Start** ile başlatın.
3. Copilot Chat'i açın, **Agent** moduna geçin ve ajan olarak **ScoutOut**'u seçin. Kendi ajanınızı yazarsanız `tools` listesine `'scoutout/*'` ekleyin.
4. Örnek istekler:
   - "Hangi veritabanlarına erişebiliyorsun, hangileri taranmış?"
   - "CoreBanking'de bank şemasındaki tabloları tara, hassas veri var mı?"
   - "CoreBanking için maskeleme tanımları gerçekçi mi? Gereksiz veya eksik maskeleme var mı?"
   - "Musteri.MusteriNo bizim müşteri numaramız; kurum LLM'iyle tespit kuralı çıkar."
   - "Maskeleme denetimini Excel olarak ver."

**Güvenlik önerileri:**
- Bu ajana **terminal ve dosya araçlarını vermeyin**: terminali olan bir ajan veritabanına doğrudan bağlanabilir.
- `decide_rule` ve `review_finding` için onay sorusunu "her zaman izin ver" yapmayın.

Raporlar `%APPDATA%\ScoutOut-Mcp\data\reports\` klasörüne yazılır.

## 6. Sorun giderme

| Belirti | Çözüm |
|---|---|
| `Python 3.12 bulunamadı` ve kurulum başarısız | `offline\python\python-3.12.10-amd64.exe` dosyasını elle çalıştırın ("Install Now"), sonra betiği tekrar çalıştırın veya `-Python` ile yolu verin |
| `Paket kurulumu başarısız` | Python sürümü 3.12 olmalı ve 64 bit olmalı. Klasörün eksiksiz kopyalandığını kontrol edin (`offline\wheels` içinde 46 dosya) |
| `Data source name not found` / ODBC hatası | ODBC Driver 17 veya 18 kurulu değil (bkz. ön koşullar) |
| `Login failed` | Login adı ya da parola yanlış. Parolayı yenilemek için betiği `-Force` ile çalıştırın |
| `Bağlantının yazma yetkisi var` | Login'in yazma yetkisi var; salt-okur login kullanın (güvenlik gereği bağlanmaz) |
| `SSL` / `certificate` hatası (SQL) | Sunucu sertifikası kurumsal CA ile imzalı değilse betiği `-TrustServerCertificate` ile çalıştırın |
| `LLM sunucusu ... izin listesinde değil` | `config.yaml` → `llm.allowed_hosts` listesine gateway'in sunucu adını yazın |
| LLM sertifika hatası | Kurumsal kök sertifika Windows deposunda değilse PEM dosyasını `llm.ca_bundle` ile verin |
| LLM'e ulaşılamıyor | Bağlantı proxy kullanmaz (bilerek). Gateway'e doğrudan erişim olmalı: `Test-NetConnection LLM-GATEWAY -Port 443` |
| Maskeleme listesi okunamıyor | Login'in BoaCatalog'u okuyabildiğini ve `masking_query` sorgusunun doğru olduğunu kontrol edin |
| VS Code sunucuyu görmüyor | Doğru klasörü açtığınızdan emin olun; `.mcp.json` çalışma klasörünün kökünde olmalı |

Ayrıntılı log: `%APPDATA%\ScoutOut-Mcp\data\logs\scoutout.log`. Log değer içermez.

## 7. Güncelleme ve kaldırma

**Güncelleme:**
1. Yeni sürümü aynı yolla taşıyın ve eski klasörün üzerine açın.
2. Betiği tekrar çalıştırın (`-Force` olmadan). Yapılandırma, parola, tarama sonuçları ve öğrenilen kurallar korunur.

**Kaldırma:**
1. Kurulum klasörünü silin.
2. `%APPDATA%\ScoutOut-Mcp` klasörünü silin. Bu klasörde parola, yapılandırma, sonuçlar ve raporlar vardır.
3. Çalışma klasöründeki `.mcp.json`, `.vscode\mcp.json` ve `.github\agents\scoutout.agent.md` dosyalarını silin.

## 8. Bakımcılar için: offline paketleri yenilemek

İnternetli bir geliştirme bilgisayarında, bağımlılıklar değiştiğinde çalıştırın:

```powershell
.\scripts\build-offline.ps1
```

Betik `offline\wheels` klasörünü yeniden üretir: kilitli bağımlılıklar (`uv.lock`) ve projenin kendi paketi Windows 64 bit / Python 3.12 için indirilir. Ardından commit'leyip push edin.
