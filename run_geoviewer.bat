@echo off
rem Start GeoViewer from source.
rem Optional arguments: -w <workspace folder>, or files to open, e.g.
rem   run_geoviewer.bat -w demo_data
rem   run_geoviewer.bat demo_data\topo1.dtm demo_data\pit1.str
rem Without arguments the last workspace is opened again.
rem The virtual environment is .venv in this folder (create it with install_env.bat).
setlocal
cd /d "%~dp0"
set "VPY=%~dp0.venv\Scripts\python.exe"
if not exist "%VPY%" (
  echo .venv not found - run install_env.bat first.
  exit /b 1
)
"%VPY%" -m geoviewer %*
endlocal
