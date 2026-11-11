@echo off
cd /d "%~dp0"
rem Backup mode: Russian text is shown in Latin letters (translit).
rem To return to normal Cyrillic, delete the file latin.flag
echo latin>"%~dp0latin.flag"
call "%~dp0START.bat" %1
