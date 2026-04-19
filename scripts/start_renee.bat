@echo off
REM One-click launcher: opens the audio bridge from OptiPlex to the
REM running RunPod pod. Pod must already be awake — run `renee wake`
REM separately, or from this script in the future.
REM
REM Usage: double-click, or `scripts\start_renee.bat` from a shell.
REM        --mobile            run the phone/PWA proxy instead of talk

set ROOT=%~dp0..
set VENV=%ROOT%\.venv\Scripts

REM Silence the plaintext-vault warning until Paul picks a keyring posture.
set RENEE_SKIP_ENCRYPT_WARN=1

set MODE=talk
if /I "%~1"=="--mobile" set MODE=proxy
if /I "%~1"=="-m"       set MODE=proxy

pushd "%ROOT%"
if "%MODE%"=="proxy" (
    REM Drop the first arg (--mobile) so remaining flags pass through to
    REM `renee proxy` untouched.
    shift
    "%VENV%\python.exe" -m renee proxy %1 %2 %3 %4 %5
) else (
    "%VENV%\python.exe" -m renee talk
)
popd
