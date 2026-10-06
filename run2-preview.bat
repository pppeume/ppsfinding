@echo off
chcp 65001 >nul
echo ============================================
echo  2단계. 미리보기 (Notion 에 아무것도 쓰지 않음)
echo  최근 3일치 공고 중 무엇이 걸리는지만 봅니다.
echo ============================================
echo.
call "%~dp0scripts\run_local.bat" collect --days 3 --no-notion
echo.
pause
