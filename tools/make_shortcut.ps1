# Creates the desktop shortcut "MocapSync 마스터" that starts the PC master + dashboard.
#
#   powershell -ExecutionPolicy Bypass -File tools\make_shortcut.ps1
#
# Non-ASCII text is built from code points so this file stays ASCII
# (Windows PowerShell 5.1 reads BOM-less scripts in the ANSI code page).
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$py = Join-Path $repo '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { throw "python not found: $py" }

$master = [string]::new([char[]](0xB9C8, 0xC2A4, 0xD130))          # 마스터
$desc = [string]::new([char[]](0xC544, 0xC774, 0xD3F0, 0x0020, 0xB3D9, 0xC2DC, 0x0020, 0xCD2C, 0xC601))  # 아이폰 동시 촬영
$lnk = Join-Path ([Environment]::GetFolderPath('Desktop')) "MocapSync $master.lnk"

$sh = New-Object -ComObject WScript.Shell
$s = $sh.CreateShortcut($lnk)
$s.TargetPath = $py
$s.Arguments = 'server\master.py'
$s.WorkingDirectory = $repo
$s.Description = "MocapSync $master - $desc"
$s.IconLocation = "$env:SystemRoot\System32\shell32.dll,203"
$s.WindowStyle = 7        # minimized: the console keeps running in the taskbar
$s.Save()
Write-Output $lnk
