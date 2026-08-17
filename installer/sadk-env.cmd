@echo off
rem SADK command prompt: puts sonc on PATH for this shell only.
rem
rem Keep this file pure ASCII. cmd.exe parses these lines using the console's
rem original code page, which is whatever the machine defaults to -- on a
rem Simplified Chinese Windows that is GBK. It reads the top of the file
rem BEFORE "chcp 65001" below takes effect, so UTF-8 text up here decodes into
rem mojibake and can swallow the line ending, turning the next "rem" into a
rem bogus command name.
chcp 65001 >nul

for %%i in ("%~dp0..") do set "SADK_HOME=%%~fi"
set "PATH=%~dp0;%PATH%"

echo.
echo   SonAlgebraic SDK  ^|  %SADK_HOME%
echo.
echo   sonc --help          all commands
echo   sonc doctor          check C toolchain
echo   sonc run app.sa      compile and run
echo.
