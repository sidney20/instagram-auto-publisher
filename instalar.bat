@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Instagram Auto Publisher - Instalacao

call "%~dp0_bootstrap.bat"
if errorlevel 1 ( pause & exit /b 1 )

echo.
echo ============================================
echo  Tudo instalado! Agora execute o INICIAR.bat
echo ============================================
pause
