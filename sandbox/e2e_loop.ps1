# Runs the whole-window e2e file N times in a row (default 20); every run's stdout / stderr goes to sandbox\e2e_loop\.
param([int]$Runs = 20)
$root = Split-Path -Parent $PSScriptRoot
$out = Join-Path $PSScriptRoot "e2e_loop"
New-Item -ItemType Directory -Force $out | Out-Null
$py = Join-Path $root ".venv\Scripts\python.exe"
$failed = 0
for ($i = 1; $i -le $Runs; $i++) {
    $started = Get-Date
    $log = Join-Path $out "run$i.out"
    $err = Join-Path $out "run$i.err"
    $p = Start-Process -FilePath $py -ArgumentList @("-m", "pytest", "tests/test_app_e2e.py", "-q", "-p", "no:cacheprovider") -WorkingDirectory $root -RedirectStandardOutput $log -RedirectStandardError $err -NoNewWindow -PassThru
    $null = $p.Handle  # (without it, ExitCode comes back empty from Start-Process -PassThru)
    if (-not $p.WaitForExit(300000)) { $p.Kill(); $code = "timeout" } else { $code = $p.ExitCode }
    $seconds = [int]((Get-Date) - $started).TotalSeconds
    $errBytes = (Get-Item $err).Length
    $last = (Get-Content $log | Select-Object -Last 1)
    if ($code -ne 0 -or $errBytes -ne 0) { $failed++ }
    "run $i : exit=$code  ${seconds}s  stderr=${errBytes}B  $last"
}
"failed runs: $failed of $Runs"
