@echo off
cd /d "%~dp0"
call "%USERPROFILE%\.venv\Scripts\activate.bat" 2>nul
if not exist "%USERPROFILE%\.venv\Scripts\python.exe" (
    echo Criando ambiente virtual...
    py -3.11 -m venv "%USERPROFILE%\.venv"
)
call "%USERPROFILE%\.venv\Scripts\activate.bat"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install chromium
python main.py
pause
