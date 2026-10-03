@echo off
title ADB over ZeroTier VPN
set ADB="C:\Users\Maurya shah\AppData\Local\Android\Sdk\platform-tools\adb.exe"
set PHONE_IP=192.168.191.130
set PORT=5555

echo ===================================================
echo  Connecting to Phone over ZeroTier VPN (%PHONE_IP%:%PORT%)
echo ===================================================

%ADB% connect %PHONE_IP%:%PORT%
echo.
%ADB% devices
echo.
echo Device connected! You can now run ADB commands wirelessly.
echo.
pause
