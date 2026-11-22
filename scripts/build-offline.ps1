# Rebuilds offline\wheels (internet needed): locked dependencies + this project, for Windows x64 / Python 3.12.
$ErrorActionPreference = "Stop"
$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Out = Join-Path $Repo "offline\wheels"
Remove-Item -Recurse -Force $Out -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $Out | Out-Null
Push-Location $Repo
try {
    uv export --no-dev --no-hashes --format requirements-txt --no-emit-project | Out-File -Encoding ascii offline\requirements.txt
    py -3.12 -m pip download -q -r offline\requirements.txt -d $Out --platform win_amd64 --python-version 3.12 --implementation cp --only-binary=:all:
    uv build --wheel -o $Out
    Remove-Item (Join-Path $Out ".gitignore") -ErrorAction SilentlyContinue  # uv build leaves an ignore-all file
    Write-Host "Hazır: $((Get-ChildItem $Out).Count) dosya, $([math]::Round(((Get-ChildItem $Out | Measure-Object Length -Sum).Sum / 1MB))) MB"
} finally { Pop-Location }
