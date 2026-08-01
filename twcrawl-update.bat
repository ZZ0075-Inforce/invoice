@echo off
rem twcrawl monthly routine launcher (issue #18) -- double-click this.
rem
rem This wrapper is deliberately ASCII-only. cmd.exe parses a .bat byte by byte
rem in the console's OEM codepage (cp950 on zh-TW Windows), so UTF-8 Chinese
rem literals get chopped into bogus commands -- `chcp 65001` does not help,
rem because the damage happens at parse time. All Chinese lives in the .ps1,
rem which PowerShell reads as UTF-8 reliably.
setlocal
set "PS=powershell"
where pwsh >nul 2>nul && set "PS=pwsh"
"%PS%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0twcrawl-update.ps1"
exit /b %ERRORLEVEL%
