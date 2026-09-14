@echo off
REM Abre o painel local do Sentinela AC com dois cliques, sem janela de terminal.
start "" /b powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0abrir-painel.ps1"
