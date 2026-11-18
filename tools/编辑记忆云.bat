@echo off
chcp 65001 >nul
cd /d "%~dp0.."
python scripts\edit_graph.py
echo.
pause
