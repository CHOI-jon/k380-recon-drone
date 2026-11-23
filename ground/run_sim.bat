@echo off
REM Rehearsal without hardware (fake telemetry + fake detections)
python ground_station.py --sim-telem --sim-pi
pause
