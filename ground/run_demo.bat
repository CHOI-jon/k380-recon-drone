@echo off
REM Edit these two values on site
set COM=COM5
set PI_IP=192.168.43.120
REM Optional: Claude recon report
REM set ANTHROPIC_API_KEY=sk-ant-...
python ground_station.py --com %COM% --baud 57600 --pi %PI_IP%
pause
