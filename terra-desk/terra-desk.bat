@echo off
rem Starts terra-desk from source on Windows (needs Python 3.11+ from python.org or the Microsoft Store).
rem Most people should use terra-desk-setup.exe instead: it needs nothing else installed.
cd /d "%~dp0"
where py >nul 2>nul && (py -3 -X utf8 -m terra_desk %*) || (python -X utf8 -m terra_desk %*)
