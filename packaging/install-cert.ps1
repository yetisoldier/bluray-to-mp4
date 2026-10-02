# One-time setup: make Windows trust YetiWurks builds (run once per PC).
# Right-click this file -> "Run with PowerShell", or from an admin PowerShell:
#   powershell -ExecutionPolicy Bypass -File .\install-cert.ps1
$ErrorActionPreference = "Stop"

# Self-elevate if needed.
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
  ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
  Start-Process powershell -Verb RunAs -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
  exit
}

$rootCa = Join-Path $PSScriptRoot "YetiWurks-Root-CA.cer"
$signer = Join-Path $PSScriptRoot "YetiWurks-CodeSigning.cer"
if (-not (Test-Path $rootCa)) { throw "YetiWurks-Root-CA.cer not found next to the script" }
if (-not (Test-Path $signer)) { throw "YetiWurks-CodeSigning.cer not found next to the script" }

# 1) Trust the YetiWurks Root CA machine-wide -> the exe's signature chain
#    validates (LocalMachine\Root, no confirmation prompt when admin).
Import-Certificate -FilePath $rootCa -CertStoreLocation Cert:\LocalMachine\Root | Out-Null

# 2) Treat the signing certificate as a Trusted Publisher -> Windows stops
#    throwing unknown-publisher warnings for YetiWurks-signed binaries.
Import-Certificate -FilePath $signer -CertStoreLocation Cert:\LocalMachine\TrustedPublisher | Out-Null

# 3) Strip the mark-of-the-web from everything extracted from the zip so
#    Windows doesn't apply internet-download heuristics to local files.
Get-ChildItem -Path $PSScriptRoot -Recurse -File | Unblock-File

Write-Host ""
Write-Host "YetiWurks Root CA installed, signer trusted, files unblocked." -ForegroundColor Green
Write-Host "You can now run bluray-to-mp4.exe without warnings."
Write-Host ""
Read-Host "Press Enter to close"