@echo off
setlocal
set "ENGINE_ROOT=%~dp0"
set "PYTHONPATH=%ENGINE_ROOT%;%PYTHONPATH%"
if defined AGENT_CORE_PYTHON (
  "%AGENT_CORE_PYTHON%" -m agent_core.cli %*
  exit /b %ERRORLEVEL%
)
where py >nul 2>nul
if %ERRORLEVEL% EQU 0 (
  py -3 -m agent_core.cli %*
  exit /b %ERRORLEVEL%
)
where python >nul 2>nul
if %ERRORLEVEL% EQU 0 (
  python -m agent_core.cli %*
  exit /b %ERRORLEVEL%
)
echo agent-core: Python 3 is unavailable 1>&2
exit /b 2
