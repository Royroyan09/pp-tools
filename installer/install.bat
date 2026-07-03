@echo off
rem PP Tools installer - double-click to install the PP Tools pyRevit
rem extension for the current user. Requires install.ps1 in the same folder.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
pause
