"""Construit (ou reconstruit) les données de communes en ligne de commande.

    .venv/bin/python scripts/build_data.py 94068 94015 ...   # codes INSEE
    .venv/bin/python scripts/build_data.py                   # communes déjà présentes
    .venv/bin/python scripts/build_data.py --all [codes]    # tout recalculer

Une commune déjà construite n'est recalculée que pour ses groupes de couches périmés (FORMATS dans
pipeline.py : grille, transport, air, bruit) ; les autres couches sont reprises. Une commune à jour est
laissée telle quelle, sauf avec --all.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pipeline  # noqa: E402


def main():
    args = sys.argv[1:]
    everything = "--all" in args
    codes = [a for a in args if not a.startswith("--")]
    if not codes:
        idx = pipeline.WEB_DATA / "index.json"
        codes = [c["code"] for c in json.loads(idx.read_text())["communes"]] if idx.exists() else []
    if not codes:
        sys.exit(__doc__)
    for code in codes:
        pipeline.build_commune(code, groups=set(pipeline.FORMATS) if everything else None)


if __name__ == "__main__":
    main()
