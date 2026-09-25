@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Instagram Auto Publisher

rem Se o ambiente do projeto ja existe, abre direto (rapido).
if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" main.py
    pause
    exit /b
)

rem Primeira vez neste PC: detecta Python, instala o que falta e abre.
call "%~dp0_bootstrap.bat"
if errorlevel 1 ( pause & exit /b 1 )

"%VPY%" main.py
pause
