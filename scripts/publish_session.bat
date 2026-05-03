@echo off
REM scripts\publish_session.bat <session-id>
REM
REM Wraps `python -m renee publish --confirm <session-id>` so Paul can
REM ship a captured session to the public renee-sessions-public repo
REM with one command after a good night. Without --confirm the
REM publish pipeline only stages (writes _publish_staging/) for review.
REM
REM Usage: scripts\publish_session.bat <session-id>
REM        scripts\publish_session.bat <session-id> --no-confirm   (stage only)
REM        scripts\publish_session.bat --list                      (list publishable sessions)

setlocal
if "%~1"=="" (
    echo Usage: scripts\publish_session.bat ^<session-id^>
    echo        scripts\publish_session.bat ^<session-id^> --no-confirm
    echo        scripts\publish_session.bat --list
    exit /b 2
)

set ROOT=%~dp0..
pushd "%ROOT%"

if not exist .venv\Scripts\activate.bat (
    echo [publish] .venv not found at "%ROOT%\.venv"
    popd
    exit /b 2
)
call .venv\Scripts\activate.bat
set RENEE_SKIP_ENCRYPT_WARN=1

if /I "%~1"=="--list" (
    python -m renee publish-list
    set RC=%ERRORLEVEL%
    popd
    exit /b %RC%
)

if /I "%~2"=="--no-confirm" (
    python -m renee publish "%~1"
) else (
    python -m renee publish --confirm "%~1"
)
set RC=%ERRORLEVEL%

popd
endlocal
exit /b %RC%
