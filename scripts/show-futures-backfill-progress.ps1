param(
    [switch]$NoPause
)

$ErrorActionPreference = "Stop"
$workspace = Split-Path -Parent $PSScriptRoot
$runtime = Join-Path $workspace "runtime"
$futuresRoot = Join-Path $workspace "data\curated\futures"
$lockPath = Join-Path $runtime "continue-futures-backfill.lock"
$logPath = Join-Path $runtime "continue-futures-backfill.log"

# CFFEX open-day counts used by the 2015-2020 backfill plan.
$expectedByYear = [ordered]@{
    "2015" = 244
    "2016" = 244
    "2017" = 244
    "2018" = 243
    "2019" = 244
    "2020" = 243
}
$datasets = @("futures_limits", "futures_daily", "futures_settlements")
$expectedTotal = ($expectedByYear.Values | Measure-Object -Sum).Sum

function Test-BackfillRunning {
    if (-not (Test-Path -LiteralPath $lockPath -PathType Leaf)) {
        return $false
    }
    try {
        $stream = [System.IO.File]::Open(
            $lockPath,
            [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::ReadWrite,
            [System.IO.FileShare]::None
        )
        $stream.Dispose()
        return $false
    } catch [System.IO.IOException] {
        return $true
    }
}

function Get-DatasetDates {
    param([string]$Dataset)

    $providerRoot = Join-Path $futuresRoot "$Dataset\provider=tushare"
    if (-not (Test-Path -LiteralPath $providerRoot -PathType Container)) {
        return @()
    }
    return @(
        Get-ChildItem -LiteralPath $providerRoot -Directory -Filter "as_of_date=*" |
            Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName "data.parquet") -PathType Leaf } |
            ForEach-Object {
                $value = $_.Name.Substring("as_of_date=".Length)
                $parsed = [datetime]::MinValue
                if ([datetime]::TryParseExact(
                    $value,
                    "yyyy-MM-dd",
                    [Globalization.CultureInfo]::InvariantCulture,
                    [Globalization.DateTimeStyles]::None,
                    [ref]$parsed
                )) {
                    $parsed.Date
                }
            } |
            Where-Object { $_.Year -ge 2015 -and $_.Year -le 2020 } |
            Sort-Object -Unique
    )
}

Clear-Host
$isRunning = Test-BackfillRunning
Write-Host "QTrade futures backfill progress" -ForegroundColor Cyan
Write-Host "Workspace: $workspace"
Write-Host ("Status:    " + $(if ($isRunning) { "RUNNING" } else { "NOT RUNNING" })) `
    -ForegroundColor $(if ($isRunning) { "Green" } else { "Yellow" })
Write-Host "Scope:     2015-01-01 to 2020-12-31 ($expectedTotal trading days)"
Write-Host ""

$grandCompleted = 0
foreach ($dataset in $datasets) {
    $dates = @(Get-DatasetDates -Dataset $dataset)
    $completed = $dates.Count
    $grandCompleted += $completed
    $remaining = [Math]::Max(0, $expectedTotal - $completed)
    $percent = if ($expectedTotal) { 100.0 * $completed / $expectedTotal } else { 0 }
    $latest = if ($dates.Count) { $dates[-1].ToString("yyyy-MM-dd") } else { "none" }

    Write-Host ("{0,-20} {1,4}/{2}  {3,6:N1}%  remaining {4,4}  latest {5}" -f `
        $dataset, $completed, $expectedTotal, $percent, $remaining, $latest)
    $yearParts = foreach ($year in $expectedByYear.Keys) {
        $yearCompleted = @($dates | Where-Object Year -eq $year).Count
        $yearExpected = $expectedByYear["$year"]
        "${year}:$yearCompleted/$yearExpected"
    }
    Write-Host ("  " + ($yearParts -join "  ")) -ForegroundColor DarkGray
}

$grandExpected = $expectedTotal * $datasets.Count
$grandRemaining = [Math]::Max(0, $grandExpected - $grandCompleted)
$grandPercent = if ($grandExpected) { 100.0 * $grandCompleted / $grandExpected } else { 0 }
Write-Host ""
Write-Host ("TOTAL                {0,4}/{1}  {2,6:N1}%  remaining {3,4}" -f `
    $grandCompleted, $grandExpected, $grandPercent, $grandRemaining) -ForegroundColor Cyan

if (Test-Path -LiteralPath $logPath -PathType Leaf) {
    Write-Host ""
    Write-Host "Latest log entries:" -ForegroundColor Cyan
    Get-Content -LiteralPath $logPath -Tail 8 | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkGray }
}

Write-Host ""
Write-Host "This window is a snapshot. Run it again to refresh."
if (-not $NoPause) {
    Write-Host "Press any key to close..."
    $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
}
