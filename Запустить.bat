@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Проверяю зависимости...
python -m pip install -r requirements.txt
echo Запускаю...
python notifier.py
pause
