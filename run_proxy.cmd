@echo off
REM Run the anyformat proxy in front of a running audiocpp_server.
REM Adjust --backend if your server listens elsewhere.
REM Adjust --ffmpeg if ffmpeg.exe is not on PATH.
python "%~dp0proxy\audiocpp_anyformat_proxy.py" --backend http://127.0.0.1:8081 %*
pause
