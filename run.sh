#!/bin/sh
# Lance l'application sur http://localhost:8000/ (port modifiable : ./run.sh 8080 ; nombre de communes
# construites en parallèle : ./run.sh --jobs 4)
#
# Utilise .venv s'il fonctionne avec un Python assez récent, sinon .venv-local (créé une fois avec le premier
# Python ≥ 3.12 trouvé). Les dépendances manquantes (requirements.txt) sont installées automatiquement.
# Linux et macOS.
set -e
cd "$(dirname "$0")"
unset PYTHONPATH PIP_PREFIX

IMPORTS="import geopandas, rasterio, pyogrio, pyproj, scipy, requests"
# Python 3.12 au moins (constructions en parallèle, restauration des archives de données)
RECENT="import sys; sys.exit(sys.version_info < (3, 12))"
usable() { [ -x "$1/bin/python" ] && "$1/bin/python" -c "$RECENT" 2>/dev/null; }
complete() { "$1/bin/python" -c "$IMPORTS" 2>/dev/null; }

# fichiers ouverts : macOS n'en permet que 256 par défaut, trop pour les constructions en parallèle et le
# serveur (« Too many open files ») ; relevé dans la limite autorisée
[ "$(ulimit -n)" = unlimited ] || [ "$(ulimit -n)" -ge 4096 ] || ulimit -n 4096 2>/dev/null || true

if usable .venv && complete .venv; then
  VENV=.venv
else
  VENV=.venv-local
  if ! usable "$VENV"; then
    PY=""
    for p in python3 python3.14 python3.13 python3.12; do
      if command -v "$p" >/dev/null 2>&1 && "$p" -c "$RECENT" 2>/dev/null; then PY=$p; break; fi
    done
    if [ -z "$PY" ]; then
      echo "Python 3.12 ou plus récent introuvable ($(python3 --version 2>&1 || echo 'pas de python3'))." >&2
      if [ "$(uname)" = Darwin ]; then
        echo "Sur macOS : brew install python@3.12 (ou installeur de python.org), puis relancer ./run.sh" >&2
      else
        echo "Sur Debian/Ubuntu : sudo apt install python3.12 python3.12-venv" >&2
      fi
      exit 1
    fi
    echo "Création de l'environnement Python ($VENV, $($PY --version))…"
    rm -rf "$VENV"
    if ! "$PY" -m venv "$VENV"; then
      echo "Échec de « $PY -m venv »." >&2
      [ "$(uname)" = Darwin ] || echo "Sur Debian/Ubuntu : sudo apt install python3-venv" >&2
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
