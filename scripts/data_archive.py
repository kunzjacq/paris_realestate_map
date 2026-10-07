#!/usr/bin/env python3
"""Sauvegarde et restauration des données hors dépôt git (communes construites, téléchargements en cache).

    python3 scripts/data_archive.py save [--no-cache] [--raw=none|xz|gz] [fichier.tar]
    python3 scripts/data_archive.py restore fichier.tar | fichier.tar.gz | fichier.tar.xz

(bibliothèque standard seulement, Python ≥ 3.8 ; la commande xz, si elle est installée, accélère
--raw=xz)

save        crée une archive tar, non compressée, avec :
              - web/data/ (communes construites, suffisant pour utiliser l'application), tel quel : les
                couches y sont déjà compressées (<couche>.bin.gz, seule version gardée), sans
                décompression ni recompression ;
              - data/raw/ (téléchargements : utile pour ajouter ou reconstruire des communes sans tout
                retélécharger), tel quel par défaut : ses gros fichiers sont déjà compressés (zip, PNG,
                dalles OSM .json.gz, GeoTIFF compressés) ; une compression ne gagne plus que ~20 %.
            Sont omis les fichiers recalculables :
              - web/data/**/*.json.gz, *.geojson.gz : versions compressées des petits fichiers, recréées au
                démarrage du serveur ;
              - web/data/communes/*/layers.pack(.gz) : couches regroupées, recréées au démarrage du serveur ;
              - data/raw/airbruit2024.gpkg : conversion de airbruit2024.zip, refaite à la demande ;
              - data/raw/tiles/ : tuiles du fond de carte, retéléchargées à la demande ;
              - data/raw/gtfs_rail_*.npz, data/raw/transit_*.json : tirés des horaires (idfm_gtfs.zip) ;
              - data/raw/dvf/ventes_*.csv.gz : ventes filtrées, tirées des fichiers DVF.
            --no-cache : web/data/ seulement (archive bien plus petite).
            --raw=xz : data/raw placé dans l'archive sous forme d'une archive interne compressée en xz,
            data/raw.tar.xz (~20 % plus petite ; commande xz sur tous les cœurs si elle est installée,
            sinon module lzma, sur un seul cœur, bien plus lent) ; --raw=gz : idem en gzip
            (data/raw.tar.gz) ; --raw=none (défaut) : fichiers de data/raw tels quels.
            Nom par défaut : idf_livability_map-data-AAAAMMJJ.tar à la racine du projet.
restore     extrait l'archive à la racine du projet (dans un dépôt fraîchement cloné par exemple), y compris
            l'archive interne de data/raw (extraite à la volée, sans fichier intermédiaire) ; compression
            (gzip, xz, bzip2 ou aucune) reconnue d'après le contenu, quel que soit le nom.
            Les archives à l'ancien format (.tar.gz ou .tar.xz d'ensemble, couches <couche>.bin non
            compressées) se restaurent aussi : le serveur compresse ces couches à son démarrage puis supprime
            les versions non compressées.
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
RAW_MEMBER = "data/raw.tar"  # archive interne de data/raw (--raw=xz ou gz) : data/raw.tar.xz, data/raw.tar.gz
RAW_SUFFIX = {"xz": ".xz", "gz": ".gz", "none": ""}


def keep(rel):
    if rel in SKIP_FILES or rel.endswith(SKIP_SUFFIXES) or ".part" in rel or rel.startswith("data/raw/tiles/"):
        return False
    if rel.startswith(("data/raw/gtfs_rail_", "data/raw/transit_", "data/raw/dvf/ventes_")):  # recalculés en quelques secondes
        return False
    if rel.startswith("web/data/"):
        if rel.endswith(("/layers.pack", "/layers.pack.gz", ".json.gz", ".geojson.gz")):  # recréés au démarrage
            return False
        if rel.endswith(".bin") and (ROOT / (rel + ".gz")).exists():  # ancienne version non compressée
            return False
    return True


def files_under(root):
    return [p for p in sorted((ROOT / root).rglob("*")) if p.is_file() and keep(p.relative_to(ROOT).as_posix())]


def add_files(tar, files, label):
    for k, p in enumerate(files):
        tar.add(p, arcname=p.relative_to(ROOT).as_posix())
        if k % 1000 == 0:
            print(f"  {label} : {k}/{len(files)}", flush=True)


def write_raw_archive(path, files, compression):
    """Archive interne de data/raw, écrite dans path (xz : commande xz sur tous les cœurs si possible)."""
    xz_cmd = shutil.which("xz") if compression == "xz" else None
    if xz_cmd:  # flux tar non compressé envoyé à xz multi-cœur
        with open(path, "wb") as f:
            proc = subprocess.Popen([xz_cmd, f"-{XZ_PRESET}", "-T0", "-c"], stdin=subprocess.PIPE, stdout=f)
            with tarfile.open(fileobj=proc.stdin, mode="w|") as tar:
                add_files(tar, files, "data/raw")
            proc.stdin.close()
            if proc.wait():
                sys.exit(f"échec de xz (code {proc.returncode})")
    else:
        mode, kw = {"xz": ("w:xz", {"preset": XZ_PRESET}), "gz": ("w:gz", {"compresslevel": 6})}[compression]
        with tarfile.open(path, mode, **kw) as tar:
            add_files(tar, files, "data/raw")


def save(args):
    with_cache = "--no-cache" not in args
    names = [a for a in args if not a.startswith("--")]
    raw = next((a.split("=", 1)[1] for a in args if a.startswith("--raw=")), "none")
    if raw not in RAW_SUFFIX:
        sys.exit(f"--raw={raw} : valeurs possibles xz, gz, none")
    out = Path(names[0]) if names else ROOT / f"idf_livability_map-data-{time.strftime('%Y%m%d')}.tar"
    web = files_under("web/data")
    raw_files = files_under("data/raw") if with_cache else []
    mo = lambda files: sum(p.stat().st_size for p in files) / 1e6
    how = ("" if not with_cache else f" ; data/raw : {len(raw_files)} fichiers, {mo(raw_files):.0f} Mo, compression "
           + {"xz": "xz (commande xz, tous les cœurs)" if shutil.which("xz") else "xz (module lzma, un cœur)",
              "gz": "gzip", "none": "aucune (tels quels)"}[raw])
    print(f"web/data : {len(web)} fichiers, {mo(web):.0f} Mo, tels quels{how} → {out}")
    t0 = time.time()
    raw_tmp = out.with_name(out.name + ".raw.tmp")
    try:
        nested = raw_files and raw != "none"
        if nested:
            write_raw_archive(raw_tmp, raw_files, raw)
            print(f"  data/raw compressé : {raw_tmp.stat().st_size / 1e6:.0f} Mo", flush=True)
        with tarfile.open(out, "w") as tar:
            add_files(tar, web, "web/data")
            if nested:
                tar.add(raw_tmp, arcname=RAW_MEMBER + RAW_SUFFIX[raw])
            else:
                add_files(tar, raw_files, "data/raw")
    finally:
        if raw_tmp.exists():
            raw_tmp.unlink()
    print(f"archive : {out} ({out.stat().st_size / 1e6:.0f} Mo, {time.time() - t0:.0f} s)")


def inside(name):
    return name.startswith(("web/data/", "data/raw/")) and ".." not in Path(name).parts


def safe_member(m):
    """Fichier, dossier ou lien physique (couches reprises lors d'une reconstruction) sous web/data ou
    data/raw ; pas de lien symbolique ni de fichier spécial."""
    if not inside(m.name):
        return False
    if m.islnk():
        return inside(m.linkname)
    return m.isfile() or m.isdir()


def is_raw_member(m):
    return m.isfile() and m.name in {RAW_MEMBER + s for s in RAW_SUFFIX.values()}


def extract_members(tar, members):
    if hasattr(tarfile, "data_filter"):  # Python ≥ 3.12 (et correctifs récents des versions antérieures)
        tar.extractall(ROOT, members, filter="data")
    else:  # Python plus ancien (python3 d'Apple, 3.9) : vérifications de safe_member seulement
        tar.extractall(ROOT, members)


def extract_raw(tar, member):
    """Archive interne de data/raw, lue en flux depuis l'archive principale ; renvoie le nombre de fichiers."""
    mode = {".xz": "r|xz", ".gz": "r|gz"}.get(Path(member.name).suffix, "r|")
    n = 0
    with tarfile.open(fileobj=tar.extractfile(member), mode=mode) as inner:
        for m in inner:  # flux : chaque membre vérifié avant extraction
            if not (safe_member(m) and m.name.startswith("data/raw/")):
                sys.exit(f"archive interne inattendue ({member.name}) : {m.name}")
            if hasattr(tarfile, "data_filter"):
                inner.extract(m, ROOT, filter="data")
            else:
                inner.extract(m, ROOT)
            n += 1
            if n % 1000 == 0:
                print(f"  data/raw : {n} fichiers", flush=True)
    return n


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
        nested = [m for m in members if is_raw_member(m)]
        plain = [m for m in members if not is_raw_member(m)]
        bad = [m.name for m in plain if not safe_member(m)]
        if bad:
            sys.exit(f"archive inattendue (chemins hors web/data et data/raw, ou liens) : {bad[:5]}")
        extract_members(tar, plain)
        n = len(plain)
        for m in nested:
            print(f"extraction de {m.name} ({m.size / 1e6:.0f} Mo)…", flush=True)
            n += extract_raw(tar, m)
    print(f"{n} fichiers restaurés dans {ROOT}")
    print("Au prochain lancement de ./run.sh, le serveur recrée les fichiers omis (paquets des couches, versions\n"
          "compressées des petits fichiers) et compresse les couches d'une archive à l'ancien format.")


if __name__ == "__main__":
    commands = {"save": save, "restore": restore}
    if len(sys.argv) < 2 or sys.argv[1] not in commands:
        sys.exit(__doc__)
    commands[sys.argv[1]](sys.argv[2:])
