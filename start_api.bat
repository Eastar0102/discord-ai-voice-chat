@echo off
echo Starting Companion API Server...
python -m uvicorn src.api_server:app --host 127.0.0.1 --port 8000 --reload
pause




