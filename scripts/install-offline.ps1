<#
.SYNOPSIS
  ScoutOut-MCP'yi internet erişimi olmayan bir Windows makinesine kurar. Hiçbir şey indirmez:
  Python paketleri repodaki offline\wheels klasöründen kurulur.

.EXAMPLE
  .\scripts\install-offline.ps1 -Server "sqlkopya01,1433"
  .\scripts\install-offline.ps1 -Server "sqlkopya01,1433" -Workspace D:\calisma\veri-kesif `
      -LlmEndpoint "https://llm-gateway/vllm-qwen3-235b-fp8/v1" -LlmModel "/data/models--Qwen--Qwen3-235B-FP8" -LlmApiKey
#>
param(
    [Parameter(Mandatory = $true)][string]$Server,   # SQL Server: "sunucu,port"
    [string]$User = "scoutout_ro",                   # salt-okur login
    [string]$Workspace = "",                         # VS Code'da açacağınız klasör (boş = bu repo)
    [string]$Python = "",                            # python.exe yolu (boş = Python 3.12 otomatik bulunur)
    [string]$LlmEndpoint = "",                       # kurum LLM'i (OpenAI uyumlu, /v1 ile biter); boş = sonra
    [string]$LlmModel = "/data/models--Qwen--Qwen3-235B-FP8",
    [switch]$LlmApiKey,                              # LLM anahtar istiyorsa: anahtarı sorar, dosyaya yazar
    [switch]$TrustServerCertificate,                 # SQL Server sertifikası kurumsal CA ile imzalı değilse
    [switch]$Force                                   # mevcut config.yaml / parola dosyasının üzerine yaz
)
$ErrorActionPreference = "Stop"
$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Wheels = Join-Path $Repo "offline\wheels"
if (-not $Workspace) { $Workspace = $Repo }
New-Item -ItemType Directory -Force $Workspace | Out-Null
$Workspace = (Resolve-Path $Workspace).Path
$Home_ = Join-Path $env:APPDATA "ScoutOut-Mcp"
New-Item -ItemType Directory -Force $Home_ | Out-Null
$Utf8 = New-Object Text.UTF8Encoding $false

function Write-Step($text) { Write-Host "`n== $text" -ForegroundColor Cyan }

# --- 1. Python 3.12 ---
Write-Step "Python 3.12 aranıyor"
if (-not $Python) {
    $candidates = @()
    if (Get-Command py -ErrorAction SilentlyContinue) {
        try { $candidates += (& py -3.12 -c "import sys; print(sys.executable)" 2>$null) } catch {}
    }
    $candidates += @("$env:LOCALAPPDATA\Programs\Python\Python312\python.exe", "C:\Program Files\Python312\python.exe")
    if (Get-Command python -ErrorAction SilentlyContinue) { $candidates += (Get-Command python).Source }
    foreach ($c in $candidates) {
        if ($c -and (Test-Path $c)) {
            $v = & $c -c "import sys; print('%d.%d' % sys.version_info[:2])"
            if ($v -eq "3.12") { $Python = $c; break }
        }
    }
}
if (-not $Python) {
    $installer = Join-Path $Repo "offline\python\python-3.12.10-amd64.exe"
    Write-Host "Python 3.12 bulunamadı. Repodaki kurulum dosyası kullanıcı için sessizce kuruluyor (yönetici yetkisi gerekmez)..."
    Start-Process -Wait -FilePath $installer -ArgumentList "/quiet", "InstallAllUsers=0", "PrependPath=0", "Include_test=0", "Include_launcher=0"
    $Python = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
    if (-not (Test-Path $Python)) { throw "Python kurulamadı. offline\python\python-3.12.10-amd64.exe dosyasını elle çalıştırın, sonra betiği tekrar çalıştırın." }
}
Write-Host "Python: $Python"

# --- 2. Sanal ortam ve paketler (internetsiz) ---
Write-Step "Paketler offline\wheels klasöründen kuruluyor"
$Venv = Join-Path $Repo ".venv"
if (-not (Test-Path (Join-Path $Venv "Scripts\python.exe"))) { & $Python -m venv $Venv }
$Py = Join-Path $Venv "Scripts\python.exe"
& $Py -m pip install --no-index --find-links $Wheels --upgrade --force-reinstall --no-deps (Get-ChildItem $Wheels -Filter "scoutout_mcp-*.whl").FullName | Out-Null
& $Py -m pip install --no-index --find-links $Wheels scoutout-mcp
if ($LASTEXITCODE -ne 0) { throw "Paket kurulumu başarısız." }
$Exe = Join-Path $Venv "Scripts\scoutout-mcp.exe"

# --- 3. ODBC sürücüsü ---
Write-Step "ODBC sürücüsü kontrol ediliyor"
$drivers = (Get-OdbcDriver -Platform 64-bit -ErrorAction SilentlyContinue | Where-Object Name -like "ODBC Driver * for SQL Server").Name
if ($drivers) { Write-Host "Bulundu: $($drivers -join ', ')" }
else { Write-Warning "Microsoft ODBC Driver 17 veya 18 for SQL Server kurulu değil. Kurumun yazılım merkezinden kurun (bkz. docs\KURULUM-OFFLINE.md)." }

# --- 4. Parolalar (dosyada; sadece bu Windows kullanıcısı okuyabilir) ---
function Save-Secret($path, $prompt) {
    $sec = Read-Host $prompt -AsSecureString
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
    [IO.File]::WriteAllText($path, $plain)
    $plain = $null
    & icacls $path /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null
}
Write-Step "Parolalar"
$PwFile = Join-Path $Home_ "db_password"
if ($Force -or -not (Test-Path $PwFile)) { Save-Secret $PwFile "$User parolası" } else { Write-Host "Mevcut DB parola dosyası korunuyor." }
$KeyFile = Join-Path $Home_ "llm_api_key"
if ($LlmApiKey -and ($Force -or -not (Test-Path $KeyFile))) { Save-Secret $KeyFile "LLM API anahtarı" }

# --- 5. config.yaml ---
Write-Step "Yapılandırma"
$Config = Join-Path $Home_ "config.yaml"
if ($Force -or -not (Test-Path $Config)) {
    $text = Get-Content (Join-Path $Repo "config.example.yaml") -Raw -Encoding UTF8
    $text = $text.Replace('server: "SUNUCU_ADI,1433"', "server: `"$Server`"")
    $text = $text.Replace("user: scoutout_ro ", "user: $User ")
    $text = $text.Replace('password_file: ""', "password_file: '$($PwFile.Replace('\', '/'))'")
    if ($TrustServerCertificate) { $text = $text.Replace("trust_server_certificate: false", "trust_server_certificate: true") }
    if ($LlmEndpoint) {
        $llmHost = ([Uri]$LlmEndpoint).Host
        $text = $text.Replace("endpoint: https://LLM-GATEWAY/vllm-qwen3-235b-fp8/v1", "endpoint: $LlmEndpoint")
        $text = $text.Replace("model: /data/models--Qwen--Qwen3-235B-FP8", "model: $LlmModel")
        $text = $text.Replace("allowed_hosts: [LLM-GATEWAY]", "allowed_hosts: [$llmHost]")
    } else {
        $text = $text.Replace("endpoint: https://LLM-GATEWAY/vllm-qwen3-235b-fp8/v1", "endpoint: `"`"   # kurum LLM adresi (sonra doldurun)")
    }
    if ($LlmApiKey) {
        $text = $text.Replace("api_key_env: SCOUTOUT_LLM_API_KEY", "api_key_file: '$($KeyFile.Replace('\', '/'))'")
    }
    [IO.File]::WriteAllText($Config, $text, $Utf8)
    Write-Host "Yazıldı: $Config"
} else {
    Write-Host "Mevcut yapılandırma korunuyor: $Config (yenilemek için -Force)"
}

# --- 6. VS Code: MCP sunucusu ve örnek ajan ---
Write-Step "VS Code tanımları"
$ExeJson = $Exe.Replace('\', '/')
$copilot = "{`n  `"mcpServers`": {`n    `"scoutout`": {`n      `"type`": `"stdio`",`n      `"command`": `"$ExeJson`",`n      `"args`": [],`n      `"tools`": [`"*`"]`n    }`n  }`n}`n"
[IO.File]::WriteAllText((Join-Path $Workspace ".mcp.json"), $copilot, $Utf8)
New-Item -ItemType Directory -Force (Join-Path $Workspace ".vscode") | Out-Null
$local = "{`n  `"servers`": {`n    `"scoutout`": {`n      `"type`": `"stdio`",`n      `"command`": `"$ExeJson`",`n      `"args`": []`n    }`n  }`n}`n"
[IO.File]::WriteAllText((Join-Path $Workspace ".vscode\mcp.json"), $local, $Utf8)
New-Item -ItemType Directory -Force (Join-Path $Workspace ".github\agents") | Out-Null
if ($Workspace -ne $Repo) {
    Copy-Item (Join-Path $Repo ".github\agents\scoutout.agent.md") (Join-Path $Workspace ".github\agents\scoutout.agent.md") -Force
}
Write-Host "Yazıldı: $Workspace\.mcp.json, .vscode\mcp.json, .github\agents\scoutout.agent.md"

# --- 7. Kontrol ---
Write-Step "Kontrol"
& $Exe --check
