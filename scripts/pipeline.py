"""Préparation des données de l'application immo_map, commune par commune.

Chaque commune a sa propre grille (Web Mercator, ~10 m) écrite dans
web/data/communes/<code>/ :
  meta.json      description de la grille, des couches et des gares utilisées
  <couche>.bin   une couche raster par fichier (uint8 ou uint16, ligne par ligne, nord en haut)
  commune.geojson, stations.geojson, acces.geojson

web/data/index.json liste les communes construites ; web/data/global/ regroupe
les gares et accès de toutes les communes pour l'affichage. Les contours des zones
atteignables sont tracés par l'application à partir des temps de trajet.

Les téléchargements bruts sont mis en cache dans data/raw/ et partagés entre communes.
"""

import gzip
import json
import os
import re
import shutil
import threading
import time
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
from shapely.geometry import Point, box, shape
from shapely.ops import unary_union

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
DATA_FORMAT = 9                  # à incrémenter quand le contenu des données change : le serveur reconstruit les anciennes
AIR_POLLUTANTS = ["no2", "pm25", "pm10"]
# réseaux ferrés pris en compte pour le temps de marche : mode IDFM -> clé utilisée dans les données
NETWORKS = {"RER": "rer", "TRAIN": "transilien"}
TRAVEL_MODES = ["walk", "bike"]
MODE_NAMES = {"walk": "à pied", "bike": "à vélo"}

GEO_API = "https://geo.api.gouv.fr"
AIRPARIF_WCS = "https://namek.airparif.fr/geoserver/ows"
IDFM_API = "https://data.iledefrance-mobilites.fr/api/explore/v2.1/catalog/datasets"
OVERPASS = ["https://overpass-api.de/api/interpreter",  # serveurs essayés à tour de rôle
            "https://overpass.private.coffee/api/interpreter",
            "https://maps.mail.ru/osm/tools/overpass/api/interpreter"]
BRUITPARIF_ZIP = ("https://www.bruitparif.fr/pages/En-tete/800%20Le%20bruit%20en%20%C3%8Ele-de-France/"
                  "300%20carto-air-bruit-en-idf/600%20Opendata%20air-bruit/"
                  "Couches%20SIG%20air-bruit%202024_9_classes.zip")
DRIEAT_SEARCH = "Lot de données relatives aux cartes de bruit stratégiques"
# Cartes stratégiques de bruit E4 « consolidées » (agglomération + grandes infrastructures), route et fer,
# servie en images par le MapProxy de Bruitparif : on retrouve la classe Lden de chaque pixel par sa couleur.
BRUITPARIF_WMS = "https://raster.bruitparif.fr/mapproxy/service"
BRUITPARIF_LAYERS = {"route": "CSB4_w4echConso_Route_A_Lden", "fer": "CSB4_w4echConso_Fer_A_Lden"}
BRUITPARIF_LEGEND = {  # légende commune aux deux couches  # borne basse de la classe Lden (40 = moins de 45 dB) -> couleur de la légende
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
    "walk": f"Temps à pied : plus court chemin sur le réseau OpenStreetMap jusqu'aux entrées des gares (IDFM), {WALK_SPEED_KMH} km/h",
    "bike": f"Temps à vélo : réseau OpenStreetMap, sens uniques respectés (sauf contresens cyclables), {BIKE_SPEED_KMH} km/h ({BIKE_SLOW_KMH} km/h sur voies piétonnes)",
}

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


def cached(path, fetch):
    """Renvoie path, en le créant via fetch() (qui renvoie des bytes) s'il manque."""
    with lock_for(str(path)):
        if not path.exists():
            tmp = path.with_suffix(path.suffix + ".part")
            tmp.write_bytes(fetch())
            tmp.rename(path)
    return path


# --------------------------------------------------------------------------- communes

def idf_communes():
    """Contours de toutes les communes d'Île-de-France (pour la recherche et les requêtes spatiales)."""
    f = RAW / "idf_communes.gpkg"
    with lock_for(str(f)):
        if not f.exists():
            parts = []
            for dep in IDF_DEPTS:
                r = http_get(f"{GEO_API}/departements/{dep}/communes",
                             params={"format": "geojson", "geometry": "contour",
                                     "fields": "nom,code,codeDepartement,population"})
                parts.append(gpd.GeoDataFrame.from_features(r.json()["features"], crs=4326))
            g = pd.concat(parts, ignore_index=True)
            g = g[["code", "nom", "codeDepartement", "population", "geometry"]]
            g.to_file(f, driver="GPKG")
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


# --------------------------------------------------------------------------- gares

def idfm_tables():
    gares = gpd.read_file(cached(RAW / "idfm_gares_ferre.geojson", lambda: http_get(
        f"{IDFM_API}/emplacement-des-gares-idf/exports/geojson",
        params={"where": " or ".join(f'mode="{m}"' for m in NETWORKS)}).content)).to_crs(2154)
    gares = gares[gares.res_com.str.match(r"^(RER [A-E]|TRAIN [A-Z])$")]
    rel = pd.read_csv(cached(RAW / "idfm_relations_acces.csv", lambda: http_get(
        f"{IDFM_API}/relations-acces/exports/csv", params={"delimiter": ";"}).content), sep=";", dtype=str)
    acc = pd.read_csv(cached(RAW / "idfm_acces.csv", lambda: http_get(
        f"{IDFM_API}/acces/exports/csv", params={"delimiter": ";"}).content), sep=";", dtype=str)
    acc = acc[acc.accisentry.str.lower() == "true"].merge(rel[["zdaid", "accid"]], on="accid")
    return gares, acc


def stations_near(commune_l93):
    gares, acc = idfm_tables()
    zone = commune_l93.buffer(STATION_SEARCH_RADIUS_M)
    gares = gares[gares.within(zone)]

    stations = []
    for zdc, grp in gares.groupby("id_ref_zdc"):
        stations.append({
            "zdc": str(zdc),
            "nom": grp.nom_zdc.iloc[0],
            # « TRAIN P » -> « Transilien P » ; RER d'abord
            "lignes": sorted({l.replace("TRAIN ", "Transilien ") for l in grp.res_com}, key=lambda l: (l[0] != "R", l)),
            "networks": sorted({NETWORKS[m] for m in grp["mode"]}),
            "zdas": sorted(set(grp.id_ref_zda.astype(str))),
            "geometry": unary_union(list(grp.geometry)).centroid,
        })
    if not stations:
        return gpd.GeoDataFrame(columns=["zdc", "nom", "lignes", "networks", "zdas", "geometry"], geometry="geometry", crs=2154), \
            gpd.GeoDataFrame(columns=["station", "nom_acces", "geometry"], geometry="geometry", crs=2154)
    stations = gpd.GeoDataFrame(stations, crs=2154).sort_values("nom").reset_index(drop=True)

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

def osm_tile(i, j):
    """Voies (highway=*) d'une dalle OSM, téléchargée une fois via Overpass."""
    d = RAW / "osm"
    d.mkdir(exist_ok=True)
    dlat, dlon = OSM_TILE_DEG
    s, w = i * dlat, j * dlon
    query = f'[out:json][timeout:180];way["highway"]({s:.4f},{w:.4f},{s + dlat:.4f},{w + dlon:.4f});out body;>;out skel qt;'

    def fetch():
        err = ""
        for attempt in range(9):
            url = OVERPASS[attempt % len(OVERPASS)]
            try:
                r = requests.post(url, data={"data": query}, headers=HEADERS, timeout=300)
                if r.status_code == 200 and r.content[:1] == b"{":
                    return r.content
                err = f"HTTP {r.status_code}"
            except requests.RequestException as e:
                err = str(e)
            time.sleep(15 * (attempt // len(OVERPASS) + 1))  # 429 / 504 : serveurs chargés
        raise RuntimeError(f"Overpass indisponible ({err}), réessayez plus tard")
    return json.loads(cached(d / f"{i}_{j}.json", fetch).read_bytes())


def load_osm(bounds_wgs, log):
    w, s, e, n = bounds_wgs
    dlat, dlon = OSM_TILE_DEG
    tiles = [(i, j) for i in range(int(np.floor(s / dlat)), int(np.floor(n / dlat)) + 1)
             for j in range(int(np.floor(w / dlon)), int(np.floor(e / dlon)) + 1)]
    nodes, ways = {}, {}
    for k, (i, j) in enumerate(tiles):
        log(f"réseau OSM : dalle {k + 1}/{len(tiles)}")
        for el in osm_tile(i, j)["elements"]:
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


def travel_times(grid, stations, accesses, x, y, graphs, log):
    """Temps (s, uint16, 65535 = hors d'atteinte) et gare (uint8) par cellule, par mode et par réseau."""
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
        for net in NETWORKS.values():
            sel = acc[acc.station.map(lambda si: net in stations.networks.iloc[si])]
            t_cells = np.full(grid.shape, 65535, "uint16")
            s_cells = np.full(grid.shape, 255, "uint8")
            if len(sel):
                # un sommet virtuel par accès : arête voie la plus proche -> accès (coût = trajet d'approche)
                d0, k0 = tree.query(np.column_stack([sel.geometry.x, sel.geometry.y]))
                virt = n + np.arange(len(sel))
                va = np.concatenate([a, main[k0]]); vb = np.concatenate([b, virt])
                vw = np.concatenate([w, np.maximum(d0 / approach, 0.01)])
                g = csr_matrix((vw, (va, vb)), shape=(n + len(sel), n + len(sel)))
                # temps vers la gare : graphe inversé (sens uniques du vélo)
                dist, _, src = dijkstra(g.T, indices=virt, min_only=True, return_predecessors=True,
                                        limit=MAX_TIME_S)
                reach = node_ids[np.isfinite(dist[node_ids])]
                if len(reach):
                    rtree = cKDTree(np.column_stack([x[reach], y[reach]]))
                    dd, kk = rtree.query(np.column_stack([cxl, cyl]), k=4, distance_upper_bound=MAX_APPROACH_M)
                    ok = np.isfinite(dd)
                    kk = np.where(ok, kk, 0)
                    tot = np.where(ok, dist[reach[kk]] + dd / approach, np.inf)
                    best = tot.argmin(axis=1)
                    t = tot[np.arange(len(tot)), best]
                    station = np.array(sel.station.values)[src[reach[kk[np.arange(len(kk)), best]]] - n]
                    good = t <= MAX_TIME_S
                    t_cells.ravel()[good] = np.round(t[good]).astype("uint16")
                    s_cells.ravel()[good] = station[good]
            out[f"{mode}_{net}"] = t_cells
            out[f"station_{mode}_{net}"] = s_cells
        log(f"temps de trajet {MODE_NAMES[mode]} calculés")
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
        if not gpkg.exists():
            z = cached(RAW / "airbruit2024.zip", lambda: http_get(BRUITPARIF_ZIP).content)
            log("conversion de la carte Bruitparif (une seule fois, ~1 min)")
            src = f"/vsizip/{z}/AirBruit_2024.shp"
            n = pyogrio.read_info(src)["features"]
            tmp = gpkg.with_suffix(".part.gpkg")
            tmp.unlink(missing_ok=True)
            step = 200_000
            for start in range(0, n, step):
                g = pyogrio.read_dataframe(src, skip_features=start, max_features=step)
                g = g.rename(columns={"9": "code"})
                pyogrio.write_dataframe(g, tmp, layer="airbruit", append=start > 0)
            tmp.rename(gpkg)
    return gpkg


def bruitparif_layers(grid, log):
    """Code air-bruit = 10 × classe bruit + classe air."""
    g = pyogrio.read_dataframe(bruitparif_gpkg(log), bbox=grid.bounds_l93).to_crs(3857)
    code = g["code"].astype(int)
    return grid.burn(zip(g.geometry, code // 10)), grid.burn(zip(g.geometry, code % 10))


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
        if not gpkg.exists():
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
            out[out.lv > 0].to_file(gpkg, driver="GPKG")
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
        dist = np.sqrt(((rgba[..., None, :3].astype("float32") - palette) ** 2).sum(-1))
        k = dist.argmin(-1)
        ok = (rgba[..., 3] > 100) & (dist.min(-1) <= MAX_COLOR_DIST)
        classes[ty:ty + h, tx:tx + w] = np.where(ok, levels[k], 255)

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

def build_commune(code, log=print):
    row = commune_geom(code)
    nom = row.nom
    log(f"{nom} : préparation")
    commune = gpd.GeoDataFrame([{"code": code, "nom": nom}], geometry=[row.geometry], crs=4326)
    commune_l93 = commune.to_crs(2154).geometry.iloc[0]
    grid = Grid(commune_l93.bounds)

    stations, accesses = stations_near(commune_l93)
    log(f"{nom} : {len(stations)} gares, {len(accesses)} accès")

    layers = {"commune": grid.burn([(commune.to_crs(3857).geometry.iloc[0], 1)])}

    # Temps de trajet réels jusqu'à la gare la plus proche, par mode (marche, vélo) et par réseau ;
    # l'application combine les réseaux choisis pour le mode choisi.
    reseau = commune_l93.buffer(STATION_SEARCH_RADIUS_M + 500)
    bounds_wgs = transform_bounds(2154, 4326, *reseau.bounds)
    x, y, graphs = build_graphs(*load_osm(bounds_wgs, lambda m: log(f"{nom} : {m}")))
    if len(stations):
        layers.update(travel_times(grid, stations, accesses, x, y, graphs, lambda m: log(f"{nom} : {m}")))
    else:
        for mode in TRAVEL_MODES:
            for net in NETWORKS.values():
                layers[f"{mode}_{net}"] = np.full(grid.shape, 65535, "uint16")
                layers[f"station_{mode}_{net}"] = np.full(grid.shape, 255, "uint8")

    log(f"{nom} : pollution de l'air (Airparif)")
    for pol in AIR_POLLUTANTS:
        layers[pol] = airparif_layer(grid, pol, code)

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

    out = COMMUNES_DIR / code
    tmp = COMMUNES_DIR / f".{code}.{os.getpid()}.part"  # propre au processus (serveur et CLI peuvent coexister)
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    meta_layers = {}
    for name, arr in layers.items():
        (tmp / f"{name}.bin").write_bytes(np.ascontiguousarray(arr).tobytes())
        meta_layers[name] = {"dtype": str(arr.dtype)}

    commune.to_file(tmp / "commune.geojson", driver="GeoJSON")
    st = stations.to_crs(4326)
    gpd.GeoDataFrame({"zdc": st.zdc, "nom": st.nom, "lignes": st.lignes.map(" + ".join),
                      "networks": st.networks.map(",".join)},
                     geometry=st.geometry, crs=4326).to_file(tmp / "stations.geojson", driver="GeoJSON")
    accesses[["zdc", "nom_acces", "geometry"]].to_crs(4326).to_file(tmp / "acces.geojson", driver="GeoJSON")

    dep = row.codeDepartement
    meta = {
        "format": DATA_FORMAT,
        "code": code, "nom": nom, "dep": dep,
        "width": grid.width, "height": grid.height, "bounds": grid.latlng_bounds(),
        "merc": {"left": grid.left, "top": grid.top, "cell": CELL_M},
        "row_cell_area_m2": grid.row_cell_area_m2(),
        "layers": meta_layers,
        "stations": [{"zdc": z, "nom": n, "lignes": list(l), "networks": list(k)}
                     for z, n, l, k in zip(stations.zdc, stations.nom, stations.lignes, stations.networks)],
        "networks": list(NETWORKS.values()),
        "modes": TRAVEL_MODES,
        "bike_speed_kmh": BIKE_SPEED_KMH,
        "walk_speed_kmh": WALK_SPEED_KMH,
        "durations": DURATIONS_MIN,
        "air_year": AIR_YEAR,
        "fer_coverage": round(float((layers["lden_fer"][layers["commune"] > 0] > 0).mean()), 3),
        "route_coverage": round(float((layers["lden_route"][layers["commune"] > 0] > 0).mean()), 3),
        "built": time.strftime("%Y-%m-%d %H:%M"),
        "air_range": air_range(layers),
    }
    (tmp / "meta.json").write_text(json.dumps(meta, ensure_ascii=False))
    compress_dir(tmp)
    shutil.rmtree(out, ignore_errors=True)
    tmp.rename(out)
    update_index()
    log(f"{nom} : terminé")
    return meta


# --------------------------------------------------------------------------- index et couches globales

_index_lock = threading.Lock()


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
                             "format": m.get("format", 1)})
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
            {"communes": communes, "sources": SOURCES, "durations": DURATIONS_MIN, "air_year": AIR_YEAR},
            ensure_ascii=False))
        os.replace(tmp, WEB_DATA / "index.json")


# --------------------------------------------------------------------------- résumés et statistiques

def air_range(layers):
    """Min et max (µg/m³) de chaque polluant dans la commune : plages des curseurs de l'application."""
    inside = layers["commune"] > 0
    return {pol: [float(layers[pol][inside].min()) / 10, float(layers[pol][inside].max()) / 10]
            for pol in AIR_POLLUTANTS if inside.any()}


def read_layers(code):
    d = COMMUNES_DIR / code
    meta = json.loads((d / "meta.json").read_text())
    layers = {name: np.fromfile(d / f"{name}.bin", dtype=info["dtype"]).reshape(meta["height"], meta["width"])
              for name, info in meta["layers"].items()}
    return meta, layers


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


_stats_cache = {}  # code -> (built, cellules de la commune, surfaces, couches restreintes à ces cellules)


def commune_cells(code):
    built = json.loads((COMMUNES_DIR / code / "meta.json").read_text())["built"]
    hit = _stats_cache.get(code)
    if hit and hit[0] == built:
        return hit
    meta, layers = read_layers(code)
    inside = layers["commune"] > 0
    rows = np.nonzero(inside)[0]
    area = np.asarray(meta["row_cell_area_m2"], dtype="float64")[rows]
    sub = {k: v[inside] for k, v in layers.items() if k != "commune"}
    _stats_cache[code] = hit = (built, inside, area, sub)
    return hit


def zone_stats(codes, q):
    """Surfaces (m²) par commune : totale, retenue, et respectant chaque critère pris seul.
    Mêmes règles que l'application (web/app.js, computeCommune)."""
    out = {}
    nets = [n for n in q.get("networks", []) if n in NETWORKS.values()]
    mode = q.get("mode", "walk")
    limit = q.get("walk")                      # minutes, ou None sans filtre de temps
    limit_s = limit * 60 + 29 if limit is not None else 65535
    air = q.get("air", {})
    for code in codes:
        if not (COMMUNES_DIR / code / "meta.json").exists():
            continue
        _, _, area, v = commune_cells(code)
        t = np.full(len(area), 65535, "uint16")
        for n in nets:
            if f"{mode}_{n}" in v:
                t = np.minimum(t, v[f"{mode}_{n}"])
        walk = t <= limit_s
        airok = np.ones(len(area), bool)
        for pol in AIR_POLLUTANTS:
            if air.get(pol) is not None:
                airok &= v[pol] <= air[pol] * 10 + 0.5
        bp = v["bp_noise"] <= q.get("bp", 3)
        route = v["lden_route"] < q.get("route", 999)
        fer = v["lden_fer"] < q.get("fer", 999)
        ok = walk & airok & bp & route & fer
        out[code] = {"total": float(area.sum()), "ok": float(area[ok].sum()),
                     "crit": {"walk": float(area[walk].sum()), "air": float(area[airok].sum()),
                              "bp": float(area[bp].sum()), "route": float(area[route].sum()),
                              "fer": float(area[fer].sum())}}
    return out


# --------------------------------------------------------------------------- compression

COMPRESSED_SUFFIXES = (".bin", ".geojson", ".json")


def write_gz(path):
    """Écrit path.gz (servi tel quel par le serveur aux navigateurs qui acceptent gzip)."""
    tmp = path.with_name(path.name + f".gz.{os.getpid()}.tmp")
    tmp.write_bytes(gzip.compress(path.read_bytes(), compresslevel=6, mtime=0))
    os.replace(tmp, path.with_name(path.name + ".gz"))


def compress_dir(d):
    for f in d.iterdir():
        if f.suffix in COMPRESSED_SUFFIXES:
            write_gz(f)


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
    """Codes des communes construites avec un format de données antérieur."""
    idx = WEB_DATA / "index.json"
    if not idx.exists():
        return []
    return [c["code"] for c in json.loads(idx.read_text())["communes"] if c.get("format", 1) < DATA_FORMAT]


def remove_commune(code):
    shutil.rmtree(COMMUNES_DIR / code, ignore_errors=True)
    update_index()
