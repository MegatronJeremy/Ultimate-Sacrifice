<#
.SYNOPSIS
    Ultimate Sacrifice launcher (PowerShell): sets up a venv, installs deps, then runs.
.DESCRIPTION
    On first run this creates a .venv, installs the package (and its dependencies) into it,
    and records a stamp file so subsequent runs skip straight to launching. Any arguments are
    passed through to the app.
.EXAMPLE
    .\run.ps1
.EXAMPLE
    .\run.ps1 --root "C:\Users\me" --provider claude_cli
.NOTES
    If you get an execution-policy error, launch with:
        powershell -ExecutionPolicy Bypass -File .\run.ps1
#>
[CmdletBinding()]
param(
    [switch]$Reinstall,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$AppArgs
)

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

$Venv  = Join-Path $PSScriptRoot '.venv'
$PyExe = Join-Path $Venv 'Scripts\python.exe'
$Stamp = Join-Path $Venv '.us-installed'

function Find-BasePython {
    # Prefer the py launcher, then python, then python3.
    foreach ($candidate in @(
        @{ Cmd = 'py';      Args = @('-3') },
        @{ Cmd = 'python';  Args = @() },
        @{ Cmd = 'python3'; Args = @() }
    )) {
        $cmd = Get-Command $candidate.Cmd -ErrorAction SilentlyContinue
        if ($cmd) { return @($cmd.Source) + $candidate.Args }
    }
    throw "No Python found. Install Python 3.11+ and ensure 'py' or 'python' is on your PATH."
}

if ($Reinstall -and (Test-Path $Venv)) {
    Write-Host '[setup] Removing existing virtual environment...' -ForegroundColor Yellow
    Remove-Item -Recurse -Force $Venv
}

if (-not (Test-Path $PyExe)) {
    Write-Host "[setup] Creating virtual environment in $Venv ..." -ForegroundColor Cyan
    $base = Find-BasePython
    & $base[0] $base[1..($base.Count - 1)] -m venv $Venv
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create the virtual environment.' }
}

if (-not (Test-Path $Stamp)) {
    Write-Host '[setup] Installing dependencies (first run only)...' -ForegroundColor Cyan
    & $PyExe -m pip install --upgrade pip
    & $PyExe -m pip install -e .
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
    'installed' | Out-File -FilePath $Stamp -Encoding ascii
}

& $PyExe -m ultimate_sacrifice @AppArgs
exit $LASTEXITCODE
