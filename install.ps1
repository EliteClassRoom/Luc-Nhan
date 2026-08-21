# ──────────────────────────────────────────────────────────────────────
# Rikugan — universal installer (Windows)
#
#   irm https://raw.githubusercontent.com/EliteClassRoom/rikugan/master/install.ps1 | iex
#
# Or with arguments:
#   & ([scriptblock]::Create((irm https://raw.githubusercontent.com/EliteClassRoom/rikugan/master/install.ps1))) -Target ida
#
# Environment variables:
#   RIKUGAN_DIR     — where to clone the repo   (default: ~\.rikugan)
#   RIKUGAN_BRANCH  — git branch to check out   (default: master)
#   IDA_PYTHON      — override Python for IDA    (forwarded to install_ida.bat)
# ──────────────────────────────────────────────────────────────────────

param(
    [ValidateSet("ida", "")]
    [string]$Target = ""
)

$ErrorActionPreference = "Stop"

$RepoUrl = "https://github.com/EliteClassRoom/rikugan.git"
$InstallDir = if ($env:RIKUGAN_DIR) { $env:RIKUGAN_DIR } else { Join-Path $HOME ".rikugan" }
$Branch = if ($env:RIKUGAN_BRANCH) { $env:RIKUGAN_BRANCH } else { "master" }

# ── Helpers ──────────────────────────────────────────────────────────
function Write-Info    { param($Msg) Write-Host "[*] $Msg" -ForegroundColor Cyan }
function Write-Ok      { param($Msg) Write-Host "[+] $Msg" -ForegroundColor Green }
function Write-Warn    { param($Msg) Write-Host "[!] $Msg" -ForegroundColor Yellow }
function Write-Err     { param($Msg) Write-Host "[-] $Msg" -ForegroundColor Red }

function Show-Banner {
    Write-Host ""
    Write-Host "    +==========================================+" -ForegroundColor White
    Write-Host "    |            六眼  Rikugan                 |" -ForegroundColor White
    Write-Host "    |     Reverse Engineering AI Agent         |" -ForegroundColor White
    Write-Host "    |              IDA Pro                     |" -ForegroundColor White
    Write-Host "    +==========================================+" -ForegroundColor White
    Write-Host ""
}

# ── Detection ────────────────────────────────────────────────────────
function Test-IDA {
    # Registry
    $regPaths = @(
        "HKCU:\Software\Hex-Rays\IDA",
        "HKLM:\SOFTWARE\Hex-Rays\IDA"
    )
    foreach ($rp in $regPaths) {
        if (Test-Path $rp) { return $true }
    }
    # AppData user dir
    $idaDir = Join-Path $env:APPDATA "Hex-Rays\IDA Pro"
    if (Test-Path $idaDir) { return $true }
    # USERPROFILE\.idapro
    $idapro = Join-Path $HOME ".idapro"
    if (Test-Path $idapro) { return $true }
    # IDA in PATH
    if (Get-Command "ida64.exe" -ErrorAction SilentlyContinue) { return $true }
    if (Get-Command "idat64.exe" -ErrorAction SilentlyContinue) { return $true }
    return $false
}

# ── Prerequisites ────────────────────────────────────────────────────
function Test-Prerequisites {
    if (-not (Get-Command "git" -ErrorAction SilentlyContinue)) {
        Write-Err "git is required but not installed."
        Write-Err "Install from: https://git-scm.com/download/win"
        Write-Err "Or: winget install Git.Git"
        exit 1
    }
}

# ── Clone or update ──────────────────────────────────────────────────
function Install-Repository {
    $gitDir = Join-Path $InstallDir ".git"
    if (Test-Path $gitDir) {
        Write-Info "Updating existing installation at $InstallDir..."
        git -C $InstallDir fetch origin $Branch --quiet 2>$null
        git -C $InstallDir checkout $Branch --quiet 2>$null
        git -C $InstallDir reset --hard "origin/$Branch" --quiet 2>$null
        Write-Ok "Updated to latest $Branch"
    }
    else {
        if (Test-Path $InstallDir) {
            $backup = "${InstallDir}.bak.$(Get-Date -Format 'yyyyMMddHHmmss')"
            Write-Warn "$InstallDir exists but is not a git repo -- backing up to $backup"
            Rename-Item $InstallDir $backup
        }
        Write-Info "Cloning Rikugan into $InstallDir..."
        git clone --branch $Branch --depth 1 $RepoUrl $InstallDir --quiet 2>$null
        Write-Ok "Cloned successfully"
    }
}

# ── Run installers ───────────────────────────────────────────────────
function Install-IDA {
    $script = Join-Path $InstallDir "install_ida.bat"
    if (-not (Test-Path $script)) {
        Write-Err "install_ida.bat not found in $InstallDir"
        return $false
    }
    Write-Info "Running IDA Pro installer..."
    Write-Host ""
    Push-Location $InstallDir
    try {
        & cmd.exe /c $script
        $success = $LASTEXITCODE -eq 0
    }
    finally { Pop-Location }
    return $success
}

# ── Main ─────────────────────────────────────────────────────────────
Show-Banner
Test-Prerequisites

# Auto-detect if no target specified
if (-not $Target) {
    $hasIda = Test-IDA

    if ($hasIda) {
        $Target = "ida"
        Write-Ok "Detected IDA Pro"
    }
    else {
        Write-Warn "No IDA Pro installation detected."
        Write-Warn "Installing anyway."
        $Target = "ida"
    }
}

Write-Info "Target: $Target"
Write-Info "Install directory: $InstallDir"
Write-Host ""

Install-Repository
Write-Host ""

$failed = $false

switch ($Target) {
    "ida" {
        if (-not (Install-IDA)) { $failed = $true }
    }
}

Write-Host ""
if ($failed) {
    Write-Warn "Installation completed with errors. Check the output above."
}
else {
    Write-Ok "Rikugan installation complete!"
}
Write-Host "  Install location: $InstallDir" -ForegroundColor DarkGray
Write-Host "  To update later:  cd $InstallDir; git pull" -ForegroundColor DarkGray
Write-Host ""
