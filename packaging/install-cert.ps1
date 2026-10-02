# One-time setup: make Windows trust YetiWurks builds (run as Administrator).
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

$cer = Join-Path $PSScriptRoot "YetiWurks-CodeSigning.cer"
if (-not (Test-Path $cer)) { throw "YetiWurks-CodeSigning.cer not found next to the script" }

# Trust the YetiWurks signing certificate machine-wide, and treat it as a
# trusted publisher so signed builds stop throwing unknown-publisher walls.
Import-Certificate -FilePath $cer -CertStoreLocation Cert:\LocalMachine\Root            | Out-Null
Import-Certificate -FilePath $cer -CertStoreLocation Cert:\LocalMachine\TrustedPublisher | Out-Null

# Strip the mark-of-the-web from everything extracted from the zip so
# Windows doesn't apply internet-download heuristics to local files.
Get-ChildItem -Path $PSScriptRoot -Recurse -File | Unblock-File

Write-Host ""
Write-Host "YetiWurks certificate installed and files unblocked." -ForegroundColor Green
Write-Host "You can now run bluray-to-mp4.exe without warnings."
Write-Host ""
Read-Host "Press Enter to close"
