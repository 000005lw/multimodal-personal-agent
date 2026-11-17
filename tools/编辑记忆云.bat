@echo off
chcp 65001 >nul
cd /d "%~dp0"
python edit_graph.py
echo.
pause
