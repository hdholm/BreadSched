# Install, exercise, upgrade in place, and uninstall the BreadSched Windows installer.
#
# CI runs this on a clean windows-latest runner, outside MSYS2, so the installed
# copy must work with nothing but its own runtime. Usage:
#   pwsh packaging/windows/test-installer.ps1 dist\windows\BreadSched-<version>-setup.exe
param([Parameter(Mandatory = $true)][string]$Installer)

$ErrorActionPreference = "Stop"
$Installer = (Resolve-Path $Installer).Path
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$dir = Join-Path $env:RUNNER_TEMP "BreadSched Test Install"
$work = Join-Path $env:RUNNER_TEMP "breadsched-installer-work"
New-Item -ItemType Directory -Force -Path $work | Out-Null

function Install-Once {
    # NSIS takes /D last and unquoted, even with spaces in the path.
    $p = Start-Process -FilePath $Installer -ArgumentList "/S", "/D=$dir" -Wait -PassThru
    if ($p.ExitCode -ne 0) { throw "installer exited with $($p.ExitCode)" }
    foreach ($path in "breadsched.cmd", "Uninstall.exe", "runtime\bin\python.exe",
                      "runtime\bin\pythonw.exe") {
        if (-not (Test-Path (Join-Path $dir $path))) { throw "missing $path after install" }
    }
}

function Invoke-BreadSched {
    & (Join-Path $dir "breadsched.cmd") @args
    if ($LASTEXITCODE -ne 0) { throw "breadsched $args exited with $LASTEXITCODE" }
}

# Nothing from the build environment may leak into the installed copy.
$env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue

Write-Host "== clean install"
Install-Once
Invoke-BreadSched --version
$book = Join-Path $work "household.breadsched"
Remove-Item -Force -ErrorAction SilentlyContinue $book
Invoke-BreadSched sample $book --as-of 2026-09-15
Invoke-BreadSched verify $book
Invoke-BreadSched accounts $book
Invoke-BreadSched guide --list

Write-Host "== desktop interface"
$report = Join-Path $work "gtk-smoke.json"
& (Join-Path $dir "runtime\bin\python.exe") (Join-Path $root "scripts\flatpak_gtk_smoke.py") $book $report
if ($LASTEXITCODE -ne 0) { throw "GTK smoke exited with $LASTEXITCODE" }
Get-Content $report

Write-Host "== upgrade in place keeps the book"
Install-Once
Invoke-BreadSched verify $book

Write-Host "== uninstall"
# _?= keeps the uninstaller in place so Start-Process can wait for it.
$p = Start-Process -FilePath (Join-Path $dir "Uninstall.exe") -ArgumentList "/S", "_?=$dir" -Wait -PassThru
if ($p.ExitCode -ne 0) { throw "uninstaller exited with $($p.ExitCode)" }
Remove-Item -Force (Join-Path $dir "Uninstall.exe") -ErrorAction SilentlyContinue
if (Test-Path (Join-Path $dir "runtime")) { throw "runtime left behind after uninstall" }
if (Test-Path (Join-Path $dir "breadsched.cmd")) { throw "launcher left behind after uninstall" }
if (Test-Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\BreadSched") {
    throw "uninstall registration left behind"
}
if (-not (Test-Path $book)) { throw "uninstall removed the book" }
Write-Host "Installer passed: clean install, CLI, desktop, upgrade, uninstall."
