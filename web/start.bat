@echo off
cd /d "%~dp0backend"
python -m uvicorn app:app --host 127.0.0.1 --port 5000 --reload
