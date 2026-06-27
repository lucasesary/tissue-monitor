@echo off
cd /d "C:\Users\Lucas Brígido\teste do claude\analisar_tissue.py"
echo Iniciando dashboard...
start "" /MIN powershell -Command "cd 'C:\Users\Lucas Brigido\teste do claude\analisar_tissue.py'; python dashboard.py"
timeout /t 10 /nobreak >nul
start "" "http://127.0.0.1:8050"
echo Pronto!
