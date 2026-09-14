@echo off
chcp 65001 >nul
REM 국내 네트워크가 있는 Windows PC 에서 돌리기 위한 공통 실행기.
REM 보통은 저장소 루트의 run1-probe.bat / run2-preview.bat / run3-collect.bat 를 쓰면 된다.
setlocal
cd /d "%~dp0.."

REM --- 파이썬 찾기 (py 런처 우선, 없으면 python) ---
set PY=
py -3 --version >nul 2>&1 && set PY=py -3
if "%PY%"=="" ( python --version >nul 2>&1 && set PY=python )
if "%PY%"=="" (
  echo.
  echo [!] 파이썬을 찾지 못했습니다.
  echo     https://www.python.org/downloads/ 에서 설치하세요.
  echo     설치 화면에서 "Add python.exe to PATH" 를 반드시 체크해야 합니다.
  echo.
  exit /b 1
)

if not exist ".env" (
  echo.
  echo [!] .env 파일이 없습니다.
  echo     .env.example 을 복사해 .env 로 만들고 인증키 3개를 채우세요.
  echo.
  copy /y ".env.example" ".env" >nul 2>&1 && echo     ^(.env 를 방금 만들어 두었습니다. 메모장으로 열어 값을 채우세요.^)
  echo.
  exit /b 1
)

if not exist ".venv" (
  echo [*] 처음 실행이라 가상환경을 만듭니다. 1~2분 걸립니다...
  %PY% -m venv .venv || ( echo [!] 가상환경 생성 실패 & exit /b 1 )
)

call .venv\Scripts\activate.bat
python -m pip install -q --disable-pip-version-check -r requirements.txt || (
  echo [!] 의존성 설치 실패. 인터넷 연결을 확인하세요.
  exit /b 1
)

set PYTHONPATH=src
python -m g2b_watch.cli %*
set RC=%ERRORLEVEL%
endlocal & exit /b %RC%
