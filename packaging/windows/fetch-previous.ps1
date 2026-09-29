# Download the newest published BreadSched installer for the upgrade test.
#
# Picks the newest release (prereleases included) that carries a
# BreadSched-*-setup.exe and is not the version being built, downloads that
# installer with the release's SHA256SUMS, and refuses it unless its SHA-256
# matches the published line. Prints (as its only output) the installer's path, or
# nothing when no earlier installer has been published. Needs the gh CLI and GH_TOKEN. Usage:
#   pwsh packaging/windows/fetch-previous.ps1 <this-version> <out-dir>
param(
    [Parameter(Mandatory = $true)][string]$Version,
    [Parameter(Mandatory = $true)][string]$OutDir
)

$ErrorActionPreference = "Stop"
$repo = $env:GITHUB_REPOSITORY
$tags = gh api "repos/$repo/releases?per_page=50" --jq `
    '.[] | select(.draft | not) | select(any(.assets[]; .name | endswith("-setup.exe"))) | .tag_name'
if ($LASTEXITCODE -ne 0) { throw "could not list releases" }
$tag = @($tags | Where-Object { $_ -and $_ -ne "v$Version" }) | Select-Object -First 1
if (-not $tag) {
    Write-Host "No earlier installer has been published; the upgrade test is skipped."
    return
}

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$OutDir = (Resolve-Path $OutDir).Path
gh release download $tag --repo $repo --dir $OutDir --clobber `
    --pattern "BreadSched-*-setup.exe" --pattern "SHA256SUMS"
if ($LASTEXITCODE -ne 0) { throw "could not download $tag" }

$setup = Get-ChildItem (Join-Path $OutDir "BreadSched-*-setup.exe") | Select-Object -First 1
$line = Get-Content (Join-Path $OutDir "SHA256SUMS") |
    Where-Object { $_ -match "^([0-9a-f]{64})  $([regex]::Escape($setup.Name))$" } |
    Select-Object -First 1
if (-not $line) { throw "$tag's SHA256SUMS does not list $($setup.Name)" }
$expected = $line.Substring(0, 64)
$actual = (Get-FileHash -Algorithm SHA256 $setup.FullName).Hash.ToLowerInvariant()
if ($actual -ne $expected) { throw "$($setup.Name) does not match $tag's SHA256SUMS" }
Write-Host "Verified $($setup.Name) from $tag against its SHA256SUMS."
Write-Output $setup.FullName
