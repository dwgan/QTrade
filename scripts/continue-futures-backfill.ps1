param(
    [int]$MaxAttemptsPerYear = 12
)

$ErrorActionPreference = "Stop"
$workspace = Split-Path -Parent $PSScriptRoot
$runtime = Join-Path $workspace "runtime"
$python = Join-Path $workspace ".venv\Scripts\python.exe"
$config = Join-Path $workspace "config\base.yaml"
$logPath = Join-Path $runtime "continue-futures-backfill.log"
$lockPath = Join-Path $runtime "continue-futures-backfill.lock"
$expectedTradingDays = @{
    2015 = 244
    2016 = 244
    2017 = 244
    2018 = 243
    2019 = 244
    2020 = 243
}

New-Item -ItemType Directory -Path $runtime -Force | Out-Null

try {
    $lockStream = [System.IO.File]::Open(
        $lockPath,
        [System.IO.FileMode]::OpenOrCreate,
        [System.IO.FileAccess]::ReadWrite,
        [System.IO.FileShare]::None
    )
} catch [System.IO.IOException] {
    Write-Output "A QTrade futures backfill is already running."
    exit 2
}

function Write-BackfillLog {
    param([string]$Message)
    $line = "[$(Get-Date -Format o)] $Message"
    Write-Output $line
    Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8
}

function Initialize-ProviderEnvironment {
    if ([string]::IsNullOrWhiteSpace($env:TUSHARE_TOKEN)) {
        $secureToken = Read-Host "Tushare token" -AsSecureString
        $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)
        try {
            $env:TUSHARE_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
        } finally {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
        }
    }
    if ([string]::IsNullOrWhiteSpace($env:TUSHARE_TOKEN)) {
        throw "Tushare token is required."
    }
    if ([string]::IsNullOrWhiteSpace($env:TUSHARE_API_URL)) {
        $env:TUSHARE_API_URL = "https://ts.gyzcloud.top/api"
    }
    if ([string]::IsNullOrWhiteSpace($env:TUSHARE_MCP_URL)) {
        $env:TUSHARE_MCP_URL = "https://ts.gyzcloud.top/mcp/token=$($env:TUSHARE_TOKEN)"
    }
}

function Invoke-BackfillYear {
    param(
        [int]$Year,
        [string]$Datasets
    )

    $start = "${Year}-01-01"
    $end = "${Year}-12-31"
    for ($attempt = 1; $attempt -le $MaxAttemptsPerYear; $attempt++) {
        Write-BackfillLog "start year=$Year datasets=$Datasets attempt=$attempt"
        & $python -m qtrade --config $config futures backfill `
            --start $start --end $end --datasets $Datasets 2>&1 | `
            ForEach-Object { Write-BackfillLog $_ }
        if ($LASTEXITCODE -eq 0) {
            Write-BackfillLog "complete year=$Year datasets=$Datasets"
            return
        }
        Write-BackfillLog "retry-after-420-seconds year=$Year datasets=$Datasets"
        Start-Sleep -Seconds 420
    }
    throw "Backfill stopped after $MaxAttemptsPerYear attempts: year=$Year datasets=$Datasets"
}

function Test-DatasetYearComplete {
    param(
        [string]$Dataset,
        [int]$Year
    )

    $providerRoot = Join-Path $workspace `
        "data\curated\futures\$Dataset\provider=tushare"
    if (-not (Test-Path -LiteralPath $providerRoot -PathType Container)) {
        return $false
    }
    $count = @(
        Get-ChildItem -LiteralPath $providerRoot -Directory `
            -Filter "as_of_date=$Year-*" |
            Where-Object {
                Test-Path -LiteralPath (Join-Path $_.FullName "data.parquet") `
                    -PathType Leaf
            }
    ).Count
    return $count -ge $expectedTradingDays[$Year]
}

try {
    Initialize-ProviderEnvironment
    Set-Location -LiteralPath $workspace
    foreach ($year in 2020..2015) {
        if (Test-DatasetYearComplete -Dataset "futures_limits" -Year $year) {
            Write-BackfillLog "skip-complete year=$year datasets=futures_limits"
        } else {
            Invoke-BackfillYear -Year $year -Datasets "futures_limits"
        }
    }
    foreach ($year in 2020..2015) {
        Invoke-BackfillYear -Year $year -Datasets "futures_daily"
    }
    foreach ($year in 2020..2015) {
        Invoke-BackfillYear -Year $year -Datasets "futures_settlements"
    }
    Write-BackfillLog "all requested futures backfills complete"
} catch {
    Write-BackfillLog "fatal: $($_.Exception.Message)"
    exit 1
} finally {
    if ($lockStream) {
        $lockStream.Dispose()
    }
}
