@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 국고채 10-3 스프레드

python -c "import sys" >nul 2>nul
if errorlevel 1 (
  echo.
  echo [오류] Python이 설치되어 있지 않습니다.
  echo python.org 에서 설치할 때 "Add python.exe to PATH" 를 꼭 체크하세요.
  echo.
  pause
  exit /b
)

if exist ".streamlit\secrets.toml" goto install
echo.
set /p KEY=ECOS API 키를 붙여넣고 Enter를 누르세요: 
if not exist ".streamlit" mkdir ".streamlit"
> ".streamlit\secrets.toml" echo ECOS_API_KEY = "%KEY%"

:install
if exist ".installed_v3" goto run
echo.
echo 처음 실행이라 필요한 프로그램을 설치합니다. 1~3분 걸립니다...
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo [오류] 설치에 실패했습니다. 이 창을 캡처해서 보내주세요.
  pause
  exit /b
)
echo ok> .installed_v3

:run
if not exist "%USERPROFILE%\.streamlit" mkdir "%USERPROFILE%\.streamlit"
if not exist "%USERPROFILE%\.streamlit\credentials.toml" (
  > "%USERPROFILE%\.streamlit\credentials.toml" echo [general]
  >> "%USERPROFILE%\.streamlit\credentials.toml" echo email = ""
)
echo.
echo 잠시 후 브라우저가 열립니다. 이 검은 창을 닫으면 프로그램이 꺼집니다.
python -m streamlit run app.py --browser.gatherUsageStats false
pause
