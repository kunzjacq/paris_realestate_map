#!/usr/bin/env python3
"""Sauvegarde et restauration des données hors dépôt git (communes construites, téléchargements en cache).

    python3 scripts/data_archive.py save [--no-cache] [--xz] [fichier.tar.gz | fichier.tar.xz]
    python3 scripts/data_archive.py restore fichier.tar.gz | fichier.tar.xz

(bibliothèque standard seulement, Python ≥ 3.12 ; la commande xz, si elle est installée, accélère --xz)

save        crée une archive avec web/data/ (communes construites, suffisant pour utiliser l'application)
            et data/raw/ (téléchargements : utile pour ajouter ou reconstruire des communes sans tout
            retélécharger). Sont omis les fichiers recalculables :
              - web/data/**/*.gz : versions compressées, recréées au démarrage du serveur ;
              - web/data/communes/*/layers.pack : couches regroupées, recréées au démarrage du serveur ;
              - data/raw/airbruit2024.gpkg : conversion de airbruit2024.zip, refaite à la demande ;
              - data/raw/tiles/ : tuiles du fond de carte, retéléchargées à la demande ;
              - data/raw/gtfs_rail_*.npz, data/raw/transit_*.json : tirés des horaires (idfm_gtfs.zip).
              - data/raw/dvf/ventes_*.csv.gz : ventes filtrées, tirées des fichiers DVF.
            --no-cache : web/data/ seulement (archive bien plus petite).
            --xz : compression xz, archive ~25 % plus petite qu'en gzip ; utilise la commande xz sur tous
            les cœurs si elle est installée (sinon le module lzma, sur un seul cœur : ~7 min pour
            170 communes). Restauration plus lente qu'en gzip (~45 s contre ~20 s). Implicite si le nom
            finit par .xz.
            Nom par défaut : idf_livability_map-data-AAAAMMJJ.tar.gz (.tar.xz avec --xz) à la racine du projet.
restore     extrait l'archive à la racine du projet (dans un dépôt fraîchement cloné par exemple) ;
            compression (gzip, xz, bzip2 ou aucune) reconnue d'après le contenu, quel que soit le nom.
"""

import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_SUFFIXES = (".part", ".tmp")
SKIP_FILES = {"data/raw/airbruit2024.gpkg"}
XZ_PRESET = 6  # au-delà, gain négligeable sur ces données pour un temps bien plus long
# signature en tête de fichier -> mode de lecture tarfile
MAGIC = [(b"\x1f\x8b", "r:gz", "gzip"), (b"\xfd7zXZ\x00", "r:xz", "xz"), (b"BZh", "r:bz2", "bzip2")]


def keep(rel):
    if rel in SKIP_FILES or rel.endswith(SKIP_SUFFIXES) or ".part" in rel or rel.startswith("data/raw/tiles/"):
        return False
    if rel.startswith(("data/raw/gtfs_rail_", "data/raw/transit_", "data/raw/dvf/ventes_")):  # recalculés en quelques secondes
        return False
    if rel.startswith("web/data/") and (rel.endswith(".gz") or rel.endswith("/layers.pack")):
        return False
    return True


def save(args):
    with_cache = "--no-cache" not in args
    names = [a for a in args if not a.startswith("--")]
    xz = "--xz" in args or (bool(names) and names[0].endswith((".xz", ".txz")))
    out = Path(names[0]) if names else ROOT / f"idf_livability_map-data-{time.strftime('%Y%m%d')}.tar.{'xz' if xz else 'gz'}"
    roots = ["web/data"] + (["data/raw"] if with_cache else [])
    files = [p for r in roots for p in sorted((ROOT / r).rglob("*"))
             if p.is_file() and keep(p.relative_to(ROOT).as_posix())]
    size = sum(p.stat().st_size for p in files)
    xz_cmd = shutil.which("xz") if xz else None
    how = "gzip" if not xz else "xz (commande xz, tous les cœurs)" if xz_cmd else "xz (module lzma, un cœur)"
    print(f"{len(files)} fichiers, {size / 1e6:.0f} Mo avant compression → {out}, {how}")
    t0 = time.time()

    def add_all(tar):
        for k, p in enumerate(files):
            tar.add(p, arcname=p.relative_to(ROOT).as_posix())
            if k % 500 == 0:
                print(f"  {k}/{len(files)}", flush=True)

    if xz_cmd:  # flux tar non compressé envoyé à xz multi-cœur
        with open(out, "wb") as f:
            proc = subprocess.Popen([xz_cmd, f"-{XZ_PRESET}", "-T0", "-c"], stdin=subprocess.PIPE, stdout=f)
            with tarfile.open(fileobj=proc.stdin, mode="w|") as tar:
                add_all(tar)
            proc.stdin.close()
            if proc.wait():
                sys.exit(f"échec de xz (code {proc.returncode}) : archive {out} incomplète")
    elif xz:
        with tarfile.open(out, "w:xz", preset=XZ_PRESET) as tar:
            add_all(tar)
    else:
        with tarfile.open(out, "w:gz", compresslevel=6) as tar:
            add_all(tar)
    print(f"archive : {out} ({out.stat().st_size / 1e6:.0f} Mo, {time.time() - t0:.0f} s)")


def restore(args):
    if not args:
        sys.exit(__doc__)
    src = Path(args[0])
    with open(src, "rb") as f:
        head = f.read(6)
    mode, name = next(((m, n) for sig, m, n in MAGIC if head.startswith(sig)), ("r:", "aucune"))
    print(f"{src} : compression {name}")
    try:
        tar = tarfile.open(src, mode)
    except tarfile.ReadError as e:
        sys.exit(f"{src} n'est pas une archive tar lisible ({e})")
    with tar:
        members = tar.getmembers()
        bad = [m.name for m in members if not (m.name.startswith(("web/data/", "data/raw/")) and ".." not in m.name)]
        if bad:
            sys.exit(f"archive inattendue (chemins hors web/data et data/raw) : {bad[:5]}")
        tar.extractall(ROOT, filter="data")
    print(f"{len(members)} fichiers restaurés dans {ROOT}")
    print("Les versions compressées (.gz) seront recréées au prochain lancement de ./run.sh.")


if __name__ == "__main__":
    commands = {"save": save, "restore": restore}
    if len(sys.argv) < 2 or sys.argv[1] not in commands:
        sys.exit(__doc__)
    commands[sys.argv[1]](sys.argv[2:])
