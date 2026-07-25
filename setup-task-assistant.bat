@echo off
setlocal
"%~dp0runtime\python.exe" "%~dp0tools\configure_task_assistant.py" configure --base-dir "%~dp0"
endlocal
