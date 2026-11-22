<#
.SYNOPSIS
  ScoutOut-Mcp kurulumu: bağımlılıklar, yapılandırma, parola dosyası, VS Code MCP tanımı ve örnek Copilot ajanı.

.EXAMPLE
  .\scripts\setup.ps1 -Server "sqlkopya01,1433"
  .\scripts\setup.ps1 -Server "localhost,14333" -TrustServerCertificate -Workspace D:\works\veri-kesif
#  (Workspace: VS Code'da açacağınız klasör)
#>
param(
    [Parameter(Mandatory = $true)][string]$Server,
    [string]$User = "scoutout_ro",
    [string]$Workspace = "",            # VS Code'da açacağınız klasör (boş = bu repo)
    [switch]$TrustServerCertificate,
    [switch]$Force                       # mevcut config.yaml'ın üzerine yaz
)
$ErrorActionPreference = "Stop"
$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $Workspace) { $Workspace = $Repo }
$Workspace = (Resolve-Path $Workspace).Path
$Home_ = Join-Path $env:APPDATA "ScoutOut-Mcp"
New-Item -ItemType Directory -Force $Home_ | Out-Null

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { throw "uv bulunamadı: https://docs.astral.sh/uv/ adresinden kurun." }
Write-Host "Bağımlılıklar kuruluyor..."
& uv sync --directory $Repo --no-dev | Out-Null

# --- parola (dosyada; sadece bu Windows kullanıcısı okuyabilir) ---
$PwFile = Join-Path $Home_ "db_password"
if ($Force -or -not (Test-Path $PwFile)) {
    $sec = Read-Host "$User parolası" -AsSecureString
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
    [IO.File]::WriteAllText($PwFile, $plain)
    $plain = $null
    & icacls $PwFile /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null
}

# --- config.yaml ---
$Config = Join-Path $Home_ "config.yaml"
if ($Force -or -not (Test-Path $Config)) {
    $text = Get-Content (Join-Path $Repo "config.example.yaml") -Raw -Encoding UTF8
    $text = $text.Replace('server: "SUNUCU_ADI,1433"', "server: `"$Server`"")
    $text = $text.Replace("user: scoutout_ro ", "user: $User ")
    $text = $text.Replace('password_file: ""', "password_file: '$($PwFile.Replace('\', '/'))'")
    if ($TrustServerCertificate) { $text = $text.Replace("trust_server_certificate: false", "trust_server_certificate: true") }
    [IO.File]::WriteAllText($Config, $text, (New-Object Text.UTF8Encoding $false))
    Write-Host "Yapılandırma yazıldı: $Config"
} else {
    Write-Host "Mevcut yapılandırma korunuyor: $Config (yenilemek için -Force)"
}

# --- VS Code: MCP sunucusu ve örnek ajan ---
$RepoJson = $Repo.Replace('\', '/')
$args_ = "[`"run`", `"--directory`", `"$RepoJson`", `"scoutout-mcp`"]"
$copilot = "{`n  `"mcpServers`": {`n    `"scoutout`": {`n      `"type`": `"stdio`",`n      `"command`": `"uv`",`n      `"args`": $args_,`n      `"tools`": [`"*`"]`n    }`n  }`n}`n"
[IO.File]::WriteAllText((Join-Path $Workspace ".mcp.json"), $copilot, (New-Object Text.UTF8Encoding $false))
New-Item -ItemType Directory -Force (Join-Path $Workspace ".vscode") | Out-Null
$local = "{`n  `"servers`": {`n    `"scoutout`": {`n      `"type`": `"stdio`",`n      `"command`": `"uv`",`n      `"args`": $args_`n    }`n  }`n}`n"
[IO.File]::WriteAllText((Join-Path $Workspace ".vscode\mcp.json"), $local, (New-Object Text.UTF8Encoding $false))
if ($Workspace -ne $Repo) {
    New-Item -ItemType Directory -Force (Join-Path $Workspace ".github\agents") | Out-Null
    Copy-Item (Join-Path $Repo ".github\agents\scoutout.agent.md") (Join-Path $Workspace ".github\agents\scoutout.agent.md") -Force
}
Write-Host "VS Code tanımları yazıldı: $Workspace\.mcp.json, .vscode\mcp.json, .github\agents\scoutout.agent.md"

Write-Host "`nKontrol:"
& uv run --directory $Repo scoutout-mcp --check
