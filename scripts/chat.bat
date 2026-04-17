@echo off
REM Quick launcher for Renée/Aiden text chat.
REM Usage:
REM   scripts\chat.bat                       (Renée, default)
REM   scripts\chat.bat aiden                 (Aiden)
REM   scripts\chat.bat renee --backend ollama (force local)

set ROOT=%~dp0..
set VENV=%ROOT%\.venv\Scripts
set PERSONA=%1
if "%PERSONA%"=="" set PERSONA=renee
shift

"%VENV%\python.exe" -m src.cli.chat --persona %PERSONA% %*
