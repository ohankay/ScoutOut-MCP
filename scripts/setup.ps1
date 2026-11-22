<#
.SYNOPSIS
  ScoutOut-MCP kurulumu (internetli veya internetsiz fark etmez): scripts\install-offline.ps1 ile aynıdır.
  SQL Server adı, kullanıcı adı ve parola ilk kurulumda terminalde sorulur.
#>
param(
    [string]$Server = "",
    [string]$User = "",
    [switch]$WindowsAuth,
    [string]$Workspace = "",
    [string]$Python = "",
    [string]$LlmEndpoint = "",
    [string]$LlmModel = "/data/models--Qwen--Qwen3-235B-FP8",
    [switch]$LlmApiKey,
    [switch]$TrustServerCertificate,
    [switch]$Force
)
& (Join-Path $PSScriptRoot "install-offline.ps1") @PSBoundParameters
