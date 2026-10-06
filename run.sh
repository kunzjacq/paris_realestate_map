#!/bin/sh
# Lance l'application sur http://localhost:8000/ (port modifiable : ./run.sh 8080 ; nombre de communes
# construites en parallèle : ./run.sh --jobs 4)
#
# Utilise .venv s'il fonctionne avec le Python de cette machine, sinon .venv-local (créé une fois).
# Les dépendances manquantes (requirements.txt) sont installées automatiquement.
set -e
cd "$(dirname "$0")"
unset PYTHONPATH PIP_PREFIX

IMPORTS="import geopandas, rasterio, pyogrio, pyproj, scipy, requests"
usable() { [ -x "$1/bin/python" ] && "$1/bin/python" -c "import sys" 2>/dev/null; }
complete() { "$1/bin/python" -c "$IMPORTS" 2>/dev/null; }

if usable .venv && complete .venv; then
  VENV=.venv
else
  VENV=.venv-local
  if ! usable "$VENV"; then
    echo "Création de l'environnement Python ($VENV)…"
    rm -rf "$VENV"
    if ! python3 -m venv "$VENV"; then
      echo "Échec de « python3 -m venv ». Sur Debian/Ubuntu : sudo apt install python3-venv" >&2
      exit 1
    fi
    "$VENV/bin/python" -m pip install --quiet --upgrade pip
  fi
  if ! complete "$VENV"; then
    echo "Installation des dépendances manquantes…"
    "$VENV/bin/python" -m pip install --quiet -r requirements.txt
  fi
fi
exec "$VENV/bin/python" scripts/server.py "$@"
