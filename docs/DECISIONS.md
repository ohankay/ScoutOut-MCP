# Kararlar

**M-001 — Çekirdek kopyalandı, paket adı korundu.** ScoutOut'un `src/scoutout` paketi (web, CLI, zamanlayıcı ve otomatik onay hariç) aynen alındı; içe aktarmalar değişmesin diye paket adı `scoutout` kaldı. MCP katmanı ayrı paket: `scoutout_mcp`.

**M-002 — MCP SDK 2.x.** `mcp` 2.x kullanılıyor (`mcp.server.mcpserver.MCPServer`; 1.x'teki FastMCP'nin yeni adı). Taşıma: stdio.

**M-003 — Parola dosyada, workspace dışında.** VS Code'un yeni Copilot MCP biçimi (`.mcp.json`) parola sorma alanını (`inputs`) desteklemiyor. Parola bu yüzden `%APPDATA%\ScoutOut-Mcp\db_password` dosyasında duruyor; dosyayı sadece Windows kullanıcısı okuyabiliyor. Ajan yalnızca scoutout araçlarına erişebildiği için bu dosyayı okuyamaz.

**M-004 — Değer sızdıran kural saklanmaz.** Genellik kuralını (en fazla 4 ardışık sabit karakter), gösterilen değer kontrolünü ya da taban HMAC kontrolünü geçemeyen kural reddedilir. Böyle bir kural store'a yazılmaz; ajana deseni olmadan sadece gerekçesi döner.

**M-005 — Denetim sistem kararlarıyla yapılır.** ScoutOut raporları insan onaylı kararlardan oluşuyordu. `audit_masking` ise son sistem kararını kullanır; bir kolon gözden geçirildiyse insan kararı esas alınır. Gereksiz maskeleme sadece çelişkisiz CLEAN veya EMPTY kolonlarda önerilir.

**M-006 — Kurum içi LLM gateway'i.** LLM istemcisi her OpenAI uyumlu kurum içi sunucuyla çalışır: Ollama, vLLM ya da APISIX gibi bir gateway. Kurallar:
- Sunucu adı `llm.allowed_hosts` listesinde olmalı.
- Bağlantı her zaman proxy'siz kurulur (`trust_env=False`).
- HTTPS sertifikası Windows sertifika deposundan doğrulanır (`truststore`); istenirse bir PEM dosyası verilebilir.
- İsteğe bağlı Bearer anahtarı ortam değişkeninden ya da dosyadan okunur.
- `extra_body` isteğe olduğu gibi eklenir (Qwen3'te `enable_thinking: false`). `<think>` blokları yanıttan atılır.
- `response_format` alanını reddeden sunucuya istek bu alan olmadan tekrar gönderilir.

**M-007 — Ajan genel.** Araçlar her Copilot ajanında kullanılabilir. `.github/agents/scoutout.agent.md` yalnızca bir örnektir ve sadece `scoutout/*` araçlarına erişir.
