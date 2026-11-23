@echo off
rem Launch the GeoViewer license keygen GUI.
rem Uses the GeoViewer app's virtual environment (..\.venv) if present - it already has
rem PySide6 and cryptography. Otherwise set KEYGEN_PY to a Python that has them.
setlocal
cd /d "%~dp0"
set "PY=%~dp0..\.venv\Scripts\python.exe"
if defined KEYGEN_PY set "PY=%KEYGEN_PY%"
if not exist "%PY%" (
  echo Python not found at "%PY%".
  echo Create it with the app's install_env.bat, or set KEYGEN_PY to a Python
  echo that has PySide6 and cryptography ^(pip install -r requirements.txt^).
  pause
  exit /b 1
)
"%PY%" "%~dp0keygen_gui.py" %*
endlocal
