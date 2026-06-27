@echo off
title Tissue Monitor — iniciando...
cd /d "C:\Users\Lucas Brígido\teste do claude\analisar_tissue.py"

echo.
echo  ================================================
echo   TISSUE MONITOR — Iniciando dashboard + tunnel
echo  ================================================
echo.

:: Encerra instâncias anteriores
taskkill /F /IM python.exe /FI "WINDOWTITLE eq Tissue*" >nul 2>&1

:: Inicia o dashboard em segundo plano
echo  [1/3] Iniciando dashboard local...
start "" /B "C:\Python314\python.exe" dashboard.py

:: Aguarda o dashboard ficar pronto
echo  [2/3] Aguardando dashboard ficar pronto...
:WAIT
ping -n 2 127.0.0.1 >nul
curl -s -o nul -w "%%{http_code}" http://127.0.0.1:8050/ 2>nul | findstr "200" >nul
if errorlevel 1 goto WAIT

:: Inicia o ngrok e abre o painel
echo  [3/3] Abrindo tunnel publico...
echo.
echo  ================================================
echo   Dashboard local:  http:/127.0.0.1:8050
echo   Link publico:     sera exibido abaixo
echo  ================================================
echo.
"C:\Users\Lucas Brígido\AppData\Local\Microsoft\WinGet\Packages\Ngrok.Ngrok_Microsoft.Winget.Source_8wekyb3d8bbwe\ngrok.exe" http 8050
