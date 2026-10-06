"""Préparation des données de l'application immo_map, commune par commune.

Chaque commune a sa propre grille (Web Mercator, ~10 m) écrite dans
web/data/communes/<code>/ :
  meta.json      description de la grille, des couches et des gares utilisées
  <couche>.bin   une couche raster par fichier (uint8 ou uint16, ligne par ligne, nord en haut)
  commune.geojson, stations.geojson, acces.geojson
  quartiers.geojson  découpage en quartiers (Linternaute), absent si le téléchargement a échoué
  quartiers_limites.geojson  limites entre quartiers, chacune une seule fois (tracé en pointillés)

web/data/index.json liste les communes construites ; web/data/global/ regroupe
les gares et accès de toutes les communes pour l'affichage. Les contours des zones
atteignables sont tracés par l'application à partir des temps de trajet.

Les téléchargements bruts sont mis en cache dans data/raw/ et partagés entre communes.
"""

import gzip
import json
import multiprocessing
import os
import re
import shutil
import threading
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
import rasterio
import requests
from pyproj import Transformer
from rasterio.features import rasterize
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components, dijkstra
from scipy.spatial import cKDTree
from rasterio.fill import fillnodata
from rasterio.io import MemoryFile
from rasterio.transform import from_origin
from rasterio.warp import Resampling, reproject, transform_bounds
from shapely import STRtree
from shapely.geometry import Point, box, shape
from shapely.ops import linemerge, polylabel, unary_union

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
WEB_DATA = ROOT / "web" / "data"
COMMUNES_DIR = WEB_DATA / "communes"
GLOBAL_DIR = WEB_DATA / "global"
for d in (RAW, COMMUNES_DIR, GLOBAL_DIR):
    d.mkdir(parents=True, exist_ok=True)

HEADERS = {"User-Agent": "immo_map/1.0 (usage personnel)"}

IDF_DEPTS = ["75", "77", "78", "91", "92", "93", "94", "95"]
DURATIONS_MIN = list(range(3, 21))  # plage du curseur de temps de trajet (min)
STATION_SEARCH_RADIUS_M = 4500   # gares retenues autour de la commune (16 min à vélo ≈ 4 km au plus)
WALK_SPEED_KMH = 4.5             # vitesse de marche
BIKE_SPEED_KMH = 15              # vitesse de croisière vélo
BIKE_SLOW_KMH = 6                # vélo sur voie piétonne (au pas)
MAX_TIME_S = 3600                # au-delà, cellule considérée hors d'atteinte
MAX_APPROACH_M = 300             # distance maximale entre un point et la voie la plus proche
OSM_TILE_DEG = (0.05, 0.075)     # dalles de téléchargement OSM (lat, lon), ~5,5 km de côté
NO_WALK_HIGHWAYS = {"motorway", "motorway_link", "trunk", "trunk_link", "construction", "proposed",
                    "raceway", "bus_guideway", "abandoned", "razed"}
BIKE_HIGHWAYS = {"primary", "primary_link", "secondary", "secondary_link", "tertiary", "tertiary_link",
                 "unclassified", "residential", "living_street", "service", "cycleway", "track", "road"}
BIKE_SLOW_HIGHWAYS = {"footway", "pedestrian", "path", "bridleway", "corridor"}
GRID_MARGIN_M = 300              # marge de la grille autour de la commune
CELL_M = 15.0                    # taille de cellule en mètres Web Mercator (~10 m réels à 48,8°N)
MAX_ACCESS_DIST_M = 400          # au-delà, un accès est jugé mal rattaché à la gare
AIR_YEAR = 2025
# Formats des données, par groupe de couches : à incrémenter quand le calcul d'un groupe change. Le serveur
# reconstruit les communes concernées, en ne recalculant que les groupes périmés (les autres couches sont
# reprises de la version précédente). grille : contour de la commune et grille (tout est recalculé) ;
# transport : gares et temps jusqu'à la gare la plus proche de chaque réseau ; destinations : temps porte à
# porte jusqu'aux destinations (recalculé avec transport : ses couches désignent les gares par leur rang) ;
# air : Airparif ; bruit : Bruitparif et DRIEAT.
FORMATS = {"grille": 9, "transport": 13, "destinations": 2, "air": 9, "bruit": 9}
DATA_FORMAT = 15  # format global (meta.json, index.json ; DATA_FORMAT de web/app.js) : à incrémenter avec FORMATS
AIR_POLLUTANTS = ["no2", "pm25", "pm10"]
# réseaux ferrés pris en compte pour le temps de marche : mode IDFM -> clé utilisée dans les données
NETWORKS = {"RER": "rer", "TRAIN": "transilien", "METRO": "metro"}
GPE_LINES = {"15", "16", "17", "18"}  # Grand Paris Express : lignes de métro en projet (réseaux « gpeAAAAMMJJ »)
TRAM_MODES = ("TRAMWAY", "TRAM")     # tramways en service : un réseau par ligne (« tram1 », « tram3a »…)
NO_STATION = 65535               # indice de gare d'une cellule sans gare atteignable (uint16)
DEST_MAX_S = 2 * 3600            # temps porte à porte au-delà duquel une destination est jugée hors d'atteinte
DEST_NONE = 255                  # temps porte à porte (minutes, uint8) d'une cellule hors d'atteinte
TRAVEL_MODES = ["walk", "bike"]
MODE_NAMES = {"walk": "à pied", "bike": "à vélo"}

GEO_API = "https://geo.api.gouv.fr"
AIRPARIF_WCS = "https://namek.airparif.fr/geoserver/ows"
LINTERNAUTE_MAP = "https://www.linternaute.com/od/map"  # contours des quartiers (données Yanport)
IGN_WFS = "https://data.geopf.fr/wfs/ows"                # contours IRIS (quartiers reconstruits)
IDFM_API = "https://data.iledefrance-mobilites.fr/api/explore/v2.1/catalog/datasets"
# Overpass : les deux machines d'overpass-api.de (lambert, puis gall via lz4.), chacune avec son propre quota
# de créneaux par IP, essayées en alternance ; miroirs en dernier recours, seulement s'ils répondent
OVERPASS = ["https://overpass-api.de/api/interpreter", "https://lz4.overpass-api.de/api/interpreter"]
OVERPASS_MIRRORS = ["https://overpass.private.coffee/api/interpreter",
                    "https://maps.mail.ru/osm/tools/overpass/api/interpreter"]
BRUITPARIF_ZIP = ("https://www.bruitparif.fr/pages/En-tete/800%20Le%20bruit%20en%20%C3%8Ele-de-France/"
                  "300%20carto-air-bruit-en-idf/600%20Opendata%20air-bruit/"
                  "Couches%20SIG%20air-bruit%202024_9_classes.zip")
DRIEAT_SEARCH = "Lot de données relatives aux cartes de bruit stratégiques"
# Cartes stratégiques de bruit E4 « consolidées » (agglomération + grandes infrastructures), route et fer,
# servie en images par le MapProxy de Bruitparif : on retrouve la classe Lden de chaque pixel par sa couleur.
BRUITPARIF_WMS = "https://raster.bruitparif.fr/mapproxy/service"
BRUITPARIF_LAYERS = {"route": "CSB4_w4echConso_Route_A_Lden", "fer": "CSB4_w4echConso_Fer_A_Lden"}
BRUITPARIF_LEGEND = {  # légende commune aux deux couches : borne basse de la classe Lden (40 = moins de 45 dB) -> couleur
    40: (75, 199, 0), 45: (83, 253, 0), 50: (183, 253, 114), 55: (252, 253, 0),
    60: (253, 169, 0), 65: (253, 0, 0), 70: (211, 0, 252), 75: (149, 0, 100),
}
BRUITPARIF_ZOOM = 16             # niveau des tuiles en cache (~2,4 m/pixel) : peu de couleurs mélangées
MAX_COLOR_DIST = 30              # au-delà, pixel jugé mélangé (bord de classe) et ignoré

SOURCES = {
    "air": f"Airparif, moyennes annuelles {AIR_YEAR} modélisées (WCS namek.airparif.fr)",
    "bruitparif": "Bruitparif / Airparif, cartographie air-bruit 2024 (9 classes, toutes voies)",
    "route": "Bruitparif, carte stratégique de bruit E4 consolidée, bruit routier Lden en 8 classes (MapProxy raster.bruitparif.fr)",
    "lden": "Bruit ferroviaire : Bruitparif, CSB E4 consolidée (8 classes), complétée par la DRIEAT (CSB E4 2022, valeur la plus élevée retenue)",
    "gpe": "Grand Paris Express et prolongements de tramway : arrêts en projet et dates de mise en service estimées (IDFM, projets_arrets_idf et projets_lignes_idf)",
    "transit": "Temps porte à porte jusqu'à une destination : trajet jusqu'à une gare puis RER, Transilien, métro, tramway ou TER selon les horaires théoriques IDFM d'un mardi (GTFS, offre-horaires-tc-gtfs-idfm), durée médiane des départs de la période",
    "walk": f"Temps à pied : plus court chemin sur le réseau OpenStreetMap jusqu'aux entrées des gares (IDFM), {WALK_SPEED_KMH} km/h",
    "bike": f"Temps à vélo : réseau OpenStreetMap, sens uniques respectés (sauf contresens cyclables), {BIKE_SPEED_KMH} km/h ({BIKE_SLOW_KMH} km/h sur voies piétonnes)",
}

# Reconstructions en parallèle : un processus par commune (build_many), PARALLEL_BUILDS à la fois ; chacun
# utilise KD_WORKERS cœurs pour ses recherches spatiales, et les téléchargements de tous les processus sont
# limités à MAX_PARALLEL_DOWNLOADS (quotas d'Overpass, serveurs Bruitparif et Airparif).
CPUS = os.cpu_count() or 1
PARALLEL_BUILDS = min(8, max(1, CPUS // 4))
MAX_PARALLEL_DOWNLOADS = 2
KD_WORKERS = CPUS        # processus seul ; dans un processus de build_many : sa part des cœurs
DOWNLOAD_SEM = None      # sémaphore commun aux processus de build_many (téléchargements)
_LOG_QUEUE = None        # file des messages des processus de build_many vers le processus principal

_locks = {}
_locks_guard = threading.Lock()


def lock_for(name):
    """Verrou par fichier de cache, pour que deux constructions ne téléchargent pas la même chose."""
    with _locks_guard:
        return _locks.setdefault(name, threading.Lock())


def http_get(url, **kw):
    for attempt in range(4):
        try:
            r = requests.get(url, headers=HEADERS, timeout=300, **kw)
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.HTTPError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r
        except (requests.HTTPError, requests.ConnectionError, requests.Timeout) as e:
            if attempt == 3:
                raise
            time.sleep(2 * (attempt + 1))


# Mise à jour des données anciennes : pendant une mise à jour (refresh_stale), REFRESH_BEFORE est la date
# limite ; tout fichier en cache plus ancien est retéléchargé. Le nouveau fichier est écrit à côté puis
# remplace l'ancien d'un seul coup : aucune donnée n'est effacée avant que la nouvelle soit disponible.
MAX_AGE_DAYS = 183               # âge au-delà duquel une donnée est proposée à la mise à jour (~6 mois)
REFRESH_BEFORE = None


def is_stale(path):
    return REFRESH_BEFORE is not None and path.exists() and path.stat().st_mtime < REFRESH_BEFORE


def cached(path, fetch):
    """Renvoie path, en le créant via fetch() (qui renvoie des bytes) s'il manque, ou en le
    retéléchargeant pendant une mise à jour s'il est trop ancien."""
    with lock_for(str(path)):
        refresh = is_stale(path)
        if not path.exists() or refresh:
            if DOWNLOAD_SEM is not None:
                DOWNLOAD_SEM.acquire()
            try:
                if path.exists() and not is_stale(path):
                    return path  # téléchargé entre-temps par un autre processus
                tmp = path.with_name(path.name + f".{os.getpid()}.part")
                try:
                    tmp.write_bytes(fetch())
                except Exception as e:
                    tmp.unlink(missing_ok=True)
                    if not refresh:
                        raise
                    print(f"mise à jour impossible, ancienne version conservée : {path.name} ({e})", flush=True)
                    return path
                os.replace(tmp, path)
            finally:
                if DOWNLOAD_SEM is not None:
                    DOWNLOAD_SEM.release()
    return path


# --------------------------------------------------------------------------- communes

def idf_communes():
    """Contours de toutes les communes d'Île-de-France (pour la recherche et les requêtes spatiales)."""
    f = RAW / "idf_communes.gpkg"
    with lock_for(str(f)):
        if not f.exists() or is_stale(f):
            parts = []
            for dep in IDF_DEPTS:
                r = http_get(f"{GEO_API}/departements/{dep}/communes",
                             params={"format": "geojson", "geometry": "contour",
                                     "fields": "nom,code,codeDepartement,population"})
                parts.append(gpd.GeoDataFrame.from_features(r.json()["features"], crs=4326))
            g = pd.concat(parts, ignore_index=True)
            g = g[["code", "nom", "codeDepartement", "population", "geometry"]]
            tmp = f.with_name(f"idf_communes.{os.getpid()}.part.gpkg")
            g.to_file(tmp, driver="GPKG")
            os.replace(tmp, f)
    return gpd.read_file(f)


_idf_cache = None


def communes_table():
    global _idf_cache
    if _idf_cache is None:
        _idf_cache = idf_communes()
    return _idf_cache


def search_communes(q, limit=10):
    g = communes_table()
    norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower()
                            .translate(str.maketrans("àâäéèêëîïôöùûüç", "aaaeeeeiioouuuc")))
    nq = norm(q)
    if not nq:
        return []
    hits = g[g.nom.map(norm).str.contains(nq) | g.code.str.startswith(q.strip())].copy()
    hits["rank"] = ~hits.nom.map(norm).str.startswith(nq)
    hits = hits.sort_values(["rank", "population"], ascending=[True, False]).head(limit)
    return [{"code": r.code, "nom": r.nom, "dep": r.codeDepartement} for r in hits.itertuples()]


def communes_at(lon, lat):
    g = communes_table()
    hit = g[g.contains(Point(lon, lat))]
    return [{"code": r.code, "nom": r.nom, "dep": r.codeDepartement} for r in hit.itertuples()]


def communes_in_bbox(w, s, e, n, min_share=0.3):
    """Communes dont au moins min_share de la surface est dans l'emprise (évite d'embarquer
    une grande commune à peine visible au bord, comme Paris via le bois de Vincennes)."""
    g = communes_table()
    view = box(w, s, e, n)
    hit = g[g.intersects(view)].to_crs(2154)
    view_l93 = gpd.GeoSeries([view], crs=4326).to_crs(2154).iloc[0]
    share = hit.intersection(view_l93).area / hit.area
    hit = hit[share >= min_share]
    return [{"code": r.code, "nom": r.nom, "dep": r.codeDepartement} for r in hit.itertuples()]


def commune_geom(code):
    g = communes_table()
    row = g[g.code == code]
    if row.empty:
        raise ValueError(f"commune {code} inconnue ou hors Île-de-France")
    return row.iloc[0]


# --------------------------------------------------------------------------- quartiers

QUARTIER_RENAMES = {  # code INSEE -> {nom Linternaute: nom affiché}
    "95428": {"Bas Montmorency Centre": "Bas Montmorency", "Centre Montmorency Centre": "Centre Montmorency",
              "Haut Montmorency Est": "Haut Montmorency"},
}


QUARTIERS_FORMAT = 6     # à incrémenter quand le calcul des quartiers change : recalculés au démarrage du serveur
IRIS_MIN_COVER = 0.5   # IRIS attribué à un quartier si les quartiers Linternaute en couvrent au moins la moitié
IRIS_MIN_IOU = 0.6     # en deçà pour un quartier (surface commune / surface réunie), découpage jugé sans rapport
IRIS_FALLBACK_OPEN_M = 8  # quartier gardé en contour Linternaute : parties de moins de 16 m de large retirées


def quartiers(code, commune_wgs, log=print):
    """Quartiers de la commune, découpés par son contour officiel ; GeoDataFrame (nom, geometry), vide si
    Linternaute ne découpe pas la commune, None si un téléchargement échoue (la construction continue
    sans quartiers, réessayés au démarrage suivant du serveur). attrs["source"] : "iris", "iris+linternaute"
    (quelques quartiers sans correspondance) ou "linternaute".

    Les quartiers de Linternaute (données Yanport) sont des IRIS de l'INSEE ou des regroupements d'IRIS,
    aux contours très simplifiés : bords communs déformés (languettes de 15-25 m de large prises au voisin),
    écarts jusqu'à ~80 m avec la limite communale. Chaque quartier est donc reconstruit à partir des
    contours IRIS de l'IGN : chaque IRIS va au quartier qui en contient la plus grande part. Un quartier qui
    ne correspond pas à ses IRIS (bandes de Seine à Paris, découpage propre à Linternaute) garde son
    contour Linternaute, privé des quartiers voisins ; si c'est le cas de la plupart, toute la commune."""
    g = linternaute_quartiers(code, log)
    if g is None or not len(g):
        return g
    try:
        iris = iris_contours(code)
    except Exception as e:
        log(f"contours IRIS indisponibles ({e})")
        return None
    rebuilt = quartiers_from_iris(g, iris, log)
    out = rebuilt if rebuilt is not None else g
    if code == "75056":
        out = with_arrondissement(out, iris)
    out = gpd.clip(out, commune_wgs, keep_geom_type=True)
    out = out[~out.geometry.is_empty].sort_values("nom").reset_index(drop=True)
    out.attrs["source"] = rebuilt.attrs["source"] if rebuilt is not None else "linternaute"
    return out


def with_arrondissement(g, iris):
    """Paris : code postal de l'arrondissement ajouté au nom (« Père Lachaise-Réunion (75020) »),
    d'après l'IRIS qui couvre la plus grande partie du quartier (IRIS de l'arrondissement 751xx -> 750xx)."""
    gl, il = g.to_crs(2154).reset_index(drop=True), iris.to_crs(2154)
    inter = gpd.overlay(il[["code_insee", "geometry"]], gl.assign(k=gl.index)[["k", "geometry"]],
                        how="intersection", keep_geom_type=True)
    inter["a"] = inter.area
    arr = inter.sort_values("a").groupby("k").tail(1).set_index("k").code_insee
    out = g.reset_index(drop=True).copy()
    out["nom"] = [f"{n} (750{arr[k][-2:]})" if k in arr.index else n for k, n in enumerate(out.nom)]
    out.attrs = g.attrs
    return out


def linternaute_quartiers(code, log):
    """Quartiers de la carte « Liste des quartiers » des pages ville de Linternaute (non découpés)."""
    d = RAW / "quartiers"
    d.mkdir(exist_ok=True)

    def fetch():
        opts = {"contours": {"entites": {"type_entites": "YanportTownArea", "nombre_elements": 500,
                                         "filtres": [{"predicat": "partOfTown", "valeur": f"ville-{code}"}]}}}
        r = http_get(LINTERNAUTE_MAP, params={"directory": "odvilles", "entity_uri": f"ville-{code}",
                                              "options": json.dumps(opts, separators=(",", ":"))})
        r.json()["features"]["features"]  # format attendu, sinon rien n'est mis en cache
        return r.content
    try:
        f = cached(d / f"{code}.json", fetch)
        feats = json.loads(f.read_bytes())["features"]["features"]
    except Exception as e:
        log(f"quartiers indisponibles ({e})")
        return None
    if not feats:
        return gpd.GeoDataFrame({"nom": []}, geometry=[], crs=4326)
    renames = QUARTIER_RENAMES.get(code, {})
    names = [ft["properties"].get("name", "").strip() for ft in feats]
    g = gpd.GeoDataFrame({"nom": [renames.get(n, n) for n in names]},
                         geometry=[shape(ft["geometry"]) for ft in feats], crs=4326)
    g["geometry"] = g.geometry.make_valid()
    return g


def iris_contours(code):
    """Contours IRIS de la commune (IGN, Géoplateforme) ; Paris : ceux de ses arrondissements (751xx)."""
    d = RAW / "iris"
    d.mkdir(exist_ok=True)
    flt = "code_insee LIKE '751%'" if code == "75056" else f"code_insee='{code}'"

    def fetch():
        r = http_get(IGN_WFS, params={"SERVICE": "WFS", "VERSION": "2.0.0", "REQUEST": "GetFeature",
                                      "TYPENAMES": "STATISTICALUNITS.IRIS:contours_iris", "COUNT": 5000,
                                      "OUTPUTFORMAT": "application/json", "SRSNAME": "EPSG:4326",
                                      "CQL_FILTER": flt})
        j = r.json()
        if not j["features"] or j.get("numberMatched", 0) != j.get("numberReturned"):  # vide ou incomplet
            raise RuntimeError(f"réponse IGN inattendue ({j.get('numberReturned')}/{j.get('numberMatched')} IRIS)")
        return r.content
    return gpd.read_file(cached(d / f"{code}.json", fetch))


def quartiers_from_iris(g, iris, log):
    """Quartiers reconstruits en IRIS (noms de g), ou None si le découpage ne suit pas les IRIS."""
    gl, il = g.to_crs(2154).reset_index(drop=True), iris.to_crs(2154)
    inter = gpd.overlay(il[["code_iris", "geometry"]], gl.assign(k=gl.index)[["k", "geometry"]],
                        how="intersection", keep_geom_type=True)
    inter["a"] = inter.area
    covered = inter.groupby("code_iris").a.sum() / il.set_index("code_iris").area
    best = inter.sort_values("a").groupby("code_iris").tail(1).set_index("code_iris")
    best = best[covered.reindex(best.index) >= IRIS_MIN_COVER]  # IRIS hors des quartiers : sans quartier
    owner = best.k.to_dict()
    # quartier englobé par un autre chez Linternaute (Île Saint-Louis dans « Seine et Berges ») : sans IRIS
    # après le premier tour ; il reprend l'IRIS qui le couvre le plus s'il est surtout à lui
    iris_area = il.set_index("code_iris").area
    for k in gl.index:
        if k in owner.values():
            continue
        mine = inter[inter.k == k].sort_values("a", ascending=False)
        for r in mine.itertuples():
            prev = owner.get(r.code_iris)
            if r.a / iris_area[r.code_iris] >= IRIS_MIN_COVER and list(owner.values()).count(prev) > 1:
                owner[r.code_iris] = k
                break
    geoms, bad = [], []
    for k, q in enumerate(gl.geometry):
        codes = [c for c, o in owner.items() if o == k]
        u = unary_union(list(il[il.code_iris.isin(codes)].geometry)) if codes else None
        iou = u.intersection(q).area / u.union(q).area if u is not None else 0
        geoms.append(u if iou >= IRIS_MIN_IOU else None)
        if iou < IRIS_MIN_IOU:
            bad.append(f"{g.nom.iloc[k]} ({iou:.2f})")
    if len(bad) * 2 > len(gl):
        log(f"quartiers sans rapport avec les IRIS : contours Linternaute conservés")
        return None
    if bad:
        log(f"quartiers sans rapport avec leurs IRIS, contour Linternaute conservé : {', '.join(bad)}")
        good = unary_union([x for x in geoms if x is not None])
        o = IRIS_FALLBACK_OPEN_M
        geoms = [x if x is not None else q.difference(good).buffer(-o).buffer(o) for x, q in zip(geoms, gl.geometry)]
    res = gpd.GeoDataFrame({"nom": g.nom.values}, geometry=geoms, crs=2154).to_crs(4326)
    res = res[~res.geometry.is_empty]
    res.attrs["source"] = "iris+linternaute" if bad else "iris"
    return res


QUARTIER_EDGE_TOL_M = 2   # deux bords de quartiers voisins à moins de 2 m l'un de l'autre : une seule limite


def quartier_limits(g, commune_wgs):
    """Limites entre quartiers voisins, chacune tracée une seule fois. Les contours des polygones répètent
    chaque bord commun (une fois par voisin, décalés d'environ 1 m) et longent le bord de la commune :
    tracés tels quels, les pointillés superposés se bouchent. Le contour extérieur de l'ensemble des
    quartiers est écarté : il double la limite communale, ou s'en écarte (jusqu'à ~80 m) sans séparer
    deux quartiers. Lignes fusionnées en tronçons continus (pointillés réguliers) ; GeoDataFrame en WGS84."""
    tol = QUARTIER_EDGE_TOL_M
    gl = g.to_crs(2154)
    # ensemble des quartiers, interstices entre voisins comblés (sinon leurs bords passeraient pour extérieurs)
    whole = unary_union(list(gl.geometry.buffer(tol))).buffer(-tol)
    outer = unary_union([whole.boundary, gpd.GeoSeries([commune_wgs], crs=4326).to_crs(2154).iloc[0].boundary])
    outer_zone = outer.buffer(tol)
    bounds = list(gl.boundary)
    zones = [b.buffer(tol) for b in bounds]
    tree = STRtree(zones)
    parts = []
    for i, b in enumerate(bounds):
        # bord déjà tracé par un quartier précédent (voisins seulement, via l'index spatial) ou extérieur
        new = b.difference(outer_zone)
        for j in tree.query(b):
            if j < i and not new.is_empty:
                new = new.difference(zones[j])
        if not new.is_empty:
            parts.append(new)
    merged = unary_union(parts) if parts else None
    flat = [l for l in getattr(merged, "geoms", [merged]) if l is not None and l.geom_type == "LineString"]
    lines = linemerge(flat) if flat else None
    geoms = [] if lines is None or lines.is_empty else [l for l in getattr(lines, "geoms", [lines])
                                                         if l.length >= 3 * tol]  # débris de la différence
    return gpd.GeoDataFrame(geometry=geoms, crs=2154).to_crs(4326)


LABEL_TOLERANCE_M = 5  # précision de l'emplacement des noms (polylabel)


def label_point(geom_wgs):
    """Emplacement du nom d'une surface : point le plus éloigné de ses bords (polylabel) dans sa plus grande
    partie, [lat, lon]. Calculé ici une fois pour toutes : dans le navigateur, il bloquait l'affichage
    plusieurs secondes au démarrage (1 400 quartiers, 190 communes)."""
    g = gpd.GeoSeries([geom_wgs], crs=4326).to_crs(2154).iloc[0]
    part = max(getattr(g, "geoms", [g]), key=lambda x: x.area)
    pt = gpd.GeoSeries([polylabel(part, tolerance=LABEL_TOLERANCE_M)], crs=2154).to_crs(4326).iloc[0]
    return [round(pt.y, 6), round(pt.x, 6)]


def write_quartiers(d, g, commune_wgs):
    g = g.copy()
    g["label"] = [label_point(geom) for geom in g.geometry]  # emplacement du nom, [lat, lon]
    (d / "quartiers.geojson").write_text(g.to_json(drop_id=True, ensure_ascii=False))
    (d / "quartiers_limites.geojson").write_text(quartier_limits(g, commune_wgs).to_json(drop_id=True))


def add_missing_quartiers(log=print):
    """(Re)calcule quartiers.geojson et quartiers_limites.geojson des communes construites sans, ou avec
    un calcul antérieur (QUARTIERS_FORMAT), sans reconstruire les couches ; renvoie le nombre de communes
    complétées. Un téléchargement échoué laisse la commune en l'état, réessayée au démarrage suivant."""
    n = 0
    for d in sorted(COMMUNES_DIR.iterdir()):
        meta_f = d / "meta.json"
        if d.name.startswith(".") or not meta_f.exists():
            continue
        meta = json.loads(meta_f.read_text())
        if meta.get("quartiers_format") == QUARTIERS_FORMAT:
            continue
        geom = commune_geom(d.name).geometry
        g = quartiers(d.name, geom, lambda m: log(f"{meta['nom']} : {m}"))
        if g is None:
            continue
        write_quartiers(d, g, geom)
        write_gz(d / "quartiers.geojson")
        write_gz(d / "quartiers_limites.geojson")
        meta.update(quartiers=len(g), quartiers_format=QUARTIERS_FORMAT, quartiers_source=g.attrs.get("source"))
        meta_f.write_text(json.dumps(meta, ensure_ascii=False))
        write_gz(meta_f)
        n += 1
    return n


# --------------------------------------------------------------------------- gares

def idfm_tables():
    # le nom du cache dépend des modes demandés : ajouter un réseau force un nouveau téléchargement
    modes = sorted([*NETWORKS, *TRAM_MODES])
    gares = gpd.read_file(cached(RAW / f"idfm_gares_{'_'.join(modes).lower()}.geojson", lambda: http_get(
        f"{IDFM_API}/emplacement-des-gares-idf/exports/geojson",
        params={"where": " or ".join(f'mode="{m}"' for m in modes)}).content)).to_crs(2154)
    gares = gares[gares.res_com.str.match(r"^(RER [A-E]|TRAIN [A-Z]|METRO \w+|TRAM \w+)$")]
    rel = pd.read_csv(cached(RAW / "idfm_relations_acces.csv", lambda: http_get(
        f"{IDFM_API}/relations-acces/exports/csv", params={"delimiter": ";"}).content), sep=";", dtype=str)
    acc = pd.read_csv(cached(RAW / "idfm_acces.csv", lambda: http_get(
        f"{IDFM_API}/acces/exports/csv", params={"delimiter": ";"}).content), sep=";", dtype=str)
    acc = acc[acc.accisentry.str.lower() == "true"].merge(rel[["zdaid", "accid"]], on="accid")
    return gares, acc


def gpe_key(date):
    """Réseau des gares du Grand Paris Express ouvrant à cette date (« gpe20271231 »)."""
    return "gpe" + date.replace("-", "")


def tram_key(line, date=None):
    """Réseau d'une ligne de tramway (« tram3a »), ou de ses arrêts en projet ouvrant à cette date
    (« tram1_20281231 »)."""
    return f"tram{line.lower()}" + (f"_{date.replace('-', '')}" if date else "")


def project_name_key(name):
    """Nom d'arrêt normalisé (sans accents ni ponctuation) : identifiant des gares en projet."""
    return re.sub(r"[^a-z0-9]", "", name.lower().translate(str.maketrans("àâäéèêëîïôöùûüç", "aaaeeeeiioouuuc")))


def project_zdc(mode, line, name):
    """Identifiant d'une gare en projet, comme une zone de correspondance (« gpe-saintdenispleyel »,
    « tram1-anatolefrance ») : gares de l'application et durées en transports (transit.py)."""
    return f"gpe-{project_name_key(name)}" if mode == "métro" else f"{tram_key(line)}-{project_name_key(name)}"


def project_stops():
    """Arrêts en projet (métro 15 à 18, tramway) avec la date de mise en service de leur opération et phase :
    GeoDataFrame IDFM (projets_arrets_idf) + date, en Lambert 93 ; tracés des projets (projets_lignes_idf)."""
    arrets = gpd.read_file(cached(RAW / "idfm_projets_arrets.geojson", lambda: http_get(
        f"{IDFM_API}/projets_arrets_idf/exports/geojson").content))
    lignes = gpd.read_file(cached(RAW / "idfm_projets_lignes.geojson", lambda: http_get(
        f"{IDFM_API}/projets_lignes_idf/exports/geojson").content))
    keep = lambda d: ((d["mode"] == "métro") & d.indice.isin(GPE_LINES)) | (d["mode"] == "tram")
    arrets, lignes = arrets[keep(arrets)], lignes[keep(lignes)]
    dated = lignes[lignes.mes_estime.notna()]
    dates = (pd.DataFrame({"id_operati": dated.id_operati, "phase": dated.phase,
                           "date": pd.to_datetime(dated.mes_estime, utc=True).dt.strftime("%Y-%m-%d")})
             .groupby(["id_operati", "phase"], dropna=False).date.min())
    arrets = arrets.join(dates, on=["id_operati", "phase"])
    # phase sans tracé daté (ligne 15 Est : tracés sans phase) : date de l'opération
    arrets["date"] = arrets.date.fillna(arrets.id_operati.map(dates.groupby(level=0).min()))
    # opération sans date : Versailles Chantiers phase 4 (déjà en phase 3), T4 Montfermeil, T1 Quatre Routes
    return arrets[arrets.date.notna()].to_crs(2154), lignes.to_crs(2154)


def project_stations():
    """Arrêts en projet, avec leur date de mise en service estimée par IDFM (jeux « projets_arrets_idf » et
    « projets_lignes_idf ») : gares des lignes 15 à 18 du Grand Paris Express et arrêts des prolongements de
    tramway. La date d'un arrêt est celle de son opération et de sa phase (une même opération regroupe des
    phases de dates différentes) ; une gare du Grand Paris Express desservie par plusieurs lignes ouvre avec la
    première. GeoDataFrame (zdc, nom, lignes, networks, date, geometry) en Lambert 93 ; networks : le réseau
    de sa date (gpe_key, ou tram_key de sa ligne), pour que l'application combine les arrêts ouverts à une
    date donnée comme elle combine RER, Transilien et métro."""
    cols = ["zdc", "nom", "lignes", "networks", "date", "geometry"]
    arrets, _ = project_stops()
    if arrets.empty:
        return gpd.GeoDataFrame(columns=cols, geometry="geometry", crs=2154)
    norm = project_name_key
    rows = []
    gpe = arrets[arrets["mode"] == "métro"]
    for key, grp in gpe.groupby(gpe.nom_arret.map(norm)):
        first = grp.groupby("indice").date.min()  # ouverture de chaque ligne à cette gare
        date = first.min()
        rows.append({
            "zdc": project_zdc("métro", None, grp.nom_arret.iloc[0]), "nom": grp.nom_arret.iloc[0],
            "lignes": [f"Métro {l} ({first[l][:4]})" for l in sorted(first.index, key=int)],
            "networks": [gpe_key(date)], "date": date,
            "geometry": unary_union(list(grp.geometry)).centroid,
        })
    tram = arrets[arrets["mode"] == "tram"]
    for (line, key), grp in tram.groupby([tram.indice, tram.nom_arret.map(norm)]):
        date = grp.date.min()
        rows.append({
            "zdc": project_zdc("tram", line, grp.nom_arret.iloc[0]), "nom": grp.nom_arret.iloc[0],
            "lignes": [f"Tram T{line} ({date[:4]})"],
            "networks": [tram_key(line, date)], "date": date,
            "geometry": unary_union(list(grp.geometry)).centroid,
        })
    return gpd.GeoDataFrame(rows, crs=2154)


def line_label(res_com):
    """« TRAIN P » -> « Transilien P », « METRO 7bis » -> « Métro 7bis », « TRAM 3a » -> « Tram T3a »."""
    return res_com.replace("TRAIN ", "Transilien ").replace("METRO ", "Métro ").replace("TRAM ", "Tram T")


def line_order(label):
    """RER, puis Transilien, puis métro, puis tramway ; numéros dans l'ordre numérique."""
    kind = 0 if label.startswith("RER") else 1 if label.startswith("Transilien") else 2 if label.startswith("Métro") else 3
    num = re.match(r"(?:Métro |Tram T)(\d+)", label)
    return (kind, int(num.group(1)) if num else 0, label)


def stations_near(commune_l93):
    """Gares à portée de la commune : gares et arrêts de tramway IDFM en service (avec leurs accès), puis
    gares du Grand Paris Express et arrêts de tramway en projet (sans accès connus : on part du point)."""
    gares, acc = idfm_tables()
    zone = commune_l93.buffer(STATION_SEARCH_RADIUS_M)
    gares = gares[gares.within(zone)]

    stations = []
    for zdc, grp in gares.groupby("id_ref_zdc"):
        stations.append({
            "zdc": str(zdc),
            "nom": grp.nom_zdc.iloc[0],
            # « TRAIN P » -> « Transilien P » ; RER d'abord
            "lignes": sorted({line_label(l) for l in grp.res_com}, key=line_order),
            # un réseau par mode, sauf le tramway : un par ligne (sélection ligne à ligne dans l'application)
            "networks": sorted({tram_key(rc.split()[1]) if m in TRAM_MODES else NETWORKS[m]
                                for m, rc in zip(grp["mode"], grp.res_com)}),
            "zdas": sorted(set(grp.id_ref_zda.astype(str))),
            "geometry": unary_union(list(grp.geometry)).centroid,
        })
    projects = project_stations()
    for r in projects[projects.within(zone)].itertuples():
        stations.append({"zdc": r.zdc, "nom": r.nom, "lignes": r.lignes, "networks": r.networks, "zdas": [],
                         "date": r.date, "geometry": r.geometry})
    if not stations:
        return gpd.GeoDataFrame(columns=["zdc", "nom", "lignes", "networks", "zdas", "date", "geometry"], geometry="geometry", crs=2154), \
            gpd.GeoDataFrame(columns=["station", "nom_acces", "geometry"], geometry="geometry", crs=2154)
    stations = gpd.GeoDataFrame(stations, crs=2154).sort_values("nom").reset_index(drop=True)
    # date d'ouverture (gares du Grand Paris Express), None pour les gares en service
    stations["date"] = [d if isinstance(d, str) else None for d in stations.get("date", [None] * len(stations))]

    access_rows = []
    for i, st in stations.iterrows():
        cand = acc[acc.zdaid.isin(st.zdas)].drop_duplicates("accid")
        pts = [(Point(float(x), float(y)), name) for x, y, name in
               zip(cand.accxespg2154, cand.accyespg2154, cand.accname)]
        pts = [(p, n) for p, n in pts if p.distance(st.geometry) <= MAX_ACCESS_DIST_M]
        if not pts:  # pas d'accès connu : on part du point de la gare
            pts = [(st.geometry, "gare")]
        for p, n in pts:
            access_rows.append({"station": i, "zdc": st.zdc,
                                "nom_acces": n if isinstance(n, str) else "accès", "geometry": p})
    return stations, gpd.GeoDataFrame(access_rows, crs=2154)


# --------------------------------------------------------------------------- réseau OSM et temps de trajet
#
# Les temps de trajet sont calculés localement sur le réseau OpenStreetMap (plus court chemin depuis
# les entrées des gares), ce qui donne un temps réel en chaque point et pas seulement des tranches.

def overpass_alive(url):
    """Miroir joignable ? (page /status en moins de 10 s ; un miroir en panne fait sinon attendre 2 min)"""
    try:
        return requests.get(url.rsplit("/", 1)[0] + "/status", headers=HEADERS, timeout=10).status_code == 200
    except requests.RequestException:
        return False


def overpass_wait_slot(url, log):
    """Attend qu'un créneau soit libre pour notre IP (page /status du serveur Overpass)."""
    status_url = url.rsplit("/", 1)[0] + "/status"
    for _ in range(10):
        try:
            txt = requests.get(status_url, headers=HEADERS, timeout=20).text
        except requests.RequestException:
            return  # page d'état indisponible : on tente la requête
        if "slots available now" in txt or "Rate limit: 0" in txt:
            return
        waits = [int(w) for w in re.findall(r"in (\d+) seconds", txt)]
        if not waits:
            return
        wait = min(min(waits) + 1, 120)
        log(f"Overpass : créneau libre dans {wait} s")
        time.sleep(wait)


def osm_tile(i, j, log=print):
    """Voies (highway=*) d'une dalle OSM, téléchargée une fois via Overpass."""
    d = RAW / "osm"
    d.mkdir(exist_ok=True)
    dlat, dlon = OSM_TILE_DEG
    s, w = i * dlat, j * dlon
    query = f'[out:json][timeout:180];way["highway"]({s:.4f},{w:.4f},{s + dlat:.4f},{w + dlon:.4f});out body;>;out skel qt;'

    def fetch():
        # les deux machines principales en alternance (504 rapide = machine saturée : l'autre peut répondre),
        # en respectant leur quota ; puis les miroirs joignables ; puis un dernier tour des principales
        plan = OVERPASS * 3 + [u for u in OVERPASS_MIRRORS if overpass_alive(u)] + OVERPASS
        err, tried = "", set()
        for attempt, url in enumerate(plan):
            host = url.split("/")[2]
            if url in tried:  # nouveau passage sur une machine qui a échoué : lui laisser du répit
                wait = 15 * min(attempt // 2 + 1, 4)
                log(f"Overpass : nouvel essai sur {host} dans {wait} s")
                time.sleep(wait)
            tried.add(url)
            if url in OVERPASS:
                overpass_wait_slot(url, log)
            try:
                # connexion en 10 s au plus ; lecture : délai entre deux paquets reçus
                r = requests.post(url, data={"data": query}, headers=HEADERS,
                                  timeout=(10, 240 if url in OVERPASS else 120))
                if r.status_code == 200 and r.content[:1] == b"{":
                    data = json.loads(r.content)
                    # délai ou mémoire dépassés côté serveur : HTTP 200 mais réponse tronquée, signalée
                    # par « remark » ; une dalle sans voie vient d'un serveur sans données d'Île-de-France
                    if "remark" in data:
                        err = f"réponse incomplète ({data['remark'][:80]})"
                    elif not any(e["type"] == "way" for e in data["elements"]):
                        err = "réponse sans voie"
                    else:
                        return r.content
                else:
                    err = f"HTTP {r.status_code}"
            except (requests.RequestException, ValueError) as e:
                err = type(e).__name__
            log(f"Overpass, essai {attempt + 1}/{len(plan)} ({host}) : {err}")
        raise RuntimeError(f"Overpass indisponible ({err}), réessayez plus tard")
    return json.loads(cached(d / f"{i}_{j}.json", fetch).read_bytes())


def osm_tiles(bounds_wgs):
    w, s, e, n = bounds_wgs
    dlat, dlon = OSM_TILE_DEG
    return [(i, j) for i in range(int(np.floor(s / dlat)), int(np.floor(n / dlat)) + 1)
            for j in range(int(np.floor(w / dlon)), int(np.floor(e / dlon)) + 1)]


def osm_bounds(commune_l93):
    """Emprise du réseau OSM utile à une commune (gares à portée comprises)."""
    return transform_bounds(2154, 4326, *commune_l93.buffer(STATION_SEARCH_RADIUS_M + 500).bounds)


def load_osm(bounds_wgs, log):
    tiles = osm_tiles(bounds_wgs)
    nodes, ways = {}, {}
    for k, (i, j) in enumerate(tiles):
        f = RAW / "osm" / f"{i}_{j}.json"
        log(f"réseau OSM : dalle {k + 1}/{len(tiles)} "
            + ("(en cache)" if f.exists() and not is_stale(f) else "(téléchargement Overpass)"))
        for el in osm_tile(i, j, log)["elements"]:
            if el["type"] == "node":
                nodes[el["id"]] = (el["lon"], el["lat"])
            elif el["type"] == "way":
                ways[el["id"]] = (el.get("tags", {}), el["nodes"])
    return nodes, ways


def _walk_ok(t):
    hw = t.get("highway")
    if hw in NO_WALK_HIGHWAYS and t.get("foot") not in ("yes", "designated", "permissive"):
        return False
    if t.get("foot") in ("no", "private", "use_sidepath"):
        return False
    if t.get("access") in ("no", "private") and t.get("foot") not in ("yes", "designated", "permissive"):
        return False
    return True


def _bike_rule(t):
    """(vitesse km/h, sens) pour un vélo, ou None. Sens : 0 deux sens, 1 sens de la voie, -1 inverse."""
    hw, bicycle = t.get("highway"), t.get("bicycle")
    if bicycle in ("no", "use_sidepath", "private") or hw == "steps":
        return None
    if t.get("access") in ("no", "private") and bicycle not in ("yes", "designated", "permissive"):
        return None
    if hw in BIKE_HIGHWAYS or bicycle in ("yes", "designated", "permissive"):
        speed = BIKE_SPEED_KMH
    elif hw in BIKE_SLOW_HIGHWAYS:
        speed = BIKE_SLOW_KMH  # voies piétonnes : au pas
    else:
        return None
    oneway = t.get("oneway")
    if oneway in ("yes", "1", "true") or (t.get("junction") in ("roundabout", "circular") and oneway != "no"):
        direction = 1
    elif oneway == "-1":
        direction = -1
    else:
        direction = 0
    contraflow = t.get("oneway:bicycle") == "no" or any(
        str(t.get(k, "")).startswith("opposite") for k in ("cycleway", "cycleway:left", "cycleway:right", "cycleway:both"))
    return speed, 0 if contraflow else direction


def build_graphs(nodes, ways):
    """Graphes piéton (non orienté) et vélo (orienté), coûts en secondes, sommets en Lambert-93."""
    ids = np.fromiter(nodes.keys(), dtype=np.int64)
    pos = {nid: k for k, nid in enumerate(ids.tolist())}
    lonlat = np.array(list(nodes.values()))
    tr = Transformer.from_crs(4326, 2154, always_xy=True)
    x, y = tr.transform(lonlat[:, 0], lonlat[:, 1])
    edges = {"walk": ([], [], []), "bike": ([], [], [])}
    for tags, nds in ways.values():
        idx = [pos[n] for n in nds if n in pos]
        if len(idx) < 2:
            continue
        a, b = np.array(idx[:-1]), np.array(idx[1:])
        length = np.hypot(x[a] - x[b], y[a] - y[b])
        if _walk_ok(tags):
            sec = length / (WALK_SPEED_KMH / 3.6)
            ea, eb, ew = edges["walk"]
            ea += [a, b]; eb += [b, a]; ew += [sec, sec]
        rule = _bike_rule(tags)
        if rule:
            speed, direction = rule
            sec = length / (speed / 3.6)
            ea, eb, ew = edges["bike"]
            if direction >= 0:
                ea.append(a); eb.append(b); ew.append(sec)
            if direction <= 0:
                ea.append(b); eb.append(a); ew.append(sec)
    graphs = {}
    for mode, (ea, eb, ew) in edges.items():
        a, b, w = np.concatenate(ea), np.concatenate(eb), np.concatenate(ew)
        # arêtes en double (voies superposées) : on garde la plus rapide
        order = np.lexsort((w, b, a))
        a, b, w = a[order], b[order], w[order]
        first = np.ones(len(a), bool)
        first[1:] = (a[1:] != a[:-1]) | (b[1:] != b[:-1])
        a, b, w = a[first], b[first], np.maximum(w[first], 0.01)
        used = np.zeros(len(ids), bool)
        used[a] = used[b] = True
        graphs[mode] = (a, b, w, used)
    return x, y, graphs


def station_networks(stations):
    """Réseaux pour lesquels calculer les temps : RER, Transilien, métro (toujours), puis les lignes de
    tramway, dates d'ouverture du Grand Paris Express et prolongements de tramway qui ont un arrêt à portée."""
    base = list(NETWORKS.values())
    other = sorted({n for nets in stations.networks for n in nets if n not in base}) if len(stations) else []
    return base + other


def travel_times(grid, stations, accesses, x, y, graphs, log, dest_costs=None, networks=None):
    """Par mode : temps (s, uint16, 65535 = hors d'atteinte) jusqu'à la gare la plus proche de chaque réseau et
    gare correspondante (uint16) ; et pour chaque destination de dest_costs ({clé: durée en transports depuis
    chaque gare, s, NaN sans horaires}), temps porte à porte (min, uint8, DEST_NONE = hors d'atteinte) =
    min sur les gares de (trajet jusqu'à la gare + durée en transports), et gare correspondante. networks :
    réseaux à calculer (par défaut tous ceux des gares ; [] : destinations seulement)."""
    n = len(x)
    cells = np.mgrid[0:grid.height, 0:grid.width]
    cx = grid.left + (cells[1].ravel() + 0.5) * CELL_M
    cy = grid.top - (cells[0].ravel() + 0.5) * CELL_M
    cxl, cyl = Transformer.from_crs(3857, 2154, always_xy=True).transform(cx, cy)
    acc = accesses.to_crs(2154)
    out = {}
    for mode in TRAVEL_MODES:
        a, b, w, used = graphs[mode]
        node_ids = np.flatnonzero(used)
        # les entrées de gare sont rattachées au réseau principal (plus grande composante fortement connexe) :
        # sinon une entrée peut tomber sur un îlot (allée reliée au reste par des escaliers, voie interdite
        # aux vélos…) et la gare devient inaccessible, ou n'est atteinte qu'au prix d'un détour
        _, label = connected_components(csr_matrix((w, (a, b)), shape=(n, n)), directed=True, connection="strong")
        main = np.flatnonzero(used & (label == np.bincount(label[used]).argmax()))
        tree = cKDTree(np.column_stack([x[main], y[main]]))
        approach = (WALK_SPEED_KMH if mode == "walk" else BIKE_SLOW_KMH) / 3.6  # du point à la voie la plus proche

        def nearest(sel, extra, limit):
            """Temps (s) et gare de chaque cellule vers les accès sel, chacun avec un coût de départ extra (s)."""
            t_all = np.full(len(cxl), np.inf)
            s_all = np.full(len(cxl), NO_STATION, "uint16")
            if not len(sel):
                return t_all, s_all
            # un sommet virtuel par accès : arête voie la plus proche -> accès (coût = approche + extra)
            d0, k0 = tree.query(np.column_stack([sel.geometry.x, sel.geometry.y]))
            virt = n + np.arange(len(sel))
            va = np.concatenate([a, main[k0]]); vb = np.concatenate([b, virt])
            vw = np.concatenate([w, np.maximum(d0 / approach + extra, 0.01)])
            g = csr_matrix((vw, (va, vb)), shape=(n + len(sel), n + len(sel)))
            # temps vers la gare : graphe inversé (sens uniques du vélo)
            dist, _, src = dijkstra(g.T, indices=virt, min_only=True, return_predecessors=True, limit=limit)
            reach = node_ids[np.isfinite(dist[node_ids])]
            if len(reach):
                rtree = cKDTree(np.column_stack([x[reach], y[reach]]))
                dd, kk = rtree.query(np.column_stack([cxl, cyl]), k=4, distance_upper_bound=MAX_APPROACH_M,
                                     workers=KD_WORKERS)
                ok = np.isfinite(dd)
                kk = np.where(ok, kk, 0)
                tot = np.where(ok, dist[reach[kk]] + dd / approach, np.inf)
                best = tot.argmin(axis=1)
                t_all = tot[np.arange(len(tot)), best]
                station = np.array(sel.station.values)[src[reach[kk[np.arange(len(kk)), best]]] - n]
                good = t_all <= limit
                t_all[~good] = np.inf
                s_all[good] = station[good]
            return t_all, s_all

        for net in station_networks(stations) if networks is None else networks:
            sel = acc[acc.station.map(lambda si: net in stations.networks.iloc[si])]
            t, st = nearest(sel, 0, MAX_TIME_S)
            good = np.isfinite(t)
            t_cells = np.full(len(t), 65535, "uint16")
            t_cells[good] = np.round(t[good]).astype("uint16")
            out[f"{mode}_{net}"] = t_cells.reshape(grid.shape)
            out[f"station_{mode}_{net}"] = st.reshape(grid.shape)
        for key, cost in (dest_costs or {}).items():
            extra = cost[acc.station.to_numpy()]
            ok = np.isfinite(extra)
            t, st = nearest(acc[ok], extra[ok], DEST_MAX_S)
            good = np.isfinite(t)
            t_cells = np.full(len(t), DEST_NONE, "uint8")
            t_cells[good] = np.minimum(np.round(t[good] / 60), DEST_NONE - 1).astype("uint8")
            out[f"{mode}_dest_{key}"] = t_cells.reshape(grid.shape)
            out[f"station_{mode}_dest_{key}"] = st.reshape(grid.shape)
        log(f"temps {'jusqu’aux destinations' if networks == [] else 'de trajet'} {MODE_NAMES[mode]} calculés")
    return out


# --------------------------------------------------------------------------- grille

class Grid:
    def __init__(self, bounds_l93):
        x0, y0, x1, y1 = bounds_l93
        m = GRID_MARGIN_M
        b = transform_bounds(2154, 3857, x0 - m, y0 - m, x1 + m, y1 + m)
        self.width = int(np.ceil((b[2] - b[0]) / CELL_M))
        self.height = int(np.ceil((b[3] - b[1]) / CELL_M))
        self.left, self.top = b[0], b[3]
        self.right = self.left + self.width * CELL_M
        self.bottom = self.top - self.height * CELL_M
        self.transform = from_origin(self.left, self.top, CELL_M, CELL_M)
        self.shape = (self.height, self.width)
        self.bounds_l93 = transform_bounds(3857, 2154, self.left, self.bottom, self.right, self.top)
        self.bounds_wgs = transform_bounds(3857, 4326, self.left, self.bottom, self.right, self.top)

    def burn(self, shapes_values, dtype="uint8", fill=0):
        shapes_values = list(shapes_values)
        if not shapes_values:
            return np.full(self.shape, fill, dtype)
        return rasterize(shapes_values, out_shape=self.shape, transform=self.transform,
                         fill=fill, dtype=dtype, all_touched=False)

    def latlng_bounds(self):
        w, s, e, n = self.bounds_wgs
        return [[s, w], [n, e]]

    def row_cell_area_m2(self):
        out = []
        for r in range(self.height):
            y = self.top - (r + 0.5) * CELL_M
            lat = np.degrees(2 * np.arctan(np.exp(y / 6378137.0)) - np.pi / 2)
            out.append(round((CELL_M * np.cos(np.radians(lat))) ** 2, 3))
        return out


# --------------------------------------------------------------------------- air

def airparif_layer(grid, pollutant, code):
    """Moyenne annuelle (µg/m³) Airparif, rééchantillonnée sur la grille."""
    x0, y0, x1, y1 = transform_bounds(3857, 27572, grid.left, grid.bottom, grid.right, grid.top)
    x0, y0, x1, y1 = x0 - 200, y0 - 200, x1 + 200, y1 + 200
    d = RAW / "airparif"
    d.mkdir(exist_ok=True)
    # WCS 1.0.0 : en 2.0.1, le GeoServer échoue sur certaines emprises (« startTime is null »)
    params = {
        "service": "WCS", "version": "1.0.0", "request": "GetCoverage",
        "coverage": f"Moyenne_annuelle:{pollutant}", "crs": "EPSG:27572", "format": "GeoTIFF",
        "bbox": f"{x0:.0f},{y0:.0f},{x1:.0f},{y1:.0f}", "resx": 6.25, "resy": 6.25,
        "time": f"{AIR_YEAR}-01-01T00:00:00.000Z"}

    def fetch():
        # le GeoServer peut renvoyer une exception XML avec un statut 200 : on réessaie
        for attempt in range(5):
            content = http_get(AIRPARIF_WCS, params=params).content
            if content[:4] in (b"MM\x00*", b"II*\x00"):
                return content
            time.sleep(3 * (attempt + 1))
        raise RuntimeError(f"Airparif ne renvoie pas de GeoTIFF pour {pollutant} : {content[:300]!r}")
    f = cached(d / f"{pollutant}_{AIR_YEAR}_{code}.tif", fetch)
    with rasterio.open(f) as src:
        dst = np.full(grid.shape, np.nan, dtype="float32")
        reproject(rasterio.band(src, 1), dst, src_crs=src.crs or "EPSG:27572", dst_crs="EPSG:3857",
                  dst_transform=grid.transform, resampling=Resampling.bilinear, dst_nodata=np.nan)
    return np.where(np.isnan(dst), 0, np.round(dst * 10)).astype("uint16")  # dixièmes de µg/m³


# --------------------------------------------------------------------------- bruit

def bruitparif_gpkg(log):
    """Carte air-bruit 2024 convertie une fois en GeoPackage indexé (lecture par emprise rapide)."""
    gpkg = RAW / "airbruit2024.gpkg"
    with lock_for(str(gpkg)):
        z = cached(RAW / "airbruit2024.zip", lambda: http_get(BRUITPARIF_ZIP).content)
        if not gpkg.exists() or gpkg.stat().st_mtime < z.stat().st_mtime:
            log("conversion de la carte Bruitparif (~1 min)")
            src = f"/vsizip/{z}/AirBruit_2024.shp"
            n = pyogrio.read_info(src)["features"]
            tmp = gpkg.with_suffix(".part.gpkg")
            tmp.unlink(missing_ok=True)
            step = 200_000
            for start in range(0, n, step):
                g = pyogrio.read_dataframe(src, skip_features=start, max_features=step)
                g = g.rename(columns={"9": "code"})
                pyogrio.write_dataframe(g, tmp, layer="airbruit", append=start > 0)
            os.replace(tmp, gpkg)
    return gpkg


def bruitparif_layers(grid, log):
    """Code air-bruit = 10 × classe bruit + classe air (une seule rasterisation du code, puis découpage)."""
    g = pyogrio.read_dataframe(bruitparif_gpkg(log), bbox=grid.bounds_l93).to_crs(3857)
    code = grid.burn(zip(g.geometry, g["code"].astype(int)))
    return code // 10, code % 10


def drieat_index():
    """Département → (URL WFS, couches Lden type A) d'après les lots DRIEAT publiés sur data.gouv.fr."""
    f = RAW / "drieat_index.json"

    def fetch():
        r = http_get("https://www.data.gouv.fr/api/1/datasets/", params={"q": DRIEAT_SEARCH, "page_size": 50}).json()
        idx = {}
        for ds in r["data"]:
            for res in ds["resources"]:
                url = res["url"]
                if res["format"] != "wfs" or "geo-ide.developpement-durable" not in url:
                    continue
                base = url.split("&")[0]
                caps = http_get(base, params={"SERVICE": "WFS", "REQUEST": "GetCapabilities", "VERSION": "1.1.0"}).text
                for layer, dep in re.findall(r"<Name>((?:ms:)?N_BRUIT_ZBR_INFRA_[^<]*_A_LD_S_0(\d\d))</Name>", caps):
                    entry = idx.setdefault(dep, {"wfs": base, "layers": []})
                    if layer not in entry["layers"]:
                        entry["layers"].append(layer)
        return json.dumps(idx, indent=1).encode()
    return json.loads(cached(f, fetch).read_text())


def drieat_layer_gpkg(dep, wfs, layer, log):
    d = RAW / "drieat"
    d.mkdir(exist_ok=True)
    short = layer.split(":")[-1]
    gpkg = d / f"{short}.gpkg"
    with lock_for(str(gpkg)):
        if not gpkg.exists() or is_stale(gpkg):
            log(f"téléchargement bruit DRIEAT {short}")
            def fetch():
                # le service renvoie parfois une réponse vide avec un statut 200 : on réessaie
                for attempt in range(4):
                    content = http_get(wfs, params={"SERVICE": "WFS", "VERSION": "1.1.0", "REQUEST": "GetFeature",
                                                    "TYPENAME": layer, "SRSNAME": "EPSG:2154"}).content
                    if b"FeatureCollection" in content[:2000]:
                        return content
                    time.sleep(5 * (attempt + 1))
                raise RuntimeError(f"réponse WFS invalide pour {short} : {content[:200]!r}")
            gml = cached(d / f"{short}.gml", fetch)
            g = gpd.read_file(gml)
            g.columns = [c.lower() if c != "geometry" else c for c in g.columns]
            if "legende" in g:
                lv = pd.to_numeric(g.legende, errors="coerce")
            elif "db_lo" in g:
                lv = pd.to_numeric(g.db_lo, errors="coerce")
            else:  # niveau lisible dans l'identifiant : ...-LD55 ou ...-LD65-70
                lv = pd.to_numeric(g.idzonbruit.str.extract(r"LD(\d\d)")[0], errors="coerce")
            src = "fer" if "_F_" in short else "route"
            out = gpd.GeoDataFrame({"lv": lv.fillna(0).astype(int), "src": src}, geometry=g.geometry, crs=g.crs)
            tmp = gpkg.with_name(f"{short}.{os.getpid()}.part.gpkg")
            out[out.lv > 0].to_file(tmp, driver="GPKG")
            os.replace(tmp, gpkg)
            gml.unlink()
    return gpkg


def depts_around(grid):
    """Départements touchés par l'emprise de la grille (bruit d'une route du département voisin)."""
    g = communes_table()
    w, s, e, n = grid.bounds_wgs
    return sorted(set(g[g.intersects(box(w, s, e, n))].codeDepartement))


def drieat_lden(grid, log):
    """Lden max (borne basse de la tranche de 5 dB) des grandes infrastructures, route et fer séparés."""
    idx = drieat_index()
    out = {"route": np.zeros(grid.shape, "uint8"), "fer": np.zeros(grid.shape, "uint8")}
    covered = []
    for dep in depts_around(grid):
        if dep not in idx:
            continue
        covered.append(dep)
        for layer in idx[dep]["layers"]:
            g = pyogrio.read_dataframe(drieat_layer_gpkg(dep, idx[dep]["wfs"], layer, log), bbox=grid.bounds_l93)
            if g.empty:
                continue
            g = g.to_crs(3857).sort_values("lv")  # les tranches hautes écrasent les basses
            src = g.src.iloc[0]
            out[src] = np.maximum(out[src], grid.burn(zip(g.geometry, g.lv)))
    return out, covered


def decode_png(content):
    """PNG (paletté ou RGB/RGBA) -> tableau RGBA (h, w, 4)."""
    with MemoryFile(content) as mf, mf.open() as src:
        a = src.read()
        if src.count == 1:
            cmap = src.colormap(1)
            lut = np.array([cmap.get(i, (0, 0, 0, 0)) for i in range(256)], dtype="uint8")
            return lut[a[0]]
    rgba = np.moveaxis(a, 0, -1)
    if rgba.shape[-1] == 3:
        rgba = np.concatenate([rgba, np.full(rgba.shape[:2] + (1,), 255, "uint8")], axis=-1)
    return rgba


def bruitparif_lden(grid, code, source, log):
    """Classe Lden (borne basse, 40 = < 45 dB, 0 = non renseigné) d'une carte Bruitparif (route ou fer) sur la grille."""
    res = 156543.03392804097 / 2 ** BRUITPARIF_ZOOM
    W = int(np.ceil((grid.right - grid.left) / res))
    H = int(np.ceil((grid.top - grid.bottom) / res))
    levels = np.array(list(BRUITPARIF_LEGEND), dtype="uint8")
    palette = np.array(list(BRUITPARIF_LEGEND.values()), dtype="float32")
    classes = np.full((H, W), 255, "uint8")
    cache = RAW / f"bruitparif_{source}" / code
    cache.mkdir(parents=True, exist_ok=True)
    tile = 1024
    tiles = [(tx, ty) for ty in range(0, H, tile) for tx in range(0, W, tile)]
    for n, (tx, ty) in enumerate(tiles):
        w, h = min(tile, W - tx), min(tile, H - ty)
        bbox = (grid.left + tx * res, grid.top - (ty + h) * res, grid.left + (tx + w) * res, grid.top - ty * res)

        def fetch(bbox=bbox, w=w, h=h):
            for attempt in range(4):
                r = http_get(BRUITPARIF_WMS, params={
                    "SERVICE": "WMS", "VERSION": "1.3.0", "REQUEST": "GetMap", "LAYERS": BRUITPARIF_LAYERS[source],
                    "STYLES": "", "CRS": "EPSG:3857", "BBOX": ",".join(f"{v:.3f}" for v in bbox),
                    "WIDTH": w, "HEIGHT": h, "FORMAT": "image/png", "TRANSPARENT": "TRUE"})
                if r.content[:8] == b"\x89PNG\r\n\x1a\n":
                    return r.content
                time.sleep(3 * (attempt + 1))
            raise RuntimeError(f"Bruitparif ne renvoie pas d'image : {r.content[:200]!r}")
        log(f"bruit {source} Bruitparif : dalle {n + 1}/{len(tiles)}")
        rgba = decode_png(cached(cache / f"{tx}_{ty}_{w}x{h}.png", fetch).read_bytes())
        # chaque couleur distincte (quelques centaines par image) classée une fois, puis appliquée aux pixels
        key = ((rgba[..., 0].astype("uint32") << 24) | (rgba[..., 1].astype("uint32") << 16)
               | (rgba[..., 2].astype("uint32") << 8) | rgba[..., 3])
        colors, inverse = np.unique(key.ravel(), return_inverse=True)
        rgb = np.stack([(colors >> 24) & 255, (colors >> 16) & 255, (colors >> 8) & 255], -1).astype("float32")
        dist = np.sqrt(((rgb[:, None, :] - palette) ** 2).sum(-1))
        ok = ((colors & 255) > 100) & (dist.min(-1) <= MAX_COLOR_DIST)
        color_class = np.where(ok, levels[dist.argmin(-1)], 255).astype("uint8")
        classes[ty:ty + h, tx:tx + w] = color_class[inverse].reshape(h, w)

    # classe majoritaire par cellule, puis comblement des petits trous (pixels mélangés, bâtiments)
    out = np.full(grid.shape, 255, "uint8")
    reproject(classes, out, src_transform=from_origin(grid.left, grid.top, res, res), src_crs="EPSG:3857",
              dst_transform=grid.transform, dst_crs="EPSG:3857", resampling=Resampling.mode,
              src_nodata=255, dst_nodata=255)
    valid = out != 255
    if valid.any() and not valid.all():
        # fillnodata laisse à 255 les cellules trop éloignées de toute valeur
        filled = fillnodata(out.astype("float32"), mask=valid.astype("uint8"), max_search_distance=4)
        reached = ~valid & (filled != 255)
        out[reached] = np.clip(np.round(filled[reached] / 5) * 5, 40, 75)
    out[out == 255] = 0
    return out


# --------------------------------------------------------------------------- construction d'une commune

NOISE_LAYERS = ["bp_noise", "bp_air", "lden_route", "lden_fer"]


def layer_group(name):
    """Groupe d'une couche (voir FORMATS)."""
    if (layer_network(name) or "").startswith("dest_"):
        return "destinations"
    if layer_network(name):
        return "transport"
    if name in AIR_POLLUTANTS:
        return "air"
    if name in NOISE_LAYERS:
        return "bruit"
    return "grille"


def group_formats(meta):
    """Format de chaque groupe d'une commune construite (meta.json d'avant les formats par groupe : le format
    global vaut pour tous les groupes)."""
    formats = dict(meta.get("formats") or {g: meta.get("format", 1) for g in FORMATS if g != "destinations"})
    # destinations : groupe séparé de transport après coup ; déjà calculées si la commune en a les couches
    formats.setdefault("destinations", 1 if meta.get("destinations") else 0)
    return formats


def outdated_groups(meta):
    return {g for g, v in FORMATS.items() if group_formats(meta).get(g, 0) < v}


def build_commune(code, log=print, groups=None, index=True):
    """Construit la commune, ou la reconstruit en ne recalculant que les groupes de couches demandés (groups,
    parmi FORMATS ; None : les groupes périmés, ou tous pour une nouvelle commune ; ensemble vide : seulement
    les quartiers et meta.json). Les autres couches sont reprises de la version actuelle ; tout est recalculé
    si la grille a changé (contour de la commune modifié). index : mettre à jour index.json (build_many le
    fait une fois pour toutes). Renvoie meta, ou None si rien n'était à refaire."""
    row = commune_geom(code)
    nom = row.nom
    commune = gpd.GeoDataFrame([{"code": code, "nom": nom}], geometry=[row.geometry], crs=4326)
    commune_l93 = commune.to_crs(2154).geometry.iloc[0]
    grid = Grid(commune_l93.bounds)
    out = COMMUNES_DIR / code
    old_meta = json.loads((out / "meta.json").read_text()) if (out / "meta.json").exists() else None
    same_grid = old_meta is not None and (old_meta["width"], old_meta["height"], old_meta["merc"]) == (
        grid.width, grid.height, {"left": grid.left, "top": grid.top, "cell": CELL_M})
    if groups is None:
        groups = outdated_groups(old_meta) if old_meta else set(FORMATS)
        if old_meta and not groups:
            log(f"{nom} : déjà à jour")
            return None
    groups = set(groups)
    if not same_grid or "grille" in groups:
        groups = set(FORMATS)
    if "transport" in groups:  # rang des gares changé : couches des destinations à refaire
        groups.add("destinations")
    reuse = [k for k in (old_meta or {}).get("layers", {}) if layer_group(k) not in groups]
    log(f"{nom} : préparation" + ("" if groups == set(FORMATS) else
                                   f" (recalcul : {', '.join(sorted(groups)) or 'quartiers seulement'})"))

    layers = {"commune": grid.burn([(commune.to_crs(3857).geometry.iloc[0], 1)])}
    for k in reuse:  # couches reprises de la version actuelle
        layers[k] = np.fromfile(out / f"{k}.bin", dtype=old_meta["layers"][k]["dtype"]).reshape(grid.shape)

    if "transport" in groups:
        stations, accesses = stations_near(commune_l93)
        log(f"{nom} : {len(stations)} gares, {len(accesses)} accès")
    elif "destinations" in groups:
        # gares et accès de la version actuelle : les couches reprises désignent les gares par leur rang
        stations = pd.DataFrame(old_meta["stations"])
        rank = {z: i for i, z in enumerate(stations.zdc)}
        accesses = gpd.read_file(out / "acces.geojson").to_crs(2154)
        accesses["station"] = accesses.zdc.map(rank)
    if "transport" in groups or "destinations" in groups:
        x, y, graphs = build_graphs(*load_osm(osm_bounds(commune_l93), lambda m: log(f"{nom} : {m}")))
        # temps porte à porte jusqu'aux destinations : durée en transports depuis chaque gare (horaires IDFM)
        transit = transit_tables(lambda m: log(f"{nom} : {m}"))
        # clés <destination>_<période> (réseau actuel) et <destination>_<période>_<scénario> (réseau prévu)
        dest_costs, station_transit = {}, [{} for _ in range(len(stations))]
        for sk, dests in (transit["tables"] if transit else {}).items():
            for dk, per in dests.items():
                for pk, table in per.items():
                    key = f"{dk}_{pk}" + ("" if sk == "actuel" else f"_{sk}")
                    dest_costs[key] = np.array([table[z]["median"] * 60 if z in table else np.nan
                                                for z in stations.zdc])
                    for i, z in enumerate(stations.zdc):
                        if z in table:  # [durée médiane (min), ligne prise, part estimée (lignes en projet)]
                            station_transit[i][key] = [table[z]["median"], table[z]["line"], table[z].get("proj", 0)]
        if len(stations):
            # Temps de trajet réels jusqu'à la gare la plus proche, par mode (marche, vélo) et par réseau
            # (l'application combine les réseaux choisis pour le mode choisi), et jusqu'aux destinations
            layers.update(travel_times(grid, stations, accesses, x, y, graphs, lambda m: log(f"{nom} : {m}"),
                                       dest_costs, networks=None if "transport" in groups else []))
        elif "transport" in groups:
            for mode in TRAVEL_MODES:
                for net in NETWORKS.values():
                    layers[f"{mode}_{net}"] = np.full(grid.shape, 65535, "uint16")
                    layers[f"station_{mode}_{net}"] = np.full(grid.shape, NO_STATION, "uint16")
        destinations = sorted(dest_costs) if len(stations) else []
    else:
        destinations = old_meta.get("destinations", [])
    if "transport" in groups:
        # transit : {destination_période: [durée médiane en transports (min), ligne prise au départ]}
        station_meta = [{"zdc": z, "nom": n, "lignes": list(l), "networks": list(k),
                         **({"date": d} if isinstance(d, str) else {}), **({"transit": tr} if tr else {})}
                        for z, n, l, k, d, tr in zip(stations.zdc, stations.nom, stations.lignes, stations.networks,
                                                     stations.date, station_transit)]
        extra_networks = station_networks(stations)[len(NETWORKS):]
    else:
        station_meta = old_meta["stations"]
        if "destinations" in groups:
            station_meta = [{**{k: v for k, v in st.items() if k != "transit"}, **({"transit": tr} if tr else {})}
                            for st, tr in zip(station_meta, station_transit)]
        extra_networks = old_meta.get("extra_networks", [])

    if "air" in groups:
        log(f"{nom} : pollution de l'air (Airparif)")
        for pol in AIR_POLLUTANTS:
            layers[pol] = airparif_layer(grid, pol, code)

    if "bruit" in groups:
        log(f"{nom} : bruit (Bruitparif)")
        layers["bp_noise"], layers["bp_air"] = bruitparif_layers(grid, lambda m: log(f"{nom} : {m}"))
        log(f"{nom} : bruit routier (Bruitparif)")
        layers["lden_route"] = bruitparif_lden(grid, code, "route", lambda m: log(f"{nom} : {m}"))
        log(f"{nom} : bruit ferroviaire (Bruitparif + DRIEAT)")
        fer_bp = bruitparif_lden(grid, code, "fer", lambda m: log(f"{nom} : {m}"))
        lden, lden_depts = drieat_lden(grid, lambda m: log(f"{nom} : {m}"))
        # les deux dérivent des CSB E4 ; la DRIEAT n'a pas de classe sous 55 dB et ne couvre pas
        # tout le Val-d'Oise ni l'Essonne : on garde la valeur la plus élevée
        layers["lden_fer"] = np.maximum(fer_bp, lden["fer"])

    tmp = COMMUNES_DIR / f".{code}.{os.getpid()}.part"  # propre au processus (serveur et CLI peuvent coexister)
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    meta_layers = {}
    for name, arr in layers.items():
        if name in reuse:  # fichiers repris tels quels (et leur version compressée : pas de recompression)
            for suffix in (".bin", ".bin.gz"):
                if (out / f"{name}{suffix}").exists():
                    os.link(out / f"{name}{suffix}", tmp / f"{name}{suffix}")
        else:
            (tmp / f"{name}.bin").write_bytes(np.ascontiguousarray(arr).tobytes())
        meta_layers[name] = {"dtype": str(arr.dtype)}
    # paquet : couches de base seulement ; celles des tramways, des lignes en projet (extra_networks) et des
    # destinations, nombreuses, sont chargées une à une par l'application quand on les choisit
    pack = [k for k in meta_layers if layer_network(k) not in set(extra_networks)
            and not (layer_network(k) or "").startswith("dest_")]
    write_pack(tmp, pack)

    commune.to_file(tmp / "commune.geojson", driver="GeoJSON")
    quart = quartiers(code, row.geometry, lambda m: log(f"{nom} : {m}"))
    if quart is not None:
        write_quartiers(tmp, quart, row.geometry)
    if "transport" in groups:
        st = stations.to_crs(4326)
        gpd.GeoDataFrame({"zdc": st.zdc, "nom": st.nom, "lignes": st.lignes.map(" + ".join),
                          "networks": st.networks.map(",".join)},
                         geometry=st.geometry, crs=4326).to_file(tmp / "stations.geojson", driver="GeoJSON")
        accesses[["zdc", "nom_acces", "geometry"]].to_crs(4326).to_file(tmp / "acces.geojson", driver="GeoJSON")
    else:
        for f in ("stations.geojson", "acces.geojson"):
            shutil.copy2(out / f, tmp / f)

    dep = row.codeDepartement
    formats = {g: (FORMATS[g] if g in groups else group_formats(old_meta).get(g, 0)) for g in FORMATS}
    meta = {
        # format global : DATA_FORMAT si tous les groupes sont à jour (l'application signale les autres)
        "format": DATA_FORMAT if all(formats[g] >= v for g, v in FORMATS.items()) else min(formats.values()),
        "formats": formats,
        "code": code, "nom": nom, "dep": dep,
        "width": grid.width, "height": grid.height, "bounds": grid.latlng_bounds(),
        "merc": {"left": grid.left, "top": grid.top, "cell": CELL_M},
        "row_cell_area_m2": grid.row_cell_area_m2(),
        "layers": meta_layers,
        "pack": pack,  # couches de layers.pack, dans l'ordre ; les autres : <couche>.bin
        "stations": station_meta,
        "networks": list(NETWORKS.values()),
        # réseaux en plus de RER, Transilien et métro ayant un arrêt à portée (couches <mode>_<réseau>) :
        # lignes de tramway, dates d'ouverture du Grand Paris Express et des prolongements de tramway
        "extra_networks": extra_networks,
        # destinations_périodes ayant des couches de temps porte à porte (<mode>_dest_<destination>_<période>)
        "destinations": destinations,
        "modes": TRAVEL_MODES,
        "bike_speed_kmh": BIKE_SPEED_KMH,
        "walk_speed_kmh": WALK_SPEED_KMH,
        "durations": DURATIONS_MIN,
        "air_year": AIR_YEAR,
        "fer_coverage": round(float((layers["lden_fer"][layers["commune"] > 0] > 0).mean()), 3),
        "route_coverage": round(float((layers["lden_route"][layers["commune"] > 0] > 0).mean()), 3),
        "built": time.strftime("%Y-%m-%d %H:%M"),
        "air_range": air_range(layers),
        "label": label_point(row.geometry),  # emplacement du nom de la commune, [lat, lon]
        "quartiers": None if quart is None else len(quart),  # None : pas de quartiers.geojson
        "quartiers_format": None if quart is None else QUARTIERS_FORMAT,
        "quartiers_source": None if quart is None else quart.attrs.get("source"),  # "iris" ou "linternaute"
    }
    (tmp / "meta.json").write_text(json.dumps(meta, ensure_ascii=False))
    compress_dir(tmp)
    old = COMMUNES_DIR / f".{code}.{os.getpid()}.old"
    if out.exists():
        out.rename(old)        # l'ancienne version n'est retirée qu'une fois la nouvelle en place
    tmp.rename(out)
    shutil.rmtree(old, ignore_errors=True)
    if index:
        update_index()
    log(f"{nom} : terminé")
    return meta


# --------------------------------------------------------------------------- reconstructions en parallèle

def _init_worker(jobs, log_queue, download_sem):
    global KD_WORKERS, DOWNLOAD_SEM, _LOG_QUEUE
    KD_WORKERS = max(1, CPUS // jobs)
    DOWNLOAD_SEM, _LOG_QUEUE = download_sem, log_queue


def _build_task(code, groups, refresh_before=None):
    """Construction d'une commune dans un processus de build_executor (messages via _LOG_QUEUE) ;
    refresh_before : REFRESH_BEFORE du processus principal pendant une mise à jour des données anciennes."""
    global REFRESH_BEFORE
    REFRESH_BEFORE = refresh_before
    log = (lambda m: _LOG_QUEUE.put((code, m))) if _LOG_QUEUE is not None else print
    return build_commune(code, log, groups, index=False) is not None


def build_executor(jobs=PARALLEL_BUILDS, log_queue=None):
    """Processus de construction : un processus neuf par commune (« spawn » : il lit la version actuelle de
    pipeline.py, même modifiée depuis le lancement du serveur)."""
    ctx = multiprocessing.get_context("spawn")
    return ProcessPoolExecutor(max_workers=jobs, mp_context=ctx, max_tasks_per_child=1, initializer=_init_worker,
                               initargs=(jobs, log_queue, ctx.Semaphore(MAX_PARALLEL_DOWNLOADS)))


def build_many(items, jobs=PARALLEL_BUILDS, log=print, on_done=None, executor=None):
    """Construit les communes items ([(code, groupes)], groupes : voir build_commune) jobs à la fois, puis met à
    jour index.json. on_done(code, erreur ou None) à chaque commune terminée. Renvoie {code: erreur}."""
    errors = {}
    if executor is None and jobs <= 1:
        for code, groups in items:
            try:
                build_commune(code, log, groups, index=False)
                err = None
            except Exception as e:
                err = errors[code] = str(e)
            if on_done:
                on_done(code, err)
        update_index()
        return errors
    queue, pump, own = None, None, executor is None
    if own:
        queue = multiprocessing.get_context("spawn").Queue()
        executor = build_executor(jobs, queue)

        def relay():  # messages des processus
            while (m := queue.get()) is not None:
                log(m[1])
        pump = threading.Thread(target=relay, daemon=True)
        pump.start()
    try:
        futures = {executor.submit(_build_task, code, groups, REFRESH_BEFORE): code for code, groups in items}
        for f in as_completed(futures):
            code, err = futures[f], None
            try:
                f.result()
            except Exception as e:
                err = errors[code] = str(e)
                log(f"{code} : échec ({e})")
            if on_done:
                on_done(code, err)
    finally:
        if own:
            executor.shutdown()
            queue.put(None)
            pump.join()
    update_index()
    return errors


# --------------------------------------------------------------------------- index et couches globales

_index_lock = threading.Lock()


def transit_tables(log=print):
    """Durées en transports jusqu'aux destinations (scripts/transit.py) ; None si les horaires manquent."""
    try:
        import transit
        return transit.transit_tables(log)
    except Exception as e:
        log(f"horaires des transports indisponibles ({e})")
        return None


def network_catalog():
    """Lignes de tramway en service et arrêts en projet par date, pour les choix de l'application :
    {"trams": ["1", "2", "3a"…], "gpe": [dates], "tram_projects": {ligne: [dates]}} ; listes vides si les
    données manquent."""
    out = {"trams": [], "gpe": [], "tram_projects": {}, "destinations": {}, "periods": {}, "transit_day": None,
           "dest_scenarios": {}}
    try:
        gares, _ = idfm_tables()
        lines = {rc.split()[1] for m, rc in zip(gares["mode"], gares.res_com) if m in TRAM_MODES}
        out["trams"] = sorted(lines, key=lambda l: (int(re.match(r"\d+", l).group()), l))
        proj = project_stations()
        out["gpe"] = sorted({d for d, n in zip(proj.date, proj.networks) if n[0].startswith("gpe")})
        for d, n in zip(proj.date, proj.networks):
            if n[0].startswith("tram"):
                out["tram_projects"].setdefault(n[0][4:].split("_")[0], set()).add(d)
        out["tram_projects"] = {l: sorted(v) for l, v in sorted(out["tram_projects"].items())}
        transit = transit_tables(lambda m: None)
        if transit:
            out.update(destinations=transit["destinations"], periods=transit["periods"], transit_day=transit["day"],
                       dest_scenarios={k: v for k, v in transit["scenarios"].items() if v})
    except Exception as e:
        print(f"lignes de tramway ou arrêts en projet indisponibles ({e})", flush=True)
    return out


def update_index():
    """Réécrit index.json et les couches d'affichage communes à toutes les communes construites."""
    with _index_lock:
        communes, stations, acces = [], [], []
        for d in sorted(COMMUNES_DIR.iterdir()):
            if d.name.startswith(".") or not (d / "meta.json").exists():
                continue
            try:
                m = json.loads((d / "meta.json").read_text())
                frames = [gpd.read_file(d / f"{n}.geojson") for n in ("stations", "acces")]
            except Exception as e:  # dossier en cours de remplacement par une autre construction
                print(f"index : {d.name} ignorée ({e})", flush=True)
                continue
            communes.append({"code": m["code"], "nom": m["nom"], "dep": m["dep"], "built": m["built"],
                             "format": m.get("format", 1),
                             "bounds": m.get("bounds")})  # emprise : l'application charge d'abord les communes visibles
            stations.append(frames[0])
            acces.append(frames[1])
        communes.sort(key=lambda c: c["nom"])

        def merged(frames, key):
            frames = [f for f in frames if len(f)]
            if not frames:
                return gpd.GeoDataFrame(columns=["geometry"], geometry="geometry", crs=4326)
            return gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), crs=4326).drop_duplicates(key)

        st = merged(stations, "zdc")
        ac = merged(acces, ["zdc", "nom_acces"])
        # écriture atomique : fichier temporaire puis renommage
        suffix = f".{os.getpid()}.tmp"
        for name, g in [("stations", st), ("acces", ac)]:
            tmp = GLOBAL_DIR / f"{name}.geojson{suffix}"
            g.to_file(tmp, driver="GeoJSON")
            os.replace(tmp, GLOBAL_DIR / f"{name}.geojson")
            write_gz(GLOBAL_DIR / f"{name}.geojson")
        tmp = WEB_DATA / f"index.json{suffix}"
        tmp.write_text(json.dumps(
            {"communes": communes, "sources": SOURCES, "durations": DURATIONS_MIN, "air_year": AIR_YEAR,
             **network_catalog()},
            ensure_ascii=False))
        os.replace(tmp, WEB_DATA / "index.json")


# --------------------------------------------------------------------------- résumés et statistiques

def air_range(layers):
    """Min et max (µg/m³) de chaque polluant dans la commune : plages des curseurs de l'application."""
    inside = layers["commune"] > 0
    return {pol: [float(layers[pol][inside].min()) / 10, float(layers[pol][inside].max()) / 10]
            for pol in AIR_POLLUTANTS if inside.any()}


def read_layers(code, names=None):
    """meta.json et couches de la commune (toutes, ou celles de names présentes)."""
    d = COMMUNES_DIR / code
    meta = json.loads((d / "meta.json").read_text())
    layers = {name: np.fromfile(d / f"{name}.bin", dtype=info["dtype"]).reshape(meta["height"], meta["width"])
              for name, info in meta["layers"].items() if names is None or name in names}
    return meta, layers


def add_missing_labels():
    """Complète meta.json des communes construites avant l'emplacement précalculé de leur nom."""
    n = 0
    for d in COMMUNES_DIR.iterdir():
        f = d / "meta.json"
        if d.name.startswith(".") or not f.exists():
            continue
        meta = json.loads(f.read_text())
        if "label" not in meta:
            meta["label"] = label_point(commune_geom(d.name).geometry)
            f.write_text(json.dumps(meta, ensure_ascii=False))
            write_gz(f)
            n += 1
    return n


def add_missing_air_ranges():
    """Complète meta.json des communes construites avant l'ajout de air_range."""
    n = 0
    for d in COMMUNES_DIR.iterdir():
        f = d / "meta.json"
        if d.name.startswith(".") or not f.exists():
            continue
        meta = json.loads(f.read_text())
        if "air_range" not in meta:
            _, layers = read_layers(d.name)
            meta["air_range"] = air_range(layers)
            f.write_text(json.dumps(meta, ensure_ascii=False))
            write_gz(f)
            n += 1
    return n


_stats_cache = {}  # code -> (built, cellules de la commune, surfaces, couches déjà lues restreintes à ces cellules)


def commune_cells(code):
    built = json.loads((COMMUNES_DIR / code / "meta.json").read_text())["built"]
    hit = _stats_cache.get(code)
    if hit and hit[0] == built:
        return hit
    meta, layers = read_layers(code, {"commune"})
    inside = layers["commune"] > 0
    rows = np.nonzero(inside)[0]
    area = np.asarray(meta["row_cell_area_m2"], dtype="float64")[rows]
    _stats_cache[code] = hit = (built, inside, area, {})
    return hit


def cells_layer(code, name):
    """Couche restreinte aux cellules de la commune, lue à la première demande (les couches des tramways et
    des lignes en projet ne le sont que si on les coche) ; None si la commune n'a pas cette couche."""
    built, inside, _, sub = commune_cells(code)
    if name not in sub:
        _, layers = read_layers(code, {name})
        sub[name] = layers[name][inside] if name in layers else None
    return sub[name]


def zone_stats(codes, q):
    """Surfaces (m²) par commune : totale, retenue, et respectant chaque critère pris seul.
    Mêmes règles que l'application (web/app.js, computeCommune)."""
    out = {}
    nets = [n for n in q.get("networks", [])
            if n in NETWORKS.values() or re.fullmatch(r"gpe\d{8}|tram[0-9a-z]+(_\d{8})?", n)]
    mode = q.get("mode", "walk")
    limit = q.get("walk")                      # minutes, ou None sans filtre de temps
    limit_s = limit * 60 + 29 if limit is not None else 65535
    air = q.get("air", {})
    for code in codes:
        if not (COMMUNES_DIR / code / "meta.json").exists():
            continue
        _, _, area, _ = commune_cells(code)
        v = lambda name: cells_layer(code, name)
        t = np.full(len(area), 65535, "uint16")
        for n in nets:
            if v(f"{mode}_{n}") is not None:
                t = np.minimum(t, v(f"{mode}_{n}"))
        walk = t <= limit_s
        airok = np.ones(len(area), bool)
        for pol in AIR_POLLUTANTS:
            if air.get(pol) is not None:
                airok &= v(pol) <= air[pol] * 10 + 0.5
        bp = v("bp_noise") <= q.get("bp", 3)
        route = v("lden_route") < q.get("route", 999)
        fer = v("lden_fer") < q.get("fer", 999)
        dest = np.ones(len(area), bool)
        if q.get("dest_key") and re.fullmatch(r"[a-z0-9_]+", q["dest_key"]):  # <destination>_<période>[_<scénario>]
            layer = v(f"{mode}_dest_{q['dest_key']}")
            if layer is not None:
                dest = layer <= q.get("dest_max", 60)
        ok = walk & airok & bp & route & fer & dest
        out[code] = {"total": float(area.sum()), "ok": float(area[ok].sum()),
                     "crit": {"walk": float(area[walk].sum()), "air": float(area[airok].sum()),
                              "bp": float(area[bp].sum()), "route": float(area[route].sum()),
                              "fer": float(area[fer].sum()), "dest": float(area[dest].sum())}}
    return out


# --------------------------------------------------------------------------- fond de carte

# Tuiles IGN servies par le serveur local et gardées sur disque au fil de la consultation : une zone déjà
# vue reste disponible hors ligne. Seules ces deux couches sont relayées (pas de relais ouvert) ;
# OpenStreetMap reste en ligne (ses règles d'usage interdisent ce stockage).
TILES_DIR = RAW / "tiles"
TILE_MAX_AGE_DAYS = MAX_AGE_DAYS  # au-delà, tuile retéléchargée à sa prochaine demande, supprimée au démarrage
TILE_MAX_ZOOM = 19
BASEMAPS = {  # nom dans l'URL locale -> (couche WMTS de l'IGN, format, extension du fichier en cache)
    "plan": ("GEOGRAPHICALGRIDSYSTEMS.PLANIGNV2", "image/png", "png"),
    "ortho": ("ORTHOIMAGERY.ORTHOPHOTOS", "image/jpeg", "jpg"),
}
IGN_WMTS = "https://data.geopf.fr/wmts"


def basemap_tile(name, z, x, y):
    """(contenu, type) de la tuile, depuis le cache si elle a moins de TILE_MAX_AGE_DAYS, sinon de l'IGN ;
    None si elle n'existe pas (ou si l'IGN est injoignable et qu'aucune version n'est en cache). Une tuile
    en cache trop ancienne est encore servie quand l'IGN ne répond pas."""
    if name not in BASEMAPS or not 0 <= z <= TILE_MAX_ZOOM or not (0 <= x < 2 ** z and 0 <= y < 2 ** z):
        return None
    layer, fmt, ext = BASEMAPS[name]
    f = TILES_DIR / name / str(z) / str(x) / f"{y}.{ext}"
    if f.exists() and time.time() - f.stat().st_mtime < TILE_MAX_AGE_DAYS * 86400:
        return f.read_bytes(), fmt
    params = {"SERVICE": "WMTS", "REQUEST": "GetTile", "VERSION": "1.0.0", "LAYER": layer, "STYLE": "normal",
              "TILEMATRIXSET": "PM", "TILEMATRIX": z, "TILEROW": y, "TILECOL": x, "FORMAT": fmt}
    for attempt in range(3):  # l'IGN renvoie parfois 404 pour une tuile qui existe : quelques essais
        try:
            r = requests.get(IGN_WMTS, params=params, headers=HEADERS, timeout=(5, 30))
        except requests.RequestException:
            break
        if r.status_code == 200 and r.headers.get("Content-Type", "").startswith("image/"):
            f.parent.mkdir(parents=True, exist_ok=True)
            tmp = f.with_name(f"{f.name}.{os.getpid()}.{threading.get_ident()}.part")
            tmp.write_bytes(r.content)
            os.replace(tmp, f)
            return r.content, fmt
        if r.status_code != 404 and r.status_code < 500:
            break
        time.sleep(0.3 * (attempt + 1))
    return (f.read_bytes(), fmt) if f.exists() else None


def purge_tiles():
    """Supprime les tuiles en cache de plus de TILE_MAX_AGE_DAYS ; renvoie (supprimées, gardées, octets gardés)."""
    cutoff = time.time() - TILE_MAX_AGE_DAYS * 86400
    removed = kept = size = 0
    for f in TILES_DIR.rglob("*"):
        if not f.is_file():
            continue
        st = f.stat()
        if st.st_mtime < cutoff or f.name.endswith(".part"):
            f.unlink(missing_ok=True)
            removed += 1
        else:
            kept += 1
            size += st.st_size
    for d in sorted((d for d in TILES_DIR.rglob("*") if d.is_dir()), key=lambda d: -len(d.parts)):
        if not any(d.iterdir()):
            d.rmdir()
    return removed, kept, size


# --------------------------------------------------------------------------- âge des données et mise à jour

def _global_sources():
    """Sources communes à toutes les communes : (clé, libellé, fichiers en cache)."""
    idfm = [RAW / f"idfm_gares_{'_'.join(sorted([*NETWORKS, *TRAM_MODES])).lower()}.geojson",
            RAW / "idfm_acces.csv", RAW / "idfm_relations_acces.csv"]
    return [
        ("idfm", "Gares, stations et accès (IDFM)", idfm),
        ("transit", "Horaires des transports (IDFM, GTFS)", [RAW / "idfm_gtfs.zip"]),
        ("gpe", "Gares du Grand Paris Express et arrêts de tramway en projet (IDFM)",
         [RAW / "idfm_projets_arrets.geojson", RAW / "idfm_projets_lignes.geojson"]),
        ("communes", "Contours des communes (geo.api.gouv.fr)", [RAW / "idf_communes.gpkg"]),
        ("airbruit", "Indice air-bruit (Bruitparif)", [RAW / "airbruit2024.zip"]),
        ("drieat", "Bruit ferroviaire (DRIEAT)", [RAW / "drieat_index.json"] + sorted((RAW / "drieat").glob("*.gpkg"))),
    ]


_osm_tiles_cache = {}


def _commune_files(code):
    """Fichiers en cache propres à une commune, par source."""
    if code not in _osm_tiles_cache:
        geom = gpd.GeoSeries([commune_geom(code).geometry], crs=4326).to_crs(2154).iloc[0]
        _osm_tiles_cache[code] = osm_tiles(osm_bounds(geom))
    return {
        "osm": [RAW / "osm" / f"{i}_{j}.json" for i, j in _osm_tiles_cache[code]],
        "airparif": [RAW / "airparif" / f"{pol}_{AIR_YEAR}_{code}.tif" for pol in AIR_POLLUTANTS],
        "bruit_route": sorted((RAW / "bruitparif_route" / code).glob("*.png")),
        "bruit_fer": sorted((RAW / "bruitparif_fer" / code).glob("*.png")),
        "quartiers": [RAW / "quartiers" / f"{code}.json"],
        "iris": [RAW / "iris" / f"{code}.json"],
    }


# groupes de couches à recalculer quand une source a plus de MAX_AGE_DAYS (None : quartiers seulement,
# refaits à chaque reconstruction ; "*" : tout, la grille pouvant changer)
SOURCE_GROUPS = {"idfm": "transport", "gpe": "transport", "transit": "destinations", "communes": "*", "airbruit": "bruit", "drieat": "bruit",
                 "osm": "transport", "airparif": "air", "bruit_route": "bruit", "bruit_fer": "bruit",
                 "quartiers": None, "iris": None}


def _groups_for(keys):
    groups = {SOURCE_GROUPS[k] for k in keys} - {None}
    return sorted(FORMATS) if "*" in groups else sorted(groups)


COMMUNE_SOURCE_LABELS = {"osm": "Réseau de rues (OpenStreetMap)", "airparif": "Pollution de l'air (Airparif)",
                         "bruit_route": "Bruit routier (Bruitparif)", "bruit_fer": "Bruit ferroviaire (Bruitparif)",
                         "quartiers": "Quartiers (Linternaute)", "iris": "Contours IRIS (IGN)"}
REFRESH_STATE = RAW / "refresh_state.json"  # communes restant à reconstruire d'une mise à jour interrompue


def _iso(ts):
    return time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else None


def freshness(max_age_days=MAX_AGE_DAYS):
    """Âge des données en cache : par source, fichiers de plus de max_age_days, et communes à reconstruire."""
    cutoff = time.time() - max_age_days * 86400
    sources, oldest_all = [], None

    def summarize(key, label, files):
        nonlocal oldest_all
        mt = [f.stat().st_mtime for f in files if f.exists()]
        if not mt:
            return None
        oldest = min(mt)
        oldest_all = oldest if oldest_all is None else min(oldest_all, oldest)
        entry = {"key": key, "label": label, "files": len(mt), "stale": sum(m < cutoff for m in mt), "oldest": _iso(oldest)}
        sources.append(entry)
        return entry

    global_stale = []  # sources communes périmées
    for key, label, files in _global_sources():
        e = summarize(key, label, files)
        if e and e["stale"]:
            global_stale.append(key)

    idx = WEB_DATA / "index.json"
    built = json.loads(idx.read_text())["communes"] if idx.exists() else []
    per_source = {k: set() for k in COMMUNE_SOURCE_LABELS}
    communes = []
    pending = _pending_refresh()
    for c in built:
        reasons, keys = [], list(global_stale)
        try:
            files = _commune_files(c["code"])
        except Exception:
            continue
        for k, fl in files.items():
            per_source[k].update(fl)
            if any(f.exists() and f.stat().st_mtime < cutoff for f in fl):
                reasons.append(COMMUNE_SOURCE_LABELS[k])
                keys.append(k)
        if global_stale:
            reasons.append("données communes à mettre à jour")
        groups = _groups_for(keys)
        if c["code"] in pending:
            reasons.append("mise à jour précédente interrompue")
            groups = sorted(set(groups) | set(pending[c["code"]]))
        if reasons:
            # groups : groupes de couches à recalculer (vide : quartiers seulement)
            communes.append({"code": c["code"], "nom": c["nom"], "reasons": reasons, "groups": groups})
    for k, fl in per_source.items():
        summarize(k, COMMUNE_SOURCE_LABELS[k], sorted(fl))
    return {"max_age_days": max_age_days, "cutoff": _iso(cutoff), "oldest": _iso(oldest_all),
            "sources": sources, "communes": communes,
            "to_update": bool(communes) or bool(global_stale)}


def _pending_refresh():
    """Communes restant à reconstruire d'une mise à jour interrompue : {code: groupes} (ancien format du
    fichier, simple liste de codes : tous les groupes)."""
    if not REFRESH_STATE.exists():
        return {}
    pending = json.loads(REFRESH_STATE.read_text())["pending"]
    return pending if isinstance(pending, dict) else {c: sorted(FORMATS) for c in pending}


def refresh_stale(log=print, on_commune=None, max_age_days=MAX_AGE_DAYS, executor=None, jobs=PARALLEL_BUILDS):
    """Retélécharge les données de plus de max_age_days puis reconstruit les communes concernées.
    Chaque fichier est remplacé seulement une fois la nouvelle version complète (cached, is_stale) ;
    chaque commune est reconstruite à côté de l'ancienne version, qui reste servie jusqu'au remplacement."""
    global REFRESH_BEFORE, _idf_cache
    report = freshness(max_age_days)
    if not report["to_update"]:
        log("aucune donnée à mettre à jour")
        return {}
    REFRESH_BEFORE = time.time() - max_age_days * 86400
    errors = {}
    try:
        if any(s["stale"] for s in report["sources"] if s["key"] in ("idfm", "gpe", "transit", "communes", "airbruit", "drieat")):
            log("mise à jour : gares et accès IDFM")
            idfm_tables()
            project_stations()
            transit_tables(log)  # horaires retéléchargés (cached) : nouvelles tables pour un nouveau jour
            log("mise à jour : contours des communes")
            idf_communes()
            _idf_cache = None
            log("mise à jour : indice air-bruit Bruitparif")
            bruitparif_gpkg(log)
            log("mise à jour : bruit ferroviaire DRIEAT")
            idx = drieat_index()
            for dep, entry in idx.items():
                for layer in entry["layers"]:
                    if (RAW / "drieat" / f"{layer.split(':')[-1]}.gpkg").exists():
                        drieat_layer_gpkg(dep, entry["wfs"], layer, log)
        todo = {c["code"]: c["groups"] for c in report["communes"]}
        REFRESH_STATE.write_text(json.dumps({"pending": todo}))
        done = 0

        def finished(code, err):  # en échec, la commune garde ses anciennes données
            nonlocal done
            done += 1
            log(f"mise à jour des communes ({done}/{len(todo)})")
            if err is None:
                pending = {c: g for c, g in _pending_refresh().items() if c != code}
                REFRESH_STATE.write_text(json.dumps({"pending": pending}))
            if on_commune:
                on_commune(code, err)
        # seulement les couches des sources périmées
        errors = build_many([(code, set(groups)) for code, groups in todo.items()], jobs, log, finished, executor)
        if not errors:
            REFRESH_STATE.unlink(missing_ok=True)
    finally:
        REFRESH_BEFORE = None
    return errors


# --------------------------------------------------------------------------- paquet des couches

PACK_NAME = "layers.pack"  # toutes les couches d'une commune, dans l'ordre de meta["layers"] : une seule requête


def layer_network(name):
    """Réseau d'une couche de temps ou de gare (« station_walk_tram3a » -> « tram3a »), None sinon."""
    m = re.fullmatch(r"(?:station_)?(?:walk|bike)_(.+)", name)
    return m.group(1) if m else None


def write_pack(d, names):
    tmp = d / f"{PACK_NAME}.{os.getpid()}.tmp"
    with open(tmp, "wb") as out:
        for name in names:
            out.write((d / f"{name}.bin").read_bytes())
    os.replace(tmp, d / PACK_NAME)


def pack_missing():
    """Crée layers.pack pour les communes construites avant son introduction (ou plus ancien que meta.json)."""
    n = 0
    for d in COMMUNES_DIR.iterdir():
        meta_f, pack = d / "meta.json", d / PACK_NAME
        if d.name.startswith(".") or not meta_f.exists():
            continue
        meta = json.loads(meta_f.read_text())
        names = meta.get("pack", list(meta["layers"]))
        if not pack.exists() or pack.stat().st_mtime < max((d / f"{k}.bin").stat().st_mtime for k in names):
            write_pack(d, names)
            n += 1
    return n


# --------------------------------------------------------------------------- compression

COMPRESSED_SUFFIXES = (".bin", ".geojson", ".json", ".pack")


def write_gz(path):
    """Écrit path.gz (servi tel quel par le serveur aux navigateurs qui acceptent gzip)."""
    tmp = path.with_name(path.name + f".gz.{os.getpid()}.tmp")
    tmp.write_bytes(gzip.compress(path.read_bytes(), compresslevel=6, mtime=0))
    os.replace(tmp, path.with_name(path.name + ".gz"))


def compress_dir(d):
    """Version compressée de chaque fichier qui n'en a pas (couches reprises : leur .gz l'est aussi), sur
    plusieurs fils (zlib libère le verrou de Python)."""
    files = [f for f in d.iterdir() if f.suffix in COMPRESSED_SUFFIXES and not f.with_name(f.name + ".gz").exists()]
    with ThreadPoolExecutor(KD_WORKERS) as pool:
        list(pool.map(write_gz, files))


def compress_missing():
    """Crée les .gz absents ou plus anciens que leur fichier (données construites avant la compression)."""
    n = 0
    for f in WEB_DATA.rglob("*"):
        if f.suffix in COMPRESSED_SUFFIXES and f.is_file() and ".part" not in str(f.parent):
            gz = f.with_name(f.name + ".gz")
            if not gz.exists() or gz.stat().st_mtime < f.stat().st_mtime:
                write_gz(f)
                n += 1
    return n


def outdated_communes():
    """Codes des communes dont un groupe de couches a un format antérieur (voir FORMATS)."""
    out = []
    for d in sorted(COMMUNES_DIR.iterdir()):
        f = d / "meta.json"
        if not d.name.startswith(".") and f.exists() and outdated_groups(json.loads(f.read_text())):
            out.append(d.name)
    return out


def remove_commune(code):
    shutil.rmtree(COMMUNES_DIR / code, ignore_errors=True)
    update_index()
