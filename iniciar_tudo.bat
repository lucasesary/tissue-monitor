@echo off
chcp 65001 >nul
title Tissue Monitor — AT1

echo ============================================================
echo   TISSUE MONITOR — Aracruz AT1
echo ============================================================
echo.

cd /d "%~dp0"

echo [1/3] Iniciando dashboard...
start "Dashboard" cmd /k "python dashboard.py"
timeout /t 4 /nobreak >nul

echo [2/3] Iniciando ingestor de e-mail...
start "Ingestor" cmd /k "python ingestor.py"
timeout /t 2 /nobreak >nul

echo [3/3] Abrindo túnel Cloudflare...
echo.
echo  Aguarde a URL publica aparecer abaixo...
echo  Compartilhe essa URL com os operadores.
echo  (A URL muda a cada reinicio - veja README para URL fixa)
echo.
cloudflared tunnel --url http://localhost:8050

pause
