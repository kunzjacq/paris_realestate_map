"""Construit (ou reconstruit) les données de communes en ligne de commande.

    .venv/bin/python scripts/build_data.py 94068 94015 ...   # codes INSEE
    .venv/bin/python scripts/build_data.py                   # reconstruit les communes déjà présentes
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pipeline  # noqa: E402


def main():
    codes = sys.argv[1:]
    if not codes:
        idx = pipeline.WEB_DATA / "index.json"
        codes = [c["code"] for c in json.loads(idx.read_text())["communes"]] if idx.exists() else []
    if not codes:
        sys.exit(__doc__)
    for code in codes:
        pipeline.build_commune(code)


if __name__ == "__main__":
    main()
