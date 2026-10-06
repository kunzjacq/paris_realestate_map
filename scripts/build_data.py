"""Construit (ou reconstruit) les données de communes en ligne de commande.

    .venv/bin/python scripts/build_data.py 94068 94015 ...   # codes INSEE
    .venv/bin/python scripts/build_data.py                   # communes déjà présentes
    .venv/bin/python scripts/build_data.py --all [codes]     # tout recalculer
    .venv/bin/python scripts/build_data.py --jobs 4 [codes]  # communes construites en parallèle

Une commune déjà construite n'est recalculée que pour ses groupes de couches périmés (FORMATS dans
pipeline.py : grille, transport, destinations, air, bruit) ; les autres couches sont reprises. Une commune
à jour est laissée telle quelle, sauf avec --all. --jobs : nombre de communes construites à la fois, chacune
dans son processus (défaut : un quart des cœurs, 8 au plus ; 1 : dans ce processus).
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pipeline  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Construit les données de communes (codes INSEE).")
    parser.add_argument("codes", nargs="*", help="codes INSEE (défaut : communes déjà présentes)")
    parser.add_argument("--all", action="store_true", help="tout recalculer")
    parser.add_argument("--jobs", type=int, default=pipeline.PARALLEL_BUILDS,
                        help=f"communes construites en parallèle (défaut : {pipeline.PARALLEL_BUILDS})")
    args = parser.parse_args()
    codes = args.codes
    if not codes:
        idx = pipeline.WEB_DATA / "index.json"
        codes = [c["code"] for c in json.loads(idx.read_text())["communes"]] if idx.exists() else []
    if not codes:
        sys.exit(__doc__)
    groups = set(pipeline.FORMATS) if args.all else None
    errors = pipeline.build_many([(code, groups) for code in codes], max(1, args.jobs))
    if errors:
        sys.exit("échecs : " + ", ".join(f"{c} ({e})" for c, e in errors.items()))


if __name__ == "__main__":
    main()
