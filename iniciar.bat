@echo off
chcp 65001 >nul
title Instagram Auto Publisher
py -3 main.py
if errorlevel 1 (
    python main.py
)
pause
