@echo off
chcp 65001 >nul
title Instagram Auto Publisher - Instalacao
echo ============================================
echo   INSTALACAO - Instagram Auto Publisher
echo ============================================
echo.
echo Instalando dependencias (1a vez apenas)...
echo.

py -3 -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo Falhou com "py -3", tentando "python"...
    python -m pip install -r requirements.txt
)

echo.
echo ============================================
echo  Pronto! Agora va em INICIAR.bat para abrir
echo  o programa e faca login no Instagram.
echo ============================================
pause
