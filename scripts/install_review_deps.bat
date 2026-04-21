@echo off
REM scripts/install_review_deps.bat
REM
REM Idempotent installer for review-pipeline dependencies. Exercises
REM src/capture/review_deps.py for detection + warnings so the pip-side
REM logic stays unit-tested. Re-running is safe; packages already present
REM are left alone.

setlocal
set SCRIPT_DIR=%~dp0
set ROOT=%SCRIPT_DIR%..
pushd "%ROOT%"

if not exist .venv\Scripts\activate.bat (
    echo [install-review-deps] .venv not found at "%ROOT%\.venv"; aborting
    popd
    endlocal
    exit /b 2
)
call .venv\Scripts\activate.bat

echo [install-review-deps] checking dependency status...
python -m src.capture.review_deps summary
set SUMMARY_RC=%ERRORLEVEL%
if "%SUMMARY_RC%"=="0" (
    echo [install-review-deps] nothing to do; exiting
    popd
    endlocal
    exit /b 0
)

echo.
echo [install-review-deps] press any key to proceed with install, CTRL+C to abort...
pause > nul

for /f %%P in ('python -m src.capture.review_deps missing') do (
    echo [install-review-deps] installing %%P...
    python -m pip install %%P
    if errorlevel 1 (
        echo [install-review-deps] pip install %%P failed; aborting
        popd
        endlocal
        exit /b 3
    )
)

echo [install-review-deps] verifying ffmpeg on PATH...
python -m src.capture.review_deps check-ffmpeg
if errorlevel 1 (
    echo [install-review-deps] WARNING ffmpeg missing; install required before triage runs
)

echo [install-review-deps] verifying HuggingFace token...
python -m src.capture.review_deps check-hf
if errorlevel 1 (
    echo [install-review-deps] WARNING HF token missing; pyannote cannot download weights
)

echo [install-review-deps] done
popd
endlocal
exit /b 0
