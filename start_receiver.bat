@echo off
title Drone FPV Receiver + MAVLink Router
echo ========================================================
echo   Starting Video Receiver (UDP 5005) + MAVLink Telemetry Router
echo   - Telemetry Ingress: UDP 14551 (from Android phone)
echo   - Mission Planner:   UDP 127.0.0.1:14550 / TCP 5760
echo ========================================================
cd /d "%~dp0ground-control"
python src\main.py
pause
