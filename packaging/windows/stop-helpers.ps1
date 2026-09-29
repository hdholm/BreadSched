# Stop background helpers that run from a BreadSched installation's runtime.
#
# GLib starts gdbus.exe from the bundled runtime as the D-Bus session bus. It
# outlives the application and keeps files under runtime\ open, so an upgrade
# could not replace the runtime and an uninstall could not remove it. The
# installer and uninstaller run this before touching runtime\. It stops only
# gdbus.exe processes whose executable is inside this installation's runtime,
# never another program's. Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File stop-helpers.ps1 <install-dir>
param([Parameter(Mandatory = $true)][string]$Dir)

$runtime = (Join-Path $Dir "runtime").TrimEnd("\") + "\"
$helpers = @(Get-CimInstance Win32_Process -Filter "Name = 'gdbus.exe'" |
    Where-Object {
        $_.ExecutablePath -and
        $_.ExecutablePath.StartsWith($runtime, [StringComparison]::OrdinalIgnoreCase)
    })
foreach ($helper in $helpers) {
    Invoke-CimMethod -InputObject $helper -MethodName Terminate | Out-Null
    Wait-Process -Id $helper.ProcessId -Timeout 10 -ErrorAction SilentlyContinue
    Write-Output "Stopped $($helper.ExecutablePath) ($($helper.ProcessId))"
}
