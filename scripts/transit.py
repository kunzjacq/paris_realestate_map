"""Durée en transports en commun depuis chaque gare jusqu'à quelques gares destinations.

    .venv-local/bin/python scripts/transit.py            # calcule (ou relit) les tables et en affiche un extrait

Horaires théoriques IDFM (GTFS, « offre-horaires-tc-gtfs-idfm ») : RER, Transilien, métro et tramway d'un
mardi (SERVICE_WEEKDAY) de la période couverte par le fichier. Pour chaque destination, profil complet des
départs de chaque arrêt (Connection Scan Algorithm en profil, une seule passe sur les horaires) : pour tout
départ, heure d'arrivée au plus tôt à destination, attente et correspondances (temps de marche du GTFS)
comprises. Résultat par gare (zone de correspondance IDFM, comme les gares de l'application) et par période
(PERIODS) : durée médiane, 10e et 90e centiles sur les départs de chaque minute de la période, et ligne
empruntée au départ le plus souvent. Le pipeline en tire les temps porte à porte de chaque commune.

Scénarios (SCENARIOS) : réseau actuel, et réseau prévu à une date, avec les lignes en projet ouvertes d'ici là
(Grand Paris Express, prolongements de tramway). Leurs horaires sont estimés (project_timetable) : gares
ordonnées le long des tracés IDFM, temps de parcours tirés des vitesses commerciales annoncées (métro) ou de
celles des lignes existantes (tramway), passages réguliers, correspondances forfaitaires. La part des départs
dont le meilleur itinéraire emprunte une ligne en projet est donnée par gare (« proj ») : durée estimée.
"""

import bisect
import collections
import json
import sys
import time
import zipfile
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from pyproj import Transformer
from scipy.spatial import cKDTree
from shapely.geometry import Point
from shapely.ops import linemerge, unary_union

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipeline import RAW, cached, http_get, project_stops, project_zdc  # noqa: E402

GTFS_URL = ("https://data.iledefrance-mobilites.fr/explore/dataset/offre-horaires-tc-gtfs-idfm/files/"
            "a925e164271e4bca93433756d6a340d1/download/")
GTFS_ZIP = RAW / "idfm_gtfs.zip"
RAIL_ROUTE_TYPES = {0, 1, 2}  # tramway, métro, train (RER et Transilien) ; ni bus ni câble
SERVICE_WEEKDAY = 1           # mardi : jour de semaine ordinaire
# périodes étudiées : clé -> (libellé, début, fin des départs, en secondes depuis minuit)
PERIODS = {
    "pointe": ("Heure de pointe du matin (7 h 30 – 9 h 30)", 7 * 3600 + 30 * 60, 9 * 3600 + 30 * 60),
    "journee": ("Milieu de journée (11 h – 15 h)", 11 * 3600, 15 * 3600),
}
# destinations : clé -> (nom, zones de correspondance IDFM des quais d'arrivée)
DESTINATIONS = {
    "chatelet": ("Châtelet-Les Halles", ["474151", "71264"]),
    "defense": ("La Défense", ["71517"]),
    "gare_lyon": ("Gare de Lyon", ["73626"]),
    "saint_lazare": ("Saint-Lazare", ["71370", "73688"]),
    "montparnasse": ("Montparnasse", ["71139"]),
    "gare_nord": ("Gare du Nord", ["71410", "478733"]),
}
# scénarios : clé -> date (lignes en projet ouvertes au plus tard à cette date ; None : réseau actuel)
SCENARIOS = {"actuel": None, "2027": "2027-12-31", "2031": "2031-12-31"}

# Hypothèses des lignes en projet (Grand Paris Express : chiffres annoncés par la Société des grands projets)
# vitesses calées sur les temps de parcours annoncés, longueurs mesurées sur les tracés IDFM
GPE_SPEED_KMH = {"15": 33 / (37 / 60),       # 15 Sud : 33 km de Pont de Sèvres à Noisy-Champs en 37 min
                 "16": 27.9 / (26 / 60),     # Saint-Denis Pleyel – Noisy-Champs en ~26 min
                 "17": 26.2 / (25.5 / 60),   # Saint-Denis Pleyel – Le Mesnil-Amelot en un peu plus de 25 min
                 "18": 33.5 / (33.5 / 60)}   # Aéroport d'Orly – Versailles Chantiers en un peu plus de 33 min
GPE_PEAK_HEADWAY_S = {"15": 120, "16": 180, "17": 180, "18": 180}  # intervalle à la pointe
TRAM_SPEED_KMH, TRAM_HEADWAY_S = 18, {"pointe": 360, "journee": 600}  # si la ligne existante n'en dit rien
REFERENCE_LINE = "14"         # métro automatique : rapport des intervalles hors pointe / pointe
STRAIGHT_DETOUR = 1.15        # distance le long de la ligne / distance à vol d'oiseau, faute de tracé
TRANSFER_RADIUS_M = 400       # correspondance entre une gare en projet et les quais voisins
TRANSFER_WALK_MS = 1.0        # vitesse de marche en correspondance (couloirs, escaliers)
TRANSFER_EXTRA_S = {"métro": 240, "tram": 60}  # en plus de la marche : gare profonde du Grand Paris Express
SAME_STATION_TRANSFER_S = 120  # entre deux lignes en projet d'une même gare

HORIZON = 3 * 3600      # horaires lus jusqu'à la fin de la dernière période + HORIZON
MIN_CHANGE_S = 60       # correspondance sur le même quai (descente puis montée)
TABLE_FORMAT = 3        # à incrémenter quand le calcul des tables change (nom du fichier en cache)
INF = 10 ** 9
WINDOW = (min(p[1] for p in PERIODS.values()), max(p[2] for p in PERIODS.values()) + HORIZON)
TO_L93 = Transformer.from_crs(4326, 2154, always_xy=True)


def gtfs_zip():
    return cached(GTFS_ZIP, lambda: http_get(GTFS_URL).content)


def service_day():
    """Premier mardi après le téléchargement des horaires (le fichier couvre les 30 jours suivants)."""
    d = date.fromtimestamp(gtfs_zip().stat().st_mtime) + timedelta(days=1)
    while d.weekday() != SERVICE_WEEKDAY:
        d += timedelta(days=1)
    return d.isoformat()


def hms(s):
    h, m, sec = s.split(":")
    return int(h) * 3600 + int(m) * 60 + int(sec)


def rail_timetable(day):
    """Horaires ferrés du jour (connexions arrêt -> arrêt suivant d'une même course), mis en cache :
    la lecture de stop_times.txt (1 Go) prend quelques secondes à une minute."""
    start, end = WINDOW
    f = RAW / f"gtfs_rail_{day}_{start}_{end}_v2.npz"
    if f.exists():
        z = np.load(f, allow_pickle=True)
        return {k: z[k] for k in z.files}
    with zipfile.ZipFile(gtfs_zip()) as zf:
        read = lambda name, **kw: pd.read_csv(zf.open(name), dtype=str, **kw)
        routes = read("routes.txt", usecols=["route_id", "route_short_name", "route_type"])
        routes = routes[routes.route_type.astype(int).isin(RAIL_ROUTE_TYPES)]
        cal, dates = read("calendar.txt"), read("calendar_dates.txt")
        ymd = day.replace("-", "")
        wd = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"][date.fromisoformat(day).weekday()]
        active = set(cal[(cal.start_date <= ymd) & (cal.end_date >= ymd) & (cal[wd] == "1")].service_id)
        d = dates[dates.date == ymd]
        active = (active | set(d[d.exception_type == "1"].service_id)) - set(d[d.exception_type == "2"].service_id)
        trips = read("trips.txt", usecols=["route_id", "service_id", "trip_id"])
        trips = trips[trips.route_id.isin(set(routes.route_id)) & trips.service_id.isin(active)]
        keep = set(trips.trip_id)
        st = pd.concat(chunk[chunk.trip_id.isin(keep)] for chunk in read(
            "stop_times.txt", usecols=["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"],
            chunksize=2_000_000))
        stops = read("stops.txt", usecols=["stop_id", "stop_name", "parent_station", "stop_lon", "stop_lat"])
        transfers = read("transfers.txt")
    st["seq"] = st.stop_sequence.astype(int)
    st = st.sort_values(["trip_id", "seq"])
    st["arr"], st["dep"] = st.arrival_time.map(hms), st.departure_time.map(hms)
    nxt = st.groupby("trip_id").shift(-1)
    c = pd.DataFrame({"trip": st.trip_id, "from": st.stop_id, "to": nxt.stop_id, "dep": st.dep, "arr": nxt.arr})
    c = c[c.to.notna() & (c.dep <= end) & (c.arr >= start)]
    line_of = trips.set_index("trip_id").route_id.map(routes.set_index("route_id").route_short_name)
    used = pd.unique(pd.concat([c["from"], c["to"]]))
    t = transfers[transfers.from_stop_id.isin(used) & transfers.to_stop_id.isin(used)
                  & (transfers.from_stop_id != transfers.to_stop_id)]
    s = stops[stops.stop_id.isin(used)]
    x, y = TO_L93.transform(s.stop_lon.astype(float).to_numpy(), s.stop_lat.astype(float).to_numpy())
    out = {
        "c_trip": c.trip.astype("category").cat.codes.to_numpy().astype("int64"), "c_from": c["from"].to_numpy(),
        "c_to": c["to"].to_numpy(), "c_dep": c.dep.to_numpy(), "c_arr": c.arr.to_numpy(),
        "c_line": c.trip.map(line_of).fillna("").to_numpy(), "c_proj": np.zeros(len(c), bool),
        "t_from": t.from_stop_id.to_numpy(), "t_to": t.to_stop_id.to_numpy(),
        "t_time": t.min_transfer_time.fillna("0").astype(int).to_numpy(),
        "s_id": s.stop_id.to_numpy(), "s_name": s.stop_name.to_numpy(),
        "s_parent": s.parent_station.fillna("").str.replace("IDFM:", "").to_numpy(), "s_x": x, "s_y": y,
    }
    np.savez(f, **out)
    return out


# --------------------------------------------------------------------------- lignes en projet : horaires estimés

def line_stats(tt, line, window):
    """Vitesse (km/h, à vol d'oiseau × STRAIGHT_DETOUR) et intervalle (s, sens le plus desservi) d'une ligne
    existante sur la plage window ; None si la ligne n'y circule pas."""
    m = (tt["c_line"] == line) & (tt["c_dep"] >= window[0]) & (tt["c_dep"] < window[1])
    if m.sum() < 10:
        return None
    pos = {s: (x, y) for s, x, y in zip(tt["s_id"], tt["s_x"], tt["s_y"])}
    a, b = tt["c_from"][m], tt["c_to"][m]
    dist = np.array([np.hypot(pos[i][0] - pos[j][0], pos[i][1] - pos[j][1]) for i, j in zip(a, b)]) * STRAIGHT_DETOUR
    secs = (tt["c_arr"][m] - tt["c_dep"][m]).clip(min=1)
    pairs = collections.Counter(zip(a, b))
    return dist.sum() / secs.sum() * 3.6, (window[1] - window[0]) / pairs.most_common(1)[0][1]


def project_routes(cutoff):
    """Lignes en projet ouvertes d'ici cutoff : [(mode, ligne, [(zdc, nom, x, y)], [distances (m)])].
    Chaque opération et phase est une section, ses arrêts ordonnés le long de son tracé (ou de proche en proche
    depuis un terminus si le tracé ne passe pas par les arrêts) ; les sections d'une ligne qui se suivent (même
    gare à une extrémité) sont raccordées."""
    arrets, lignes = project_stops()
    arrets = arrets[arrets.date <= cutoff]
    sections = []
    for (mode, line, op, phase), grp in arrets.groupby(["mode", "indice", "id_operati", "phase"], dropna=False):
        geo = lignes[(lignes.id_operati == op) & ((lignes.phase == phase) | lignes.phase.isna())].geometry
        track = unary_union(list(geo)) if len(geo) else None
        if track is not None and track.geom_type == "MultiLineString":
            track = linemerge(track)
        stops = {}
        for r in grp.itertuples():
            stops.setdefault(project_zdc(mode, line, r.nom_arret), (r.nom_arret, r.geometry, r.terminus))
        items = [(z, n, g, t) for z, (n, g, t) in stops.items()]
        if track is not None and track.geom_type == "LineString" and max(track.distance(g) for _, _, g, _ in items) < 300:
            items.sort(key=lambda it: track.project(it[2]))
            pos = [track.project(g) for _, _, g, _ in items]
            dists = list(np.diff(pos))
        else:  # de proche en proche depuis un terminus (ou l'arrêt le plus excentré)
            start = next((it for it in items if it[3] == 1), None) or max(
                items, key=lambda it: it[2].distance(unary_union([g for _, _, g, _ in items]).centroid))
            order, rest = [start], [it for it in items if it is not start]
            while rest:
                nxt = min(rest, key=lambda it: it[2].distance(order[-1][2]))
                order.append(nxt)
                rest.remove(nxt)
            items = order
            dists = [a[2].distance(b[2]) * STRAIGHT_DETOUR for a, b in zip(items, items[1:])]
        if len(items) >= 2:
            sections.append([mode, line, [(z, n, g.x, g.y) for z, n, g, _ in items], dists, op])
    # raccordement des sections d'une même ligne par leurs extrémités communes
    merged = True
    while merged:
        merged = False
        for i, a in enumerate(sections):
            for j, b in enumerate(sections):
                if i >= j or a[:2] != b[:2]:
                    continue
                for ra, rb in ((False, False), (True, False), (False, True), (True, True)):
                    sa = (a[2][::-1], a[3][::-1]) if ra else (a[2], a[3])
                    sb = (b[2][::-1], b[3][::-1]) if rb else (b[2], b[3])
                    if sa[0][-1][0] == sb[0][0][0] and not {z for z, *_ in sa[0]} & {z for z, *_ in sb[0][1:]}:
                        sections[i] = [a[0], a[1], sa[0] + sb[0][1:], sa[1] + sb[1], a[4]]
                        del sections[j]
                        merged = True
                        break
                if merged:
                    break
            if merged:
                break
    # puis les sections d'une même ligne de métro sans gare commune (ligne 18 : Massy-Palaiseau / Massy
    # Opéra), ou d'une même opération de tramway, par leurs extrémités les plus proches
    while True:
        best = None
        for i, a in enumerate(sections):
            for j, b in enumerate(sections):
                if i >= j or a[:2] != b[:2] or {z for z, *_ in a[2]} & {z for z, *_ in b[2]}:
                    continue
                limit = 8000 if a[0] == "métro" else (1000 if a[4] == b[4] else 0)
                for ra, rb in ((False, False), (True, False), (False, True), (True, True)):
                    ea, eb = (a[2][0] if ra else a[2][-1]), (b[2][-1] if rb else b[2][0])
                    d = np.hypot(ea[2] - eb[2], ea[3] - eb[3])
                    if d < limit and (best is None or d < best[0]):
                        best = (d, i, j, ra, rb)
        if best is None:
            return sections
        d, i, j, ra, rb = best
        a, b = sections[i], sections[j]
        sa = (a[2][::-1], a[3][::-1]) if ra else (a[2], a[3])
        sb = (b[2][::-1], b[3][::-1]) if rb else (b[2], b[3])
        sections[i] = [a[0], a[1], sa[0] + sb[0], sa[1] + [d * STRAIGHT_DETOUR] + sb[1], a[4]]
        del sections[j]


def project_timetable(tt, cutoff, log=print):
    """Horaires estimés des lignes en projet ouvertes d'ici cutoff, au format de rail_timetable (connexions,
    quais, correspondances), à ajouter aux horaires réels."""
    ref = {pk: line_stats(tt, REFERENCE_LINE, (s, e)) for pk, (_, s, e) in PERIODS.items()}
    offpeak = ref["journee"][1] / ref["pointe"][1] if ref["pointe"] and ref["journee"] else 2.0
    peak_from, peak_to = PERIODS["pointe"][1] - 3600, PERIODS["pointe"][2] + 3600  # plage à fréquence de pointe
    routes = project_routes(cutoff)
    conns, stops, trip = [], {}, int(tt["c_trip"].max()) + 1
    for k, (mode, line, items, dists, _) in enumerate(routes):
        if mode == "métro":
            speed = GPE_SPEED_KMH[line]
            headway = {"pointe": GPE_PEAK_HEADWAY_S[line], "journee": GPE_PEAK_HEADWAY_S[line] * offpeak}
            label = line
        else:  # prolongement de tramway : vitesse et fréquence de la ligne existante
            label = f"T{line}"
            stats = {pk: line_stats(tt, label, (s, e)) for pk, (_, s, e) in PERIODS.items()}
            speed = stats["pointe"][0] if stats["pointe"] else TRAM_SPEED_KMH
            headway = {pk: (stats[pk][1] if stats[pk] else TRAM_HEADWAY_S[pk]) for pk in PERIODS}
        runs = [d / (speed / 3.6) for d in dists]
        ids = [f"PRJ:{k}:{z}" for z, *_ in items]
        for sid, (z, n, x, y) in zip(ids, items):
            stops[sid] = (n, z, x, y, mode)
        for seq, times in ((ids, runs), (ids[::-1], runs[::-1])):  # deux sens
            t0 = WINDOW[0] - sum(times)
            while t0 <= WINDOW[1]:
                t = t0
                for a, b, run in zip(seq, seq[1:], times):
                    conns.append((trip, a, b, int(t), int(t + run), label))
                    t += run
                trip += 1
                t0 += headway["pointe"] if peak_from <= t0 <= peak_to else headway["journee"]
        log(f"ligne en projet {label} : {len(items)} gares, {sum(dists) / 1000:.1f} km, {sum(runs) / 60:.0f} min")
    # correspondances : quais voisins (existants ou en projet) et lignes en projet d'une même gare
    s_ids = list(tt["s_id"]) + list(stops)
    xy = np.array(list(zip(tt["s_x"], tt["s_y"])) + [(v[2], v[3]) for v in stops.values()])
    tree = cKDTree(xy)
    t_from, t_to, t_time = [], [], []
    for sid, (n, z, x, y, mode) in stops.items():
        for j in tree.query_ball_point((x, y), TRANSFER_RADIUS_M):
            other = s_ids[j]
            if other == sid:
                continue
            same = other in stops and stops[other][1] == z
            w = SAME_STATION_TRANSFER_S if same else (
                np.hypot(xy[j][0] - x, xy[j][1] - y) / TRANSFER_WALK_MS + TRANSFER_EXTRA_S[mode])
            t_from += [sid, other]
            t_to += [other, sid]
            t_time += [int(w), int(w)]
    c = np.array(conns, dtype=object).reshape(-1, 6)
    return {
        "c_trip": c[:, 0].astype("int64"), "c_from": c[:, 1].astype(str), "c_to": c[:, 2].astype(str),
        "c_dep": c[:, 3].astype("int64"), "c_arr": c[:, 4].astype("int64"), "c_line": c[:, 5].astype(str),
        "c_proj": np.ones(len(c), bool),
        "t_from": np.array(t_from, dtype=object), "t_to": np.array(t_to, dtype=object), "t_time": np.array(t_time),
        "s_id": np.array(list(stops), dtype=object), "s_name": np.array([v[0] for v in stops.values()], dtype=object),
        "s_parent": np.array([v[1] for v in stops.values()], dtype=object),
        "s_x": np.array([v[2] for v in stops.values()]), "s_y": np.array([v[3] for v in stops.values()]),
    }


def merged_timetable(tt, extra):
    return {k: np.concatenate([tt[k], extra[k]]) for k in tt}


# --------------------------------------------------------------------------- profils et durées

class Profile:
    """Profil d'un arrêt : départs non dominés (départ, arrivée à destination, ligne prise, ligne en projet
    empruntée), par départ croissant."""
    __slots__ = ("deps", "arrs", "lines", "projs")

    def __init__(self):
        self.deps, self.arrs, self.lines, self.projs = [], [], [], []

    def best(self, t):
        """(arrivée au plus tôt, ligne, ligne en projet empruntée) en partant de l'arrêt à l'heure t ou après."""
        i = bisect.bisect_left(self.deps, t)
        return (self.arrs[i], self.lines[i], self.projs[i]) if i < len(self.deps) else (INF, "", False)

    def add(self, dep, arr, line, proj):
        i = bisect.bisect_left(self.deps, dep)
        if i < len(self.deps) and self.arrs[i] <= arr:
            return False  # un départ plus tardif arrive aussi tôt
        j = i
        while j > 0 and self.arrs[j - 1] >= arr:  # départs plus tôt qui n'arrivent pas avant : dominés
            j -= 1
        self.deps[j:i], self.arrs[j:i], self.lines[j:i], self.projs[j:i] = [dep], [arr], [line], [proj]
        return True


def profiles_to(tt, targets):
    """Profils de tous les arrêts vers les arrêts targets (CSA en profil, connexions par départ décroissant)."""
    walk_in = {}  # arrêt x -> [(y, durée de marche de y à x)]
    for a, b, w in zip(tt["t_from"], tt["t_to"], tt["t_time"]):
        walk_in.setdefault(b, []).append((a, int(w)))
    to_target = {s: 0 for s in targets}  # durée de marche jusqu'à la destination
    for s in targets:
        for y, w in walk_in.get(s, []):
            to_target[y] = min(to_target.get(y, INF), w)
    prof, trip_best = {}, {}
    deps, arrs, frm, to, trips, lines, projs = (tt[k] for k in ("c_dep", "c_arr", "c_from", "c_to", "c_trip",
                                                                  "c_line", "c_proj"))
    for k in np.argsort(-deps, kind="stable"):
        dep, arr, a, b, trip = int(deps[k]), int(arrs[k]), frm[k], to[k], trips[k]
        best = (arr + to_target[b], False) if b in to_target else (INF, False)   # descente à destination
        best = min(best, trip_best.get(trip, (INF, False)))                     # rester dans la rame
        p = prof.get(b)
        if p is not None:
            arr2, _, proj2 = p.best(arr + MIN_CHANGE_S)                         # correspondance
            best = min(best, (arr2, proj2))
        tau, proj = best[0], best[1] or bool(projs[k])
        if tau >= INF:
            continue
        trip_best[trip] = (tau, proj)
        if prof.setdefault(a, Profile()).add(dep, tau, lines[k], proj):
            for y, w in walk_in.get(a, []):                                    # venir à pied d'un quai voisin
                prof.setdefault(y, Profile()).add(dep - w, tau, lines[k], proj)
    return prof


def station_durations(tt, prof, start, end):
    """Par zone de correspondance : durées (min) pour un départ à chaque minute de start à end (médiane,
    10e et 90e centiles), ligne prise au départ le plus souvent, part des départs empruntant une ligne en
    projet (proj)."""
    quays = {}
    for sid, parent in zip(tt["s_id"], tt["s_parent"]):
        if parent and sid in prof:
            quays.setdefault(parent, []).append(prof[sid])
    out = {}
    for zdc, ps in quays.items():
        d, used, proj = [], collections.Counter(), 0
        for t in range(start, end + 1, 60):
            arr, line, pr = min(p.best(t) for p in ps)
            if arr < INF:
                d.append((arr - t) / 60)
                used[line] += 1
                proj += pr
        if d:
            out[zdc] = {"median": round(float(np.median(d)), 1), "p10": round(float(np.percentile(d, 10)), 1),
                        "p90": round(float(np.percentile(d, 90)), 1), "line": used.most_common(1)[0][0],
                        **({"proj": round(proj / len(d), 2)} if proj else {})}
    return out


def transit_tables(log=print):
    """{"day", "destinations": {clé: nom}, "periods": {clé: libellé}, "scenarios": {clé: date ou None},
    "tables": {scénario: {destination: {période: {zdc: {median, p10, p90, line, proj}}}}}}, en cache dans
    data/raw/transit_<jour>_v<TABLE_FORMAT>.json. Gares en projet : identifiants de pipeline.project_zdc."""
    day = service_day()
    f = RAW / f"transit_{day}_v{TABLE_FORMAT}.json"
    if f.exists():
        return json.loads(f.read_text())
    t0 = time.time()
    base = rail_timetable(day)
    log(f"horaires du {day} : {len(base['c_dep'])} connexions ferrées ({time.time() - t0:.0f} s)")
    tables = {}
    for sk, cutoff in SCENARIOS.items():
        tt = base if cutoff is None else merged_timetable(base, project_timetable(base, cutoff, log))
        tables[sk] = {}
        for key, (nom, zdcs) in DESTINATIONS.items():
            targets = {s for s, parent in zip(tt["s_id"], tt["s_parent"]) if parent in zdcs}
            prof = profiles_to(tt, targets)
            per = {pk: station_durations(tt, prof, start, end) for pk, (_, start, end) in PERIODS.items()}
            for durations in per.values():  # depuis la destination elle-même : on y est déjà
                durations.update({z: {"median": 0.0, "p10": 0.0, "p90": 0.0, "line": ""} for z in zdcs})
            tables[sk][key] = per
        log(f"temps en transports, scénario {sk} : calculés")
    out = {"day": day, "destinations": {k: v[0] for k, v in DESTINATIONS.items()},
           "periods": {k: v[0] for k, v in PERIODS.items()}, "scenarios": SCENARIOS, "tables": tables}
    tmp = f.with_name(f.name + ".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False))
    tmp.replace(f)
    return out


if __name__ == "__main__":
    t = transit_tables()
    names = {}
    tt = rail_timetable(t["day"])
    for name, parent in zip(tt["s_name"], tt["s_parent"]):
        names.setdefault(name, parent)
    names.update({"Champigny Centre (15)": project_zdc("métro", "15", "Champigny Centre"),
                  "Saint-Denis Pleyel (GPE)": project_zdc("métro", "16", "Saint-Denis Pleyel")})
    sample = ["Champigny", "Champigny Centre (15)", "Villejuif Louis Aragon", "Saint-Maur - Créteil",
              "Massy - Palaiseau", "Saint-Denis Pleyel (GPE)"]
    for sk in t["scenarios"]:
        for key in ("chatelet", "defense"):
            r = t["tables"][sk][key]["pointe"]
            print(f"{sk:7} {t['destinations'][key]:20} " + " ; ".join(
                f"{s} {r[names[s]]['median']:.0f} min ({r[names[s]]['line']}{', est.' if r[names[s]].get('proj', 0) >= .5 else ''})"
                for s in sample if names.get(s) in r))
