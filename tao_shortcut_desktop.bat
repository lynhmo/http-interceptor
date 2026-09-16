@echo off
rem ============================================================
rem  Tao shortcut "Network Monitor" ra man hinh Desktop
rem  Chay file nay 1 lan sau khi copy thu muc app di noi khac.
rem ============================================================
setlocal
set "TARGET=%~dp0dist\NetworkMonitor.exe"
set "SHORTCUT_NAME=Network Monitor.lnk"

if not exist "%TARGET%" (
    echo [LOI] Khong tim thay file:
    echo   %TARGET%
    echo Hay chac chan file bat nay nam trong thu muc goc cua app.
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "$ws = New-Object -ComObject WScript.Shell; $p = [IO.Path]::Combine([Environment]::GetFolderPath('Desktop'), $env:SHORTCUT_NAME); $link = $ws.CreateShortcut($p); $link.TargetPath = $env:TARGET; $link.WorkingDirectory = Split-Path $env:TARGET; $link.IconLocation = $env:TARGET; $link.Description = 'Network Monitor - theo doi HTTP request'; $link.Save(); Write-Host ('[OK] Da tao shortcut: ' + $p)"

pause
