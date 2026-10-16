@echo off
echo Starting Discord AI Voice Chat...
echo.
echo Starting API Server in new window...
start "Companion API Server" cmd /k "python -m uvicorn src.api_server:app --host 127.0.0.1 --port 8000 --reload"
timeout /t 2 /nobreak >nul
echo.
echo Starting Discord Bot in new window...
start "Companion Discord Bot" cmd /k "cd discord_bot && node index.js"
echo.
echo Both services are starting...
echo Close this window when done.
pause


