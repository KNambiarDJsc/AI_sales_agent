@echo off
rem Double-click (or run) to open the console website (http://localhost:3100) in its own window.
start "AI Sales Agent - Website (keep open)" /D "%~dp0..\web" cmd /k npm run dev
