# Stage a tested installer and its checksum for the release publisher.
#
# Copies the installer into OutDir and writes "<sha256>  <name>" plus a single LF
# beside it: the same form as SHA256SUMS, which the Linux publisher checks with
# `sha256sum --check` without running anything it receives. MSYS2's sha256sum
# marks binary mode ("*name") on Windows, so the line is written here instead.
# CI runs this on every pull request so the release path is exercised before a
# release depends on it. Usage:
#   pwsh packaging/windows/stage-release.ps1 dist\windows\BreadSched-<version>-setup.exe release-installer
param(
    [Parameter(Mandatory = $true)][string]$Installer,
    [Parameter(Mandatory = $true)][string]$OutDir
)

$ErrorActionPreference = "Stop"
$Installer = (Resolve-Path $Installer).Path
$name = Split-Path -Leaf $Installer
if ($name -notmatch '^BreadSched-[0-9]+\.[0-9]+\.[0-9]+(a[0-9]+)?-setup\.exe$') {
    throw "unexpected installer name $name"
}
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$OutDir = (Resolve-Path $OutDir).Path
Copy-Item $Installer $OutDir
$hash = (Get-FileHash -Algorithm SHA256 $Installer).Hash.ToLowerInvariant()
[IO.File]::WriteAllText((Join-Path $OutDir "$name.sha256"), "$hash  $name`n")
Write-Host "Staged $name ($hash)"
