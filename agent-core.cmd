@echo off
setlocal
set "ENGINE_ROOT=%~dp0"
set "PYTHONPATH=%ENGINE_ROOT%;%PYTHONPATH%"
if defined AGENT_CORE_PYTHON goto custom_python
where py >nul 2>nul
if %ERRORLEVEL% EQU 0 goto py_launcher
where python >nul 2>nul
if %ERRORLEVEL% EQU 0 goto system_python
echo agent-core: Python 3 is unavailable 1>&2
exit /b 2

:custom_python
"%AGENT_CORE_PYTHON%" -m agent_core.cli %*
exit /b %ERRORLEVEL%

:py_launcher
py -3 -m agent_core.cli %*
exit /b %ERRORLEVEL%

:system_python
python -m agent_core.cli %*
exit /b %ERRORLEVEL%
