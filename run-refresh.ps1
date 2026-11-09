# Scheduled wrapper for refresh_data.ps1.
#
# Registered as the Windows Task Scheduler job "Cost Reconciler - Refresh Data",
# which runs every morning. This wrapper exists so each unattended run is logged
# to refresh_data.log in this folder — if a run ever fails (most likely a git
# push auth problem), it's visible there instead of failing silently.
#
# refresh_data.ps1 is run in a CHILD PowerShell process so its `exit` statements
# (it exits 0 when nothing changed) can't abort this wrapper's logging. The child
# output is captured and written as UTF-8 (Windows PowerShell's >> operator would
# write UTF-16 and garble the log).

$root   = $PSScriptRoot
$log    = Join-Path $root 'refresh_data.log'
$script = Join-Path $root 'refresh_data.ps1'

$out  = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $script 2>&1 | Out-String
$code = $LASTEXITCODE

$entry = "===== Refresh run $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') =====`r`n" +
         $out + "exit code: $code`r`n`r`n"
Add-Content -Path $log -Value $entry -Encoding UTF8
