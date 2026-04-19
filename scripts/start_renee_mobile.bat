@echo off
REM One-click launcher: OptiPlex-side mobile proxy so Paul can talk to
REM Renée from his phone browser over Tailscale. The pod must already
REM be awake — run `renee wake` separately.
REM
REM Usage: double-click, or `scripts\start_renee_mobile.bat` from a shell.

set ROOT=%~dp0..
set VENV=%ROOT%\.venv\Scripts

REM Silence the plaintext-vault warning until Paul picks a keyring posture.
set RENEE_SKIP_ENCRYPT_WARN=1

pushd "%ROOT%"
"%VENV%\python.exe" -m renee proxy %*
popd
