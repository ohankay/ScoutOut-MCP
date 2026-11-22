# Kararlar

**M-001 — Çekirdek kopyalandı, paket adı korundu.** ScoutOut'un `src/scoutout` paketi (web, CLI, zamanlayıcı ve otomatik onay hariç) aynen alındı; içe aktarmalar değişmesin diye paket adı `scoutout` kaldı. MCP katmanı ayrı paket: `scoutout_mcp`.

**M-002 — MCP SDK 2.x.** `mcp` 2.x kullanılıyor (`mcp.server.mcpserver.MCPServer`; 1.x'teki FastMCP'nin yeni adı). Taşıma: stdio.
