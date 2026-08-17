[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string[]]$InputPath,

    [Parameter(Mandatory = $true)]
    [string]$OwnerEmail,

    [int]$Limit = 20,
    [string]$Model = "auto",
    [string]$SecondModel = "",
    [string]$ReconcilerModel = "",

    # The database is a derivative of the mailbox. Keeping it outside the
    # repository removes any chance of committing it.
    [string]$Database = "",

    [switch]$DryRun,
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not $Database) {
    $Database = Join-Path $ProjectRoot "data\knowledge.db"
}
$TemporaryApiKey = $false

if (-not (Test-Path $Python)) {
    throw "Python environment not found. Follow README.md installation steps first."
}

Push-Location $ProjectRoot
try {
    & $Python -m email_kb --db $Database ingest @InputPath
    if ($LASTEXITCODE -ne 0) {
        throw "Email import failed."
    }

    if (-not $DryRun -and -not $env:CURSOR_API_KEY) {
        $SecureKey = Read-Host "Cursor API Key" -AsSecureString
        $Pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureKey)
        try {
            $env:CURSOR_API_KEY =
                [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Pointer)
            $TemporaryApiKey = $true
        }
        finally {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Pointer)
        }
    }

    $AnalyzeArguments = @(
        "-m", "email_kb",
        "--db", $Database,
        "analyze",
        "--owner-email", $OwnerEmail,
        "--model", $Model
    )
    if ($SecondModel) {
        $AnalyzeArguments += @("--second-model", $SecondModel)
    }
    if ($ReconcilerModel) {
        $AnalyzeArguments += @("--reconciler-model", $ReconcilerModel)
    }
    if ($Limit -gt 0) {
        $AnalyzeArguments += @("--limit", $Limit)
    }
    if ($DryRun) {
        $AnalyzeArguments += "--dry-run"
    }
    if ($Force) {
        $AnalyzeArguments += "--force"
    }

    & $Python @AnalyzeArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Cursor analysis failed."
    }

    & $Python -m email_kb --db $Database report
    if ($LASTEXITCODE -ne 0) {
        throw "Quality report failed."
    }

    if (-not $DryRun) {
        & $Python -m email_kb --db $Database index
        if ($LASTEXITCODE -ne 0) {
            throw "Building the retrieval index failed."
        }
    }

    # Last, so the closing output says what still needs doing.
    & $Python -m email_kb --db $Database doctor
}
finally {
    if ($TemporaryApiKey) {
        Remove-Item Env:CURSOR_API_KEY -ErrorAction SilentlyContinue
    }
    Pop-Location
}
