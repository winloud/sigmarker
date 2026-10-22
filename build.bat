@echo off
REM SigMarker portable build; use the same dependency rules as SigMarker.spec.
setlocal
pushd "%~dp0"
python -m PyInstaller --noconfirm --clean SigMarker.spec
if errorlevel 1 (
  echo Build failed. See the output above.
  popd
  pause
  exit /b 1
)
dist\SigMarker\SigMarker.exe --self-test .verification\package-self-test.json
if errorlevel 1 (
  echo Package self-test failed. See .verification\package-self-test.json
  popd
  pause
  exit /b 1
)
echo Build complete: dist\SigMarker\SigMarker.exe
echo Package self-test passed.
popd
pause
