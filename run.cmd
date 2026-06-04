@echo off
REM Root entry point (Windows). Prefers the local venv interpreter if present.
setlocal
set HERE=%~dp0
if exist "%HERE%.venv\Scripts\python.exe" (
    "%HERE%.venv\Scripts\python.exe" "%HERE%run.py" %*
) else (
    python "%HERE%run.py" %*
)
endlocal
