@echo off
REM 국내 네트워크가 있는 Windows PC 에서 바로 돌리기 위한 스크립트
REM
REM   scripts\run_local.bat probe
REM   scripts\run_local.bat collect --dry-run --no-notion
REM   scripts\run_local.bat collect
REM
REM Windows 작업 스케줄러에 등록하면 매일 자동 실행됩니다.
REM   프로그램/스크립트: C:\경로\ppsfinding\scripts\run_local.bat
REM   인수 추가:        collect
REM   시작 위치:        C:\경로\ppsfinding

setlocal
cd /d "%~dp0.."

if not exist ".env" (
  echo [!] .env 가 없습니다. .env.example 을 복사해 값을 채우세요.
  echo       copy .env.example .env
  exit /b 1
)

if not exist ".venv" (
  echo [*] 가상환경을 만듭니다...
  python -m venv .venv
)
call .venv\Scripts\activate.bat
pip install -q -r requirements.txt

set PYTHONPATH=src
python -m g2b_watch.cli %*
endlocal
