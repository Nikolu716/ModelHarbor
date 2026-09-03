@echo off
sc query ZGateway | findstr /i "RUNNING" >nul
if errorlevel 1 (
  sc start ZGateway >nul 2>&1
)
