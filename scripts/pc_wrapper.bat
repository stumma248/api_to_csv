@echo off
REM Informatica PowerCenter Command Task wrapper
REM Returns the same exit code as the Python utility.

set SCRIPT=C:\api_ingestion\api_to_csv.py
set CONFIG=C:\api_ingestion\config\orders.yaml

python "%SCRIPT%" --config "%CONFIG%"
set RC=%ERRORLEVEL%

echo API_TO_CSV_EXIT_CODE=%RC%
exit /b %RC%
