# ScoutOut-Mcp — Claude Code çalışma kuralları

Spesifikasyon: `docs/SPEC.md`. Kararlar: `docs/DECISIONS.md`. Çekirdek (`src/scoutout`) ScoutOut projesinden gelir; MCP katmanı `src/scoutout_mcp`.

- **Yalınlık önce:** önce işe yarayan basit sistem. Over-engineering yok. Bağımlılık sadece gerektiğinde.
- Kod, tanımlayıcılar ve commit mesajları İngilizce; kullanıcıya dönük metinler (araç açıklamaları, hata mesajları, dokümanlar) Türkçe.
- **Ajan veriyi görmez:** her MCP aracı çıktısı `scoutout_mcp.guard` üzerinden döner. Hücre değeri, örnek satır ya da redakte örnek dönen araç yazılmaz. Sızıntı testi (`tests/mcp/test_leak.py`) her araç için yeşil kalmalı.
- Yerel LLM ham değer görebilir; ama LLM çıktısından ajana sadece doğrulanmış kural alanları gider.
- Doğrulama algoritmalarını resmî tanıma göre uygula, test vektörleriyle doğrula.
- Her kilometre taşı ayrı branch'te ve ayrı commit'lerde. Testsiz kod birleştirilmez.

## Ortam notları
- Windows; `uv sync`, `uv run pytest` (veya `.venv/Scripts/python -m pytest`).
- Dev SQL Server: `docker compose --profile dev up -d mssql` (port 14333, parolalar `.env`). MSSQL testleri `-m mssql`, sunucu yoksa atlanır.
- Sentetik veri testlerde ilk ihtiyaçta kurulur (`synth_out/<db>/manifest.json`).
