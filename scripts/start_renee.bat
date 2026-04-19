@echo off
REM One-click launcher: opens the audio bridge from OptiPlex to the
REM running RunPod pod. Pod must already be awake — run `renee wake`
REM separately, or from this script in the future.
REM
REM Usage: double-click, or `scripts\start_renee.bat` from a shell.

set ROOT=%~dp0..
set VENV=%ROOT%\.venv\Scripts

REM Silence the plaintext-vault warning until Paul picks a keyring posture.
set RENEE_SKIP_ENCRYPT_WARN=1

pushd "%ROOT%"
"%VENV%\python.exe" -m renee talk
popd
