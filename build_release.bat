@echo off
rem Build the GeoViewer Windows release (PyInstaller, offline).
rem Outputs in distribute\:
rem    dist\GeoViewer\                      the application (GeoViewer.exe + libraries)
rem    release\GeoViewer_<version>_win64.zip  the zip to hand out
rem    build\                               PyInstaller work folder (removed unless --keep-work)
rem Options (passed through to packaging\build_release.py):
rem    --skip-build   only re-test and re-zip an existing dist\GeoViewer
rem    --no-zip       do not create the release zip
rem    --no-test      skip the smoke test of the built GeoViewer.exe
rem    --keep-work    keep distribute\build
setlocal
cd /d "%~dp0"
set "VPY=%~dp0.venv\Scripts\python.exe"
if not exist "%VPY%" (
  echo .venv not found - run install_env.bat first.
  exit /b 1
)
"%VPY%" -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
  echo Installing PyInstaller from whl\ ...
  "%VPY%" -m pip install --no-index --find-links "%~dp0whl" pyinstaller
  if errorlevel 1 exit /b 1
)
"%VPY%" "%~dp0packaging\build_release.py" %*
exit /b %errorlevel%
