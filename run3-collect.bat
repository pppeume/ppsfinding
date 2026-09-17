@echo off
chcp 65001 >nul
echo ============================================
echo  3단계. 실제 수집 및 Notion 적재
echo ============================================
echo.
call "%~dp0scripts\run_local.bat" collect --days 2
echo.
pause
