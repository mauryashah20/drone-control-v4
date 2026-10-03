@echo off
title Network & ZeroTier IPv6 Optimizer
echo ========================================================
echo Configuring IPv6 and ZeroTier for Direct Low-Latency P2P
echo ========================================================

net session >nul 2>&1
if %errorLevel% neq 0 (
    echo [ERROR] Please RIGHT-CLICK this file and select "RUN AS ADMINISTRATOR".
    echo.
    pause
    exit /b 1
)

echo [1/3] Ensuring IPv6 is enabled on all network adapters...
powershell -Command "Enable-NetAdapterBinding -Name 'Wi-Fi' -ComponentID ms_tcpip6 -ErrorAction SilentlyContinue"
powershell -Command "Enable-NetAdapterBinding -Name 'Ethernet' -ComponentID ms_tcpip6 -ErrorAction SilentlyContinue"

echo [2/3] Adding Windows Firewall rules for ZeroTier UDP traffic...
powershell -Command "New-NetFirewallRule -DisplayName 'ZeroTier All UDP In' -Direction Inbound -Protocol UDP -Action Allow -Profile Any -ErrorAction SilentlyContinue" >nul 2>&1

echo [3/3] Restarting ZeroTier service to pick up native IPv6...
powershell -Command "Restart-Service ZeroTierOneService -Force"

echo.
echo ========================================================
echo SUCCESS! IPv6 & ZeroTier service refreshed.
echo Your current IPv6 addresses:
powershell -Command "Get-NetIPAddress -AddressFamily IPv6 | Where-Object { $_.InterfaceAlias -like '*Wi-Fi*' } | Select-Object IPAddress"
echo ========================================================
echo.
pause
