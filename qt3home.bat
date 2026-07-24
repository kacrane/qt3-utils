@echo off
call C:\anaconda\files\Scripts\activate.bat QTLM
cd /d C:\Users\QT3-TQLM\qt3-utils
set PYTHONPATH=C:\Users\QT3-TQLM\qt3-utils\src;%PYTHONPATH%
python -m qt3utils.applications.qt3home.main
pause
