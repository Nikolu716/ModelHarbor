@echo off
rem ============================================
rem ModelHarbor 服务管理脚本
rem 用法: zgateway-service.cmd [start|stop|restart|status|logs|uninstall]
rem ============================================
set NSSM=%~dp0nssm-2.24\win64\nssm.exe
set SVC=ModelHarbor

if "%1"=="" goto usage

if "%1"=="start" (
    %NSSM% start %SVC%
    goto end
)
if "%1"=="stop" (
    %NSSM% stop %SVC%
    goto end
)
if "%1"=="restart" (
    %NSSM% restart %SVC%
    goto end
)
if "%1"=="status" (
    sc query %SVC% | findstr /i "STATE"
    goto end
)
if "%1"=="logs" (
    if exist "%~dp0..\data\service.log" type "%~dp0..\data\service.log"
    goto end
)
if "%1"=="uninstall" (
    %NSSM% stop %SVC%
    %NSSM% remove %SVC% confirm
    goto end
)

:usage
echo 用法: %~nx0 [start^|stop^|restart^|status^|logs^|uninstall]
echo   start      启动服务
echo   stop       停止服务
echo   restart    重启服务
echo   status     查看服务状态
echo   logs       查看网关运行日志
echo   uninstall  卸载服务

:end
