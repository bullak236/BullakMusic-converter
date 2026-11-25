#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

printf "\033]0;BullakMusic-converter // by bullak\007"

cat << "EOF"

  ____        _ _       _    __  __           _      
 |  _ \      | | |     | |  |  \/  |         (_)     
 | |_) |_   _| | | __ _| | _| \  / |_   _ ___ _  ___ 
 |  _ <| | | | | |/ _` | |/ / |\/| | | | / __| |/ __|
 | |_) | |_| | | | (_| |   <| |  | | |_| \__ \ | (__ 
 |____/ \__,_|_|_|\__,_|_|\_\_|  |_|\__,_|___/_|\___|

  CONVERTER // CAR AUDIO STUDIO  (v1.0 - by bullak)
  GitHub: https://github.com/bullak236/BullakMusic-converter
 ======================================================================
EOF
echo ""

if [ ! -x ".venv/bin/python" ]; then
    echo "[1/3] Creando entorno virtual .venv..."
    if command -v python3 >/dev/null 2>&1; then
        python3 -m venv .venv
    elif command -v python >/dev/null 2>&1; then
        python -m venv .venv
    elif command -v uv >/dev/null 2>&1; then
        uv venv --seed --python 3.12 .venv
    else
        echo "[ERROR] No se encontró Python 3.11+ ni uv instalado."
        exit 1
    fi
else
    echo "[OK] Entorno virtual .venv detectado."
fi

echo "[2/3] Verificando e instalando dependencias..."
.venv/bin/python -m pip install --upgrade pip >/dev/null 2>&1 || true
.venv/bin/python -m pip install -r requirements.txt

echo ""
echo "[3/3] Iniciando BullakMusic-converter en http://localhost:8000 ..."
echo "      (Presioná Ctrl+C para detener el servidor)"
echo ""

(
    sleep 2
    if command -v xdg-open >/dev/null 2>&1; then
        xdg-open "http://localhost:8000" >/dev/null 2>&1 || true
    elif command -v open >/dev/null 2>&1; then
        open "http://localhost:8000" >/dev/null 2>&1 || true
    fi
) &

exec .venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
