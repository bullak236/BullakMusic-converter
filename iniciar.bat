@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
title BullakMusic-converter // by bullak

echo.
echo   ____        _ _       _    __  __           _      
echo  ^|  _ \      ^| ^| ^|     ^| ^|  ^|  \/  ^|         (_)     
echo  ^| ^|_) ^|_   _^| ^| ^| __ _^| ^| _^| \  / ^|_   _ ___ _  ___ 
echo  ^|  _ ^<^| ^| ^| ^| ^| ^|/ _` ^| ^|/ / ^|\/^| ^| ^| ^| / __^| ^|/ __^|
echo  ^| ^|_) ^| ^|_^| ^| ^| ^| (_^| ^|   ^<^| ^|  ^| ^| ^|_^| \__ \ ^| (__ 
echo  ^|____/ \__,_^|_^|_^|\__,_^|_^|\_\_^|  ^|_^|\__,_^|___/_^|\___^|
echo.
echo   CONVERTER // CAR AUDIO STUDIO  (v1.0 - by bullak)
echo   GitHub: https://github.com/bullak236/BullakMusic-converter
echo  ======================================================================
echo.

cd /d "%~dp0"

:: 1. Si ya existe el entorno virtual .venv, verificar que su python.exe exista
if exist ".venv\Scripts\python.exe" (
    echo [OK] Entorno virtual .venv detectado.
    goto :INSTALL_DEPS
)

echo [1/3] Creando entorno virtual .venv...

:: Intentar con python estandar (excluyendo el alias vacio de Microsoft Store)
python -c "import sys; print(sys.version)" >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo [INFO] Usando Python del sistema...
    python -m venv .venv
    if exist ".venv\Scripts\python.exe" goto :INSTALL_DEPS
)

:: Intentar con el lanzador py de Windows
py -3 -c "import sys; print(sys.version)" >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo [INFO] Usando lanzador py -3...
    py -3 -m venv .venv
    if exist ".venv\Scripts\python.exe" goto :INSTALL_DEPS
)

:: Fallback a uv si esta instalado en PATH o en %USERPROFILE%\.local\bin\uv.exe
where uv >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo [INFO] Usando uv venv --seed...
    uv venv --seed --python 3.12 .venv
    if exist ".venv\Scripts\python.exe" goto :INSTALL_DEPS
)

if exist "%USERPROFILE%\.local\bin\uv.exe" (
    echo [INFO] Usando %USERPROFILE%\.local\bin\uv.exe venv --seed...
    "%USERPROFILE%\.local\bin\uv.exe" venv --seed --python 3.12 .venv
    if exist ".venv\Scripts\python.exe" goto :INSTALL_DEPS
)

echo.
echo [ERROR] No se encontro una instalacion valida de Python 3.11+ en el sistema.
echo Por favor, instala Python desde https://www.python.org/downloads/
echo (Asegurate de marcar la casilla "Add Python to PATH" durante la instalacion).
pause
exit /b 1

:INSTALL_DEPS
echo [2/3] Verificando e instalando dependencias en .venv...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul 2>&1
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if %ERRORLEVEL% NEQ 0 (
    if exist "%USERPROFILE%\.local\bin\uv.exe" (
        "%USERPROFILE%\.local\bin\uv.exe" pip install --python ".venv\Scripts\python.exe" -r requirements.txt
    ) else (
        echo [ERROR] Fallo la instalacion de dependencias de requirements.txt
        pause
        exit /b 1
    )
)

echo.
echo [3/3] Levantando servidor BullakMusic-converter en http://localhost:8000 ...
echo       (Presiona Ctrl+C en esta ventana para apagar el servidor)
echo.

:: Abrir el navegador automaticamente despues de 2 segundos en segundo plano
start "" cmd /c "timeout /t 2 /nobreak >nul && start http://localhost:8000"

".venv\Scripts\python.exe" -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
pause
