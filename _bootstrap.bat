@echo off
chcp 65001 >nul
rem ============================================================
rem  Bootstrap do Instagram Auto Publisher
rem  - Detecta o Python (3.11+ se existir, senao o mais novo)
rem  - Cria ambiente isolado na primeira vez
rem  - Instala dependencias so se faltar alguma
rem  - Baixa o navegador do Playwright so se faltar
rem  Usa: call _bootstrap.bat   (define VPY no chamador)
rem ============================================================

set "PYP="
py -3.11 -c "pass" >nul 2>nul && set "PYP=py -3.11"
if not defined PYP py -3 -c "pass" >nul 2>nul && set "PYP=py -3"
if not defined PYP python -c "pass" >nul 2>nul && set "PYP=python"

set "VDIR=%USERPROFILE%\.venv_instagram_publisher"
if exist "%~dp0.venv\Scripts\python.exe" set "VDIR=%~dp0.venv"

if exist "%VDIR%\Scripts\python.exe" goto :havevenv

if not defined PYP goto :nopython
echo [1/3] Criando ambiente Python (so na primeira vez)...
%PYP% -m venv "%VDIR%" >nul 2>nul
if not exist "%VDIR%\Scripts\python.exe" goto :novenv

:havevenv
set "VPY=%VDIR%\Scripts\python.exe"

"%VPY%" -c "import cv2, customtkinter, playwright, PIL, requests, numpy" >nul 2>nul
if errorlevel 1 goto :install
echo [2/3] Dependencias OK.
goto :browser

:install
echo [2/3] Instalando dependencias... (primeira vez: 1 a 2 minutos)
"%VPY%" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 goto :nopip

:browser
if not exist "%LOCALAPPDATA%\ms-playwright\chromium-*" (
    echo [3/3] Baixando o navegador do Playwright... (primeira vez)
    "%VPY%" -m playwright install chromium
    if errorlevel 1 goto :nobrowser
) else (
    echo [3/3] Navegador OK.
)
exit /b 0

:nopython
echo.
echo [ERRO] Python nao encontrado neste computador.
echo.
echo Instale o Python 3.11 ou superior em:
echo     https://www.python.org/downloads/
echo Durante a instalacao MARQUE a opcao "Add python.exe to PATH"
echo e rode o INICIAR.bat de novo.
exit /b 1

:novenv
echo.
echo [ERRO] Nao foi possivel criar o ambiente Python.
echo Tente de novo; se persistir, reinstale o Python pelo
echo site python.org marcando "Add python.exe to PATH".
exit /b 1

:nopip
echo.
echo [ERRO] Falha ao instalar as dependencias.
echo Verifique a internet e rode o INICIAR.bat de novo.
exit /b 1

:nobrowser
echo.
echo [ERRO] Falha ao baixar o navegador do Playwright.
echo Verifique a internet e rode o INICIAR.bat de novo.
exit /b 1
