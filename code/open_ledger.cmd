@echo off
setlocal
if /i "%CONDA_DEFAULT_ENV%"=="personal-ledger" if exist "%CONDA_PREFIX%\pythonw.exe" (
    start "" "%CONDA_PREFIX%\pythonw.exe" "%~dp0ledger_gui.py"
    exit /b
)
if exist "D:\Miniconda3\envs\personal-ledger\pythonw.exe" (
    start "" "D:\Miniconda3\envs\personal-ledger\pythonw.exe" "%~dp0ledger_gui.py"
    exit /b
)
where conda >nul 2>nul
if not errorlevel 1 (
    call conda run --no-capture-output -n personal-ledger python "%~dp0ledger_gui.py"
    exit /b
)
echo Conda environment personal-ledger was not found. See code\README.md.
pause
