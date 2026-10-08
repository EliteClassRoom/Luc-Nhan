#Requires -Version 5.1
<#
.SYNOPSIS
    Local CI simulation for Windows — mirrors the GitHub Actions pipeline.
.DESCRIPTION
    PowerShell equivalent of ci-local.sh. Runs the same 5 checks:
    ruff format, ruff lint, mypy, pytest, and desloppify objective score gate.

    On Windows with uv, dev tools (ruff, mypy, pytest) run via `uvx` so they
    do not depend on a pip-enabled venv. Python is resolved through
    `uv run python` first (matches CI's pinned 3.11), then falls back to a
    pip-capable system python.
.PARAMETER Fix
    Auto-fix ruff formatting and lint issues instead of just checking.
.EXAMPLE
    .\ci-local.ps1
    .\ci-local.ps1 -Fix
#>
[CmdletBinding()]
param(
    [switch]$Fix
)

$ErrorActionPreference = "Continue"   # external tools set $LASTEXITCODE; check manually

# Force UTF-8 output so the ✔/✘/⚠/▶ glyphs render in the Windows console
# (defaults to the system OEM codepage, e.g. cp1252, which mangles them).
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$OutputEncoding = [System.Text.UTF8Encoding]::new()

Set-Location -Path $PSScriptRoot

# ── Counters ───────────────────────────────────────────────────────────────────
$script:Pass = 0
$script:Fail = 0
$script:Results = [System.Collections.Generic.List[string]]::new()

function Add-Ok {
    param([string]$Message)
    $script:Pass++
    $script:Results.Add("✔ $Message")
}

function Add-Fail {
    param([string]$Label, [string]$Detail)
    $script:Fail++
    $script:Results.Add("✘ ${Label}: ${Detail}")
}

function Add-Note {
    param([string]$Message)
    $script:Results.Add("⚠ $Message")
}

function Write-Info {
    param([string]$Message)
    Write-Host "▶ $Message" -ForegroundColor Yellow
}

# ── Python resolution (for mypy + pytest) ──────────────────────────────────────
# Prefer `uv run python` (matches CI's pinned 3.11). Otherwise accept a system
# python that has a working pip module — a bare venv without pip cannot run
# `python -m pytest` after install.
function Test-PythonUsable {
    param([string]$Exe, [string[]]$PreArgs = @())
    & $Exe @PreArgs -c "import sys, importlib.util; sys.exit(0 if importlib.util.find_spec('pip') else 1)" 2>$null
    return $LASTEXITCODE -eq 0
}

function Get-PythonCmd {
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        & uv run python --version *> $null
        if ($LASTEXITCODE -eq 0) { return @("uv", "run", "python") }
    }
    foreach ($candidate in @(@("python"), @("python3"), @("py"), @("py", "-3"))) {
        $exe = $candidate[0]
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        $preArgs = if ($candidate.Count -gt 1) { $candidate[1..($candidate.Length - 1)] } else { @() }
        if (Test-PythonUsable -Exe $exe -PreArgs $preArgs) { return $candidate }
    }
    return $null
}

# Dev-tool runner: `uvx <tool> ...` when uv is present (no pip dependency),
# otherwise `<python> -m <tool> ...` against a pip-capable interpreter.
# NOTE: parameter is $ToolArgs, not $Args — $args is a PowerShell automatic
# variable and shadowing it breaks array splatting.
function Invoke-DevTool {
    param([string]$Tool, [string[]]$ToolArgs = @())
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        & uvx $Tool @ToolArgs
    } else {
        $py = Get-PythonCmd
        if (-not $py) { Write-Host "ERROR: No python for $Tool" -ForegroundColor Red; return 1 }
        & $py[0] @($py[1..($py.Length - 1)]) -m $Tool @ToolArgs
    }
}

$Py = Get-PythonCmd

# ── 1. Ruff — format check ────────────────────────────────────────────────────
Write-Info "[1/5] Ruff format..."
if ($Fix) {
    Invoke-DevTool -Tool "ruff" -ToolArgs @("format", "lucnhan/")
    if ($LASTEXITCODE -eq 0) { Add-Ok "ruff format (auto-fixed)" } else { Add-Fail "ruff format" "failed" }
} else {
    Invoke-DevTool -Tool "ruff" -ToolArgs @("format", "--check", "lucnhan/")
    if ($LASTEXITCODE -eq 0) { Add-Ok "ruff format" } else { Add-Fail "ruff format" "run with -Fix to auto-fix" }
}

# ── 2. Ruff — lint (config in pyproject.toml) ─────────────────────────────────
Write-Info "[2/5] Ruff lint..."
if ($Fix) {
    Invoke-DevTool -Tool "ruff" -ToolArgs @("check", "lucnhan/", "--fix")
    if ($LASTEXITCODE -eq 0) { Add-Ok "ruff lint (auto-fixed)" } else { Add-Fail "ruff lint" "see above" }
} else {
    Invoke-DevTool -Tool "ruff" -ToolArgs @("check", "lucnhan/")
    if ($LASTEXITCODE -eq 0) { Add-Ok "ruff lint" } else { Add-Fail "ruff lint" "see above" }
}

# ── 3. Mypy — core modules only (config in pyproject.toml) ────────────────────
Write-Info "[3/5] Mypy (core + providers)..."
$mypyOutput = (Invoke-DevTool -Tool "mypy" -ToolArgs @("lucnhan/core", "lucnhan/providers", "--pretty") 2>&1) -join "`n"
$mypyOk = $LASTEXITCODE -eq 0

if ($mypyOk) {
    Add-Ok "mypy"
} else {
    # Only count as failure if there are actual errors (not just notes).
    $errorCount = ([regex]::Matches($mypyOutput, ": error:")).Count
    if ($errorCount -gt 0) {
        Write-Host $mypyOutput
        Add-Fail "mypy" "$errorCount error(s)"
    } else {
        Add-Ok "mypy (warnings only)"
    }
}

# ── 4. Pytest ──────────────────────────────────────────────────────────────────
# NOTE: pytest must run inside the PROJECT venv (not `uvx pytest`) because tests
# import lucnhan_plugin.py which needs PySide6 (a project dev dependency).
# `uvx pytest` would create an isolated ephemeral env missing PySide6.
Write-Info "[4/5] Pytest..."
if ($Py) {
    & $Py[0] @($Py[1..($Py.Length - 1)]) -m pytest --version *> $null
    if ($LASTEXITCODE -ne 0) {
        # Ensure pytest is available in the resolved python env.
        & $Py[0] @($Py[1..($Py.Length - 1)]) -m pip install --quiet pytest 2>$null
    }
    & $Py[0] @($Py[1..($Py.Length - 1)]) -m pytest tests/ --tb=short -q
    if ($LASTEXITCODE -eq 0) { Add-Ok "pytest" } else { Add-Fail "pytest" "see above" }
} else {
    Add-Note "pytest: no python available, skipped"
}

# ── 5. Desloppify — objective score gate ───────────────────────────────────────
Write-Info "[5/5] Desloppify (objective score)..."

$desloppifyCmd = $null
if (Get-Command uv -ErrorAction SilentlyContinue) {
    $desloppifyCmd = @("uvx", "desloppify")
} elseif (Get-Command desloppify -ErrorAction SilentlyContinue) {
    $desloppifyCmd = @("desloppify")
}

if ($desloppifyCmd) {
    $exe = $desloppifyCmd[0]
    $args = $desloppifyCmd[1..($desloppifyCmd.Length - 1)]
    & $exe @args scan --profile objective --no-badge 2>&1 | Select-Object -Last 5 | Out-Host

    # Read the objective score from the desloppify query cache.
    $score = 0.0
    $queryPath = ".desloppify\query.json"
    if (Test-Path $queryPath) {
        try {
            $query = Get-Content $queryPath -Raw | ConvertFrom-Json
            if ($query.objective_score) { $score = [double]$query.objective_score }
        } catch {
            $score = 0.0
        }
    }

    $baseline = 89.0
    if ($score -lt ($baseline - 0.5)) {
        Add-Fail "desloppify" "objective score $score < baseline $baseline"
    } else {
        Add-Ok "desloppify (objective: $score/100, baseline: $baseline)"
    }
} else {
    Add-Note "desloppify: not found, skipped"
}

# ── Summary ────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "── CI Results ──────────────────────────────────────────" -ForegroundColor White
foreach ($r in $script:Results) {
    if ($r.StartsWith("✔")) {
        Write-Host "  $r" -ForegroundColor Green
    } elseif ($r.StartsWith("✘")) {
        Write-Host "  $r" -ForegroundColor Red
    } else {
        Write-Host "  $r" -ForegroundColor Yellow
    }
}
Write-Host ""

if ($script:Fail -gt 0) {
    Write-Host "FAILED — $($script:Fail) check(s) failed, $($script:Pass) passed" -ForegroundColor Red
    exit 1
} else {
    Write-Host "ALL PASSED — $($script:Pass) checks" -ForegroundColor Green
    exit 0
}
