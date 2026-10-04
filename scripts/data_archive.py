"""Sauvegarde et restauration des données hors dépôt git (communes construites, téléchargements en cache).

    .venv/bin/python scripts/data_archive.py sauver [--sans-cache] [fichier.tar.gz]
    .venv/bin/python scripts/data_archive.py restaurer fichier.tar.gz

sauver      crée une archive avec web/data/ (communes construites, suffisant pour utiliser l'application)
            et data/raw/ (téléchargements : utile pour ajouter ou reconstruire des communes sans tout
            retélécharger). Sont omis les fichiers recalculables :
              - web/data/**/*.gz : versions compressées, recréées au démarrage du serveur ;
              - data/raw/airbruit2024.gpkg : conversion de airbruit2024.zip, refaite à la demande.
            --sans-cache : web/data/ seulement (archive bien plus petite).
restaurer   extrait l'archive à la racine du projet (dans un dépôt fraîchement cloné par exemple).
"""

import sys
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_SUFFIXES = (".part", ".tmp")
SKIP_FILES = {"data/raw/airbruit2024.gpkg"}


def keep(rel):
    if rel in SKIP_FILES or rel.endswith(SKIP_SUFFIXES) or ".part" in rel:
        return False
    if rel.startswith("web/data/") and rel.endswith(".gz"):
        return False
    return True


def sauver(args):
    with_cache = "--sans-cache" not in args
    names = [a for a in args if not a.startswith("--")]
    out = Path(names[0]) if names else ROOT / f"immo_map-donnees-{time.strftime('%Y%m%d')}.tar.gz"
    roots = ["web/data"] + (["data/raw"] if with_cache else [])
    files = [p for r in roots for p in sorted((ROOT / r).rglob("*"))
             if p.is_file() and keep(p.relative_to(ROOT).as_posix())]
    size = sum(p.stat().st_size for p in files)
    print(f"{len(files)} fichiers, {size / 1e6:.0f} Mo avant compression → {out}")
    with tarfile.open(out, "w:gz", compresslevel=6) as tar:
        for k, p in enumerate(files):
            tar.add(p, arcname=p.relative_to(ROOT).as_posix())
            if k % 500 == 0:
                print(f"  {k}/{len(files)}", flush=True)
    print(f"archive : {out} ({out.stat().st_size / 1e6:.0f} Mo)")


def restaurer(args):
    if not args:
        sys.exit(__doc__)
    src = Path(args[0])
    with tarfile.open(src, "r:*") as tar:
        members = tar.getmembers()
        bad = [m.name for m in members if not (m.name.startswith(("web/data/", "data/raw/")) and ".." not in m.name)]
        if bad:
            sys.exit(f"archive inattendue (chemins hors web/data et data/raw) : {bad[:5]}")
        tar.extractall(ROOT, filter="data")
    print(f"{len(members)} fichiers restaurés dans {ROOT}")
    print("Les versions compressées (.gz) seront recréées au prochain lancement de ./run.sh.")


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("sauver", "restaurer"):
        sys.exit(__doc__)
    {"sauver": sauver, "restaurer": restaurer}[sys.argv[1]](sys.argv[2:])
