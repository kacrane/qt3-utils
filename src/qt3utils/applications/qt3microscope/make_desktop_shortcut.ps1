# Makes "QT3 Microscope" shortcuts on the desktop and in the Start menu.
# Run it once in PowerShell:
#
#   powershell -ExecutionPolicy Bypass -File make_desktop_shortcut.ps1
#
# The shortcuts start QT3 Microscope with pythonw.exe, so no terminal window
# opens; what used to go to the terminal is in the Log section under the
# scope. Each shortcut carries the program's app ID (QT3.Microscope, the
# same as APP_ID in main.py), so the taskbar shows "QT3 Microscope" with the
# QT3 logo rather than Python or Tk, and it can be pinned to the taskbar.
# Windows looks the app ID up in the Start menu, which is why a Start menu
# shortcut is made too. Run it again after moving the folder or changing Python.
#
# Options:  -Setup TQLM     (default VQLM)
#           -Python <path to python.exe or pythonw.exe of the environment>

param(
    [string]$Setup = "VQLM",
    [string]$Python = ""
)

$AppId = "QT3.Microscope"
$app = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $Python) {
    if ($Setup -eq "VQLM") {
        $Python = "C:\Users\QT3 User Facility\qdl-utils\vqlm-home-venv\Scripts\pythonw.exe"
    } elseif ($Setup -eq "TQLM") {
        # The QTLM conda environment that qt3home.bat activates.
        $Python = "C:\anaconda\files\envs\QTLM\pythonw.exe"
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

# Writes the app ID into a shortcut (the WScript.Shell object cannot).
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
namespace QT3Shortcut {
    [StructLayout(LayoutKind.Sequential, Pack = 4)]
    public struct PropertyKey { public Guid fmtid; public uint pid; }
    [StructLayout(LayoutKind.Sequential)]
    public struct PropVariant { public ushort vt; public ushort r1; public ushort r2; public ushort r3; public IntPtr p; public IntPtr p2; }
    [ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface IPropertyStore {
        void GetCount(out uint count);
        void GetAt(uint index, out PropertyKey key);
        void GetValue(ref PropertyKey key, out PropVariant value);
        void SetValue(ref PropertyKey key, ref PropVariant value);
        void Commit();
    }
    [ComImport, Guid("00021401-0000-0000-C000-000000000046")]
    public class CShellLink { }
    public static class Link {
        public static void SetAppId(string path, string appId) {
            IPersistFile file = (IPersistFile)new CShellLink();
            file.Load(path, 2);
            IPropertyStore store = (IPropertyStore)file;
            PropertyKey key = new PropertyKey();
            key.fmtid = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3");
            key.pid = 5;
            PropVariant value = new PropVariant();
            value.vt = 31;
            value.p = Marshal.StringToCoTaskMemUni(appId);
            try {
                store.SetValue(ref key, ref value);
                store.Commit();
                file.Save(path, true);
            } finally {
                Marshal.FreeCoTaskMem(value.p);
                Marshal.ReleaseComObject(file);
            }
        }
    }
}
"@

$main = Join-Path $app "main.py"
$icon = Join-Path $app "QT3logo.ico"
$places = @(
    [Environment]::GetFolderPath("Desktop"),
    (Join-Path ([Environment]::GetFolderPath("StartMenu")) "Programs")
)
$shell = New-Object -ComObject WScript.Shell
foreach ($dir in $places) {
    $link = Join-Path $dir "QT3 Microscope.lnk"
    $s = $shell.CreateShortcut($link)
    $s.TargetPath = $Python
    $s.Arguments = "`"$main`" --setup $Setup"
    $s.WorkingDirectory = $app
    if (Test-Path $icon) { $s.IconLocation = "$icon,0" }
    $s.Description = "QT3 Microscope ($Setup)"
    $s.Save()
    try {
        [QT3Shortcut.Link]::SetAppId($link, $AppId)
        Write-Host "Made $link"
    } catch {
        Write-Host "Made $link, but could not set its app ID: $_" -ForegroundColor Yellow
    }
}
Write-Host "  runs: $Python `"$main`" --setup $Setup"
Write-Host "If an old pinned Python or Tk button is on the taskbar, unpin it, start QT3 Microscope"
Write-Host "from the new shortcut, then right-click its taskbar button and choose Pin to taskbar."
