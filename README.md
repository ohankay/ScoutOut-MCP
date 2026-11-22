# ScoutOut-Mcp

VS Code Copilot ajanlarının MCP üzerinden kullandığı yerel hassas veri keşif sunucusu. Ajan gerçek veriyi hiç görmez; sunucu sadece meta veri, kararlar, oranlar ve biçim istatistikleri döner.

Sunucu şunları yapar:
- Deterministik kurallarla hassas veri taraması.
- Maskeleme denetimi: gereksiz maskeleme, eksik maskeleme, algoritma uyumsuzluğu.
- Base kolonlardan yerel LLM ile yeni kural üretimi.

## Kurulum
```powershell
.\scripts\setup.ps1 -Server "SUNUCU,1433"
```

Betik şunları yapar:
1. Bağımlılıkları kurar.
2. `%APPDATA%\ScoutOut-Mcp\config.yaml` dosyasını ve parola dosyasını oluşturur. Parola dosyasını sadece sizin Windows kullanıcınız okuyabilir.
3. `.mcp.json` ve `.vscode\mcp.json` dosyalarını yazar.
4. `scoutout-mcp --check` ile bağlantıyı, salt-okur kontrolünü, maskeleme listesini ve yerel LLM'i doğrular.

VS Code'da Copilot Chat'i açın. Araçlar her Copilot ajanında (Agent modu) kullanılabilir. Hazır örnek ajan **ScoutOut** (`.github/agents/scoutout.agent.md`) sadece bu araçlara erişir; kendi ajanınızı yazarken `tools: ['scoutout/*']` ekleyin. Ajana terminal ve dosya araçları vermemeniz önerilir: terminali olan bir ajan veritabanına doğrudan bağlanabilir.

Kural üretimi (`mine_rules`) kurum içi OpenAI uyumlu bir LLM kullanır: vLLM gateway'i ya da Ollama. Bunu `config.yaml` → `llm` bölümünde tanımlayın: adres, model, izinli sunucu, `extra_body`, API anahtarı ve sertifika. Bağlantı proxy'siz kurulur; HTTPS sertifikası Windows sertifika deposundan doğrulanır.

## Araçlar
| Grup | Araçlar |
|---|---|
| Keşif | `list_databases`, `list_tables`, `describe_table` |
| Tarama | `scan`, `scan_status`, `get_findings`, `explain_finding`, `profile_column` |
| Maskeleme | `masking_sync`, `audit_masking` |
| Karar ve rapor | `review_finding`, `export_report` |
| Kurallar | `list_rules`, `teach_baseline`, `mine_rules`, `propose_rule`, `decide_rule` |

Ayrıntılar: [docs/SPEC.md](docs/SPEC.md). Testler: `uv run pytest`. MSSQL testleri için önce dev sunucusunu başlatın: `docker compose --profile dev up -d mssql`.
