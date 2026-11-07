# Makes a "QT3 Microscope" shortcut on the desktop. Run it once in PowerShell:
#
#   powershell -ExecutionPolicy Bypass -File make_desktop_shortcut.ps1
#
# The shortcut starts QT3 Microscope with pythonw.exe, so no terminal window
# opens; what used to go to the terminal is in the Log section under the
# scope. Run it again after moving the folder or changing Python.
#
# Options:  -Setup TQLM     (default VQLM)
#           -Python <path to python.exe or pythonw.exe of the environment>

param(
    [string]$Setup = "VQLM",
    [string]$Python = ""
)

$app = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $Python) {
    if ($Setup -eq "VQLM") {
        $Python = "C:\Users\QT3 User Facility\qdl-utils\vqlm-home-venv\Scripts\pythonw.exe"
    } else {
        $Python = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
    }
}
# pythonw.exe is the same Python without a terminal window.
$Python = $Python -replace 'python\.exe$', 'pythonw.exe'
if (-not $Python -or -not (Test-Path $Python)) {
    Write-Host "Could not find pythonw.exe at '$Python'. Give it with -Python." -ForegroundColor Red
    exit 1
}

$main = Join-Path $app "main.py"
$icon = Join-Path $app "QT3logo.ico"
$desktop = [Environment]::GetFolderPath("Desktop")
$link = Join-Path $desktop "QT3 Microscope.lnk"

$shell = New-Object -ComObject WScript.Shell
$s = $shell.CreateShortcut($link)
$s.TargetPath = $Python
$s.Arguments = "`"$main`" --setup $Setup"
$s.WorkingDirectory = $app
if (Test-Path $icon) { $s.IconLocation = "$icon,0" }
$s.Description = "QT3 Microscope ($Setup)"
$s.Save()

Write-Host "Made $link"
Write-Host "  runs: $Python `"$main`" --setup $Setup"
