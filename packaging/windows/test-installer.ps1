# Install, exercise, upgrade in place, and uninstall the BreadSched Windows installer.
#
# CI runs this on a clean windows-latest runner, outside MSYS2, so the installed
# copy must work with nothing but its own runtime. With -Previous, it first installs
# that (previously published) installer, makes a book with it, installs this build
# over it, and requires the upgraded copy to report this version and still read the
# book, then uninstalls before the clean-install checks. Usage:
#   pwsh packaging/windows/test-installer.ps1 dist\windows\BreadSched-<version>-setup.exe
#   pwsh packaging/windows/test-installer.ps1 <new-setup.exe> -Previous <published-setup.exe>
param(
    [Parameter(Mandatory = $true)][string]$Installer,
    [string]$Previous = ""
)

$ErrorActionPreference = "Stop"
$Installer = (Resolve-Path $Installer).Path
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$dir = Join-Path $env:RUNNER_TEMP "BreadSched Test Install"
$work = Join-Path $env:RUNNER_TEMP "breadsched-installer-work"
New-Item -ItemType Directory -Force -Path $work | Out-Null
$uninstallKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\BreadSched"

function Install-Once([string]$Setup = $Installer, [string]$Target = $dir) {
    # NSIS takes /D last and unquoted, even with spaces in the path.
    $p = Start-Process -FilePath $Setup -ArgumentList "/S", "/D=$Target" -Wait -PassThru
    if ($p.ExitCode -ne 0) { throw "installer $Setup exited with $($p.ExitCode)" }
    foreach ($path in "breadsched.cmd", "Uninstall.exe", "runtime\bin\python.exe",
                      "runtime\bin\pythonw.exe") {
        if (-not (Test-Path (Join-Path $Target $path))) { throw "missing $path after install" }
    }
}

function Invoke-BreadSched {
    & (Join-Path $dir "breadsched.cmd") @args
    if ($LASTEXITCODE -ne 0) { throw "breadsched $args exited with $LASTEXITCODE" }
}

function Invoke-In([string]$Target) {
    $output = & (Join-Path $Target "breadsched.cmd") @args
    if ($LASTEXITCODE -ne 0) { throw "breadsched $args exited with $LASTEXITCODE" }
    return ($output -join "`n")
}

function Uninstall-From([string]$Target) {
    # _?= keeps the uninstaller in place so Start-Process can wait for it.
    $p = Start-Process -FilePath (Join-Path $Target "Uninstall.exe") -ArgumentList "/S", "_?=$Target" -Wait -PassThru
    if ($p.ExitCode -ne 0) { throw "uninstaller exited with $($p.ExitCode)" }
    Remove-Item -Force (Join-Path $Target "Uninstall.exe") -ErrorAction SilentlyContinue
    if (Test-Path (Join-Path $Target "runtime")) { throw "runtime left behind after uninstall" }
    if (Test-Path (Join-Path $Target "breadsched.cmd")) { throw "launcher left behind after uninstall" }
    if (Test-Path $uninstallKey) { throw "uninstall registration left behind" }
}

# Nothing from the build environment may leak into the installed copy.
$env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue

# This build's version, from its file name (BreadSched-<version>-setup.exe).
if ((Split-Path -Leaf $Installer) -notmatch '^BreadSched-(.+)-setup\.exe$') {
    throw "unexpected installer name $Installer"
}
$version = $Matches[1]

if ($Previous) {
    $Previous = (Resolve-Path $Previous).Path
    $upgradeDir = Join-Path $env:RUNNER_TEMP "BreadSched Upgrade Install"
    Write-Host "== upgrade from the previously published installer $(Split-Path -Leaf $Previous)"
    Install-Once $Previous $upgradeDir
    $oldVersion = Invoke-In $upgradeDir --version
    Write-Host "published: $oldVersion"
    $published = Join-Path $work "published.breadsched"
    Remove-Item -Force -ErrorAction SilentlyContinue $published
    Invoke-In $upgradeDir sample $published --as-of 2026-09-15 | Out-Null
    Invoke-In $upgradeDir verify $published | Out-Null

    Install-Once $Installer $upgradeDir
    $newVersion = Invoke-In $upgradeDir --version
    Write-Host "upgraded: $newVersion"
    if (-not $newVersion.StartsWith("breadsched $version ")) {
        throw "upgraded copy reports '$newVersion', expected $version"
    }
    # The book the published version made still verifies and reads after the
    # upgrade (output formats may differ between versions, so none is compared).
    Invoke-In $upgradeDir verify $published | Out-Null
    Invoke-In $upgradeDir accounts $published | Out-Null
    Uninstall-From $upgradeDir
    if (-not (Test-Path $published)) { throw "uninstall removed the book" }
}

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
Uninstall-From $dir
if (-not (Test-Path $book)) { throw "uninstall removed the book" }
$upgraded = if ($Previous) { ", upgrade from $(Split-Path -Leaf $Previous)" } else { "" }
Write-Host "Installer passed: clean install, CLI, desktop, upgrade, uninstall$upgraded."
