@echo off
sc query ModelHarbor | findstr /i "RUNNING" >nul
if errorlevel 1 (
  sc start ModelHarbor >nul 2>&1
)
