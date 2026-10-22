@echo off
rem Double-click (or run) to open the backend in its own window; it restarts itself if it stops.
start "AI Sales Agent - Backend (keep open)" powershell -NoExit -ExecutionPolicy Bypass -File "%~dp0backend_window.ps1"
