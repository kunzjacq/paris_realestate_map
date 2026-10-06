"""Durée en transports en commun depuis chaque gare jusqu'à quelques gares destinations.

    .venv-local/bin/python scripts/transit.py            # calcule (ou relit) les tables et en affiche un extrait

Horaires théoriques IDFM (GTFS, « offre-horaires-tc-gtfs-idfm ») : RER, Transilien, métro et tramway d'un
mardi (SERVICE_WEEKDAY) de la période couverte par le fichier. Pour chaque destination, profil complet des
départs de chaque arrêt (Connection Scan Algorithm en profil, une seule passe sur les horaires) : pour tout
départ, heure d'arrivée au plus tôt à destination, attente et correspondances (temps de marche du GTFS)
comprises. Résultat par gare (zone de correspondance IDFM, comme les gares de l'application) et par période
(PERIODS) : durée médiane, 10e et 90e centiles sur les départs de chaque minute de la période, et ligne
empruntée au départ le plus souvent. Le pipeline en tire les temps porte à porte de chaque commune.
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipeline import RAW, cached, http_get  # noqa: E402

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
HORIZON = 3 * 3600      # horaires lus jusqu'à la fin de la dernière période + HORIZON
MIN_CHANGE_S = 60       # correspondance sur le même quai (descente puis montée)
TABLE_FORMAT = 2        # à incrémenter quand le calcul des tables change (nom du fichier en cache)
INF = 10 ** 9


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
    start = min(p[1] for p in PERIODS.values())
    end = max(p[2] for p in PERIODS.values()) + HORIZON
    f = RAW / f"gtfs_rail_{day}_{start}_{end}.npz"
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
        stops = read("stops.txt", usecols=["stop_id", "stop_name", "parent_station"])
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
    out = {
        "c_trip": c.trip.astype("category").cat.codes.to_numpy(), "c_from": c["from"].to_numpy(),
        "c_to": c["to"].to_numpy(), "c_dep": c.dep.to_numpy(), "c_arr": c.arr.to_numpy(),
        "c_line": c.trip.map(line_of).fillna("").to_numpy(),
        "t_from": t.from_stop_id.to_numpy(), "t_to": t.to_stop_id.to_numpy(),
        "t_time": t.min_transfer_time.fillna("0").astype(int).to_numpy(),
        "s_id": s.stop_id.to_numpy(), "s_name": s.stop_name.to_numpy(),
        "s_parent": s.parent_station.fillna("").str.replace("IDFM:", "").to_numpy(),
    }
    np.savez(f, **out)
    return out


class Profile:
    """Profil d'un arrêt : départs non dominés (départ, arrivée à destination, ligne prise), départ croissant."""
    __slots__ = ("deps", "arrs", "lines")

    def __init__(self):
        self.deps, self.arrs, self.lines = [], [], []

    def best(self, t):
        """(arrivée au plus tôt, ligne) en partant de l'arrêt à l'heure t ou après."""
        i = bisect.bisect_left(self.deps, t)
        return (self.arrs[i], self.lines[i]) if i < len(self.deps) else (INF, "")

    def add(self, dep, arr, line):
        i = bisect.bisect_left(self.deps, dep)
        if i < len(self.deps) and self.arrs[i] <= arr:
            return False  # un départ plus tardif arrive aussi tôt
        j = i
        while j > 0 and self.arrs[j - 1] >= arr:  # départs plus tôt qui n'arrivent pas avant : dominés
            j -= 1
        self.deps[j:i], self.arrs[j:i], self.lines[j:i] = [dep], [arr], [line]
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
    deps, arrs, frm, to, trips, lines = (tt[k] for k in ("c_dep", "c_arr", "c_from", "c_to", "c_trip", "c_line"))
    for k in np.argsort(-deps, kind="stable"):
        dep, arr, a, b, trip = int(deps[k]), int(arrs[k]), frm[k], to[k], trips[k]
        tau = arr + to_target[b] if b in to_target else INF                 # descente à destination
        tau = min(tau, trip_best.get(trip, INF))                              # rester dans la rame
        p = prof.get(b)
        if p is not None:
            tau = min(tau, p.best(arr + MIN_CHANGE_S)[0])                     # correspondance
        if tau >= INF:
            continue
        trip_best[trip] = tau
        if prof.setdefault(a, Profile()).add(dep, tau, lines[k]):
            for y, w in walk_in.get(a, []):                                   # venir à pied d'un quai voisin
                prof.setdefault(y, Profile()).add(dep - w, tau, lines[k])
    return prof


def station_durations(tt, prof, start, end):
    """Par zone de correspondance : durées (min) pour un départ à chaque minute de start à end (médiane,
    10e et 90e centiles) et ligne prise au départ le plus souvent."""
    quays = {}
    for sid, parent in zip(tt["s_id"], tt["s_parent"]):
        if parent and sid in prof:
            quays.setdefault(parent, []).append(prof[sid])
    out = {}
    for zdc, ps in quays.items():
        d, used = [], collections.Counter()
        for t in range(start, end + 1, 60):
            arr, line = min(p.best(t) for p in ps)
            if arr < INF:
                d.append((arr - t) / 60)
                used[line] += 1
        if d:
            out[zdc] = {"median": round(float(np.median(d)), 1), "p10": round(float(np.percentile(d, 10)), 1),
                        "p90": round(float(np.percentile(d, 90)), 1), "line": used.most_common(1)[0][0]}
    return out


def transit_tables(log=print):
    """{"day", "destinations": {clé: nom}, "periods": {clé: libellé}, "tables": {destination: {période:
    {zdc: {median, p10, p90, line}}}}}, en cache dans data/raw/transit_<jour>.json."""
    day = service_day()
    f = RAW / f"transit_{day}_v{TABLE_FORMAT}.json"
    if f.exists():
        return json.loads(f.read_text())
    t0 = time.time()
    tt = rail_timetable(day)
    log(f"horaires du {day} : {len(tt['c_dep'])} connexions ferrées ({time.time() - t0:.0f} s)")
    tables = {}
    for key, (nom, zdcs) in DESTINATIONS.items():
        targets = {s for s, parent in zip(tt["s_id"], tt["s_parent"]) if parent in zdcs}
        prof = profiles_to(tt, targets)
        tables[key] = {pk: station_durations(tt, prof, start, end) for pk, (_, start, end) in PERIODS.items()}
        for durations in tables[key].values():  # depuis la destination elle-même : on y est déjà
            durations.update({z: {"median": 0.0, "p10": 0.0, "p90": 0.0, "line": ""} for z in zdcs})
        log(f"temps en transports jusqu'à {nom} calculés")
    out = {"day": day, "destinations": {k: v[0] for k, v in DESTINATIONS.items()},
           "periods": {k: v[0] for k, v in PERIODS.items()}, "tables": tables}
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
    sample = ["La Défense", "Vincennes", "Saint-Maur - Créteil", "Massy - Palaiseau", "Versailles Chantiers"]
    for key, nom in t["destinations"].items():
        for pk in PERIODS:
            r = t["tables"][key][pk]
            print(f"{nom:20} {pk:8} " + " ; ".join(f"{s} {r[names[s]]['median']:.0f} min ({r[names[s]]['line']})"
                                                for s in sample if names.get(s) in r))
