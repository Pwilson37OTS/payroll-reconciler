# Refresh the bundled data/ files from the SharePoint-synced payroll folder and
# push to GitHub so the Streamlit Cloud deploy picks up the latest payroll
# register + C2C exports.
#
# Usage:
#   .\refresh_data.ps1
# or just double-click the file in Explorer.
#
# Safe to run repeatedly — if nothing changed since last push, it skips the commit.
#
# The payroll register is exported with a timestamped name
# (Payroll_Register_Export_by_Date_Range_<stamp>.xlsx); this copies the NEWEST one
# to the stable name data/payroll_register.xlsx that the app looks for. The C2C
# files already have stable names.

$ErrorActionPreference = "Stop"

$repo = $PSScriptRoot
$dst  = Join-Path $repo "data"

# Source = the SharePoint-synced payroll-audit folder in this user's OneDrive.
# Override by setting $env:PAYROLL_AUDIT_DIR before running, if your sync path
# differs.
$src = $env:PAYROLL_AUDIT_DIR
if (-not $src) {
    $src = Join-Path $env:USERPROFILE "OakTree Software, Inc d b a OakTree Staffing\Management - PBW Agents\OakTree Operations\_shared\payroll-audit"
}

if (-not (Test-Path $src)) {
    Write-Error "Payroll-audit folder not found: $src`nSet `$env:PAYROLL_AUDIT_DIR to the correct path and re-run."
}
if (-not (Test-Path $dst)) { New-Item -ItemType Directory -Force $dst | Out-Null }

Set-Location $repo
Write-Host "Copying source files from the synced payroll folder..." -ForegroundColor Cyan

# Newest payroll register -> stable name.
$register = Get-ChildItem -Path $src -Filter "Payroll_Register_Export_*.xlsx" |
    Where-Object { $_.Name -notlike "~$*" } |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $register) {
    Write-Error "No Payroll_Register_Export_*.xlsx found in $src"
}
Copy-Item $register.FullName (Join-Path $dst "payroll_register.xlsx") -Force
Write-Host "  payroll_register.xlsx  <-  $($register.Name)"

# C2C files (stable names).
foreach ($name in @("C2C - CTS.xlsx", "C2C - OTS.xlsx")) {
    $path = Join-Path $src $name
    if (Test-Path $path) {
        Copy-Item $path (Join-Path $dst $name) -Force
        Write-Host "  $name"
    } else {
        Write-Host "  (skipped, not found) $name" -ForegroundColor Yellow
    }
}

# Stage and only commit if something actually changed.
git add data/ | Out-Null
$status = git status --porcelain data/
if (-not $status) {
    Write-Host ""
    Write-Host "No changes since last push. Cloud app already has the latest." -ForegroundColor Yellow
    exit 0
}

Write-Host ""
Write-Host "Changes to push:" -ForegroundColor Cyan
$status | ForEach-Object { Write-Host "  $_" }

$stamp = Get-Date -Format "yyyy-MM-dd HH:mm"
git commit -m "Refresh data $stamp" | Out-Null
git push

Write-Host ""
Write-Host "Pushed. Streamlit Cloud will auto-redeploy in ~30 seconds." -ForegroundColor Green
