@echo off
chcp 65001 >nul
cd /d "%~dp0.."
python scripts\view_graph.py
echo.
pause
