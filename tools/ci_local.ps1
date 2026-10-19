# Local CI for the protobot checkout.
#
# Everything here runs without a Minecraft server, except the last step, which
# the ``-Live`` switch adds on purpose: the in-game acceptance suite is the only
# check that can prove the library talks to a real server the way vanilla does,
# so it deserves a place next to the unit tests rather than in a wiki page.
#
#   .\tools\ci_local.ps1              # compile, imports, unit tests
#   .\tools\ci_local.ps1 -Live        # ... and the 23-case acceptance suite
#
# Exits 1 when any step fails, so a shell or an agent can gate on $LASTEXITCODE.

[CmdletBinding()]
param(
    # Also run tools\live_suite.py --run all against the test server.
    [switch]$Live,
    # Python to use. Defaults to the checkout's .venv, then python on PATH.
    [string]$Python,
    # Server log the suite reads for GrimAC alerts.
    [string]$ServerLog = "..\Test26.3Server\logs\latest.log",
    # Bridge endpoint, used to decide whether -Live can run at all.
    [string]$Bridge = "http://127.0.0.1:25963"
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Push-Location $repo
$steps = [System.Collections.Generic.List[object]]::new()

function Resolve-Python {
    if ($Python) { return $Python }
    $venv = Join-Path $repo ".venv\Scripts\python.exe"
    if (Test-Path $venv) { return $venv }
    $venv = Join-Path $repo ".venv\bin\python"
    if (Test-Path $venv) { return $venv }
    return "python"
}

function Invoke-Step {
    param(
        [string]$Name,
        [scriptblock]$Body,
        [switch]$AllowSkip
    )
    Write-Host ""
    Write-Host "== $Name" -ForegroundColor Cyan
    $started = Get-Date
    $ok = $true
    $note = ""
    try {
        & $Body
        if ($LASTEXITCODE -ne $null -and $LASTEXITCODE -ne 0) { $ok = $false }
    } catch {
        $ok = $false
        $note = $_.Exception.Message
    }
    $seconds = ((Get-Date) - $started).TotalSeconds
    $steps.Add([pscustomobject]@{
            Name    = $Name
            Ok      = $ok
            Skipped = $false
            Seconds = $seconds
            Note    = $note
        })
    return $ok
}

$python = Resolve-Python
Write-Host "protobot local CI" -ForegroundColor Green
Write-Host "  checkout: $repo"
Write-Host "  python:   $python"

try {
    Invoke-Step "byte-compile (protobot, tools, tests)" {
        & $python -m compileall -q protobot tools tests
    } | Out-Null

    Invoke-Step "import hygiene (tools\check_imports.py)" {
        & $python tools\check_imports.py
    } | Out-Null

    Invoke-Step "unit tests (no server, no network)" {
        # -b buffers whatever a test prints, so a passing run stays one block of
        # dots instead of asyncio's slow-callback noise from IsolatedAsyncioTestCase.
        & $python -m unittest discover -s tests -t . -p "test_*.py" -b
    } | Out-Null

    if ($Live) {
        $reachable = $false
        try {
            Invoke-RestMethod -Uri "$Bridge/status" -TimeoutSec 3 | Out-Null
            $reachable = $true
        } catch {
            $reachable = $false
        }
        if (-not $reachable) {
            Write-Host ""
            Write-Host "== acceptance suite skipped: no bridge at $Bridge" -ForegroundColor Yellow
            Write-Host "   start it with: Test26.3Server\start-server.bat"
            $steps.Add([pscustomobject]@{
                    Name    = "acceptance suite (23 cases)"
                    Ok      = $true
                    Skipped = $true
                    Seconds = 0.0
                    Note    = "no bridge at $Bridge"
                })
        } else {
            Invoke-Step "acceptance suite (23 cases, GrimAC on)" {
                & $python tools\live_suite.py --run all --server-log $ServerLog
            } | Out-Null
        }
    }

    Write-Host ""
    Write-Host "== summary" -ForegroundColor Cyan
    $failed = 0
    $total = 0.0
    foreach ($step in $steps) {
        $total += $step.Seconds
        if ($step.Skipped) {
            $mark = "skip"
            $color = "Yellow"
        } elseif ($step.Ok) {
            $mark = "ok  "
            $color = "Green"
        } else {
            $mark = "FAIL"
            $color = "Red"
            $failed += 1
        }
        $line = "  {0}  {1,-44} {2,6:N1}s" -f $mark, $step.Name, $step.Seconds
        if ($step.Note) { $line += "  ($($step.Note))" }
        Write-Host $line -ForegroundColor $color
    }
    Write-Host ("  total {0:N1}s" -f $total)
    if ($failed -gt 0) {
        Write-Host "  $failed step(s) failed" -ForegroundColor Red
        exit 1
    }
    Write-Host "  all steps passed" -ForegroundColor Green
    exit 0
} finally {
    Pop-Location
}
