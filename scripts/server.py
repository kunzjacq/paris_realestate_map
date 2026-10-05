"""Serveur local de l'application : fichiers statiques de web/ + API de construction à la demande.

    .venv/bin/python scripts/server.py [port]

API (JSON) :
  GET    /api/search?q=nogent           communes d'Île-de-France correspondant au texte
  GET    /api/at?lon=..&lat=..          commune sous un point
  GET    /api/bbox?w=..&s=..&e=..&n=..  communes intersectant une emprise
  POST   /api/build  {"codes": [...]}   met des communes en file de construction
  POST   /api/stats  {"codes", "mode", "networks", "walk", "air", "bp", "route", "fer"}
                                        surfaces de la zone retenue par commune (calcul côté serveur,
                                        pour ne pas charger dans le navigateur les communes hors écran)
  DELETE /api/commune/<code>            retire une commune
  GET    /api/freshness[?days=N]        âge des données en cache : sources et communes de plus de 6 mois
  POST   /api/refresh                   met en file la mise à jour des données de plus de 6 mois
  GET    /api/status                    état de la file (le front le sonde pendant les constructions)

Fond de carte : GET /tiles/<plan|ortho>/<z>/<x>/<y> sert les tuiles IGN, gardées dans data/raw/tiles/
(retéléchargées après 6 mois ; les plus anciennes sont supprimées au démarrage).
"""

import importlib
import json
import mimetypes
import os
import queue
import sys
import threading
import traceback
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pipeline  # noqa: E402

PIPELINE_FILE = Path(pipeline.__file__)

MAX_BBOX_COMMUNES = 12


class Jobs:
    """File de construction traitée par un seul thread (les étapes sont lourdes et partagent le cache)."""

    def __init__(self):
        self.q = queue.Queue()
        self.lock = threading.Lock()
        self.pending = []      # codes en attente, dans l'ordre
        self.current = None    # {"code", "nom", "step"}
        self.errors = {}       # code -> message
        self.version = 0       # incrémenté à chaque commune construite ou retirée
        self.pipeline_mtime = PIPELINE_FILE.stat().st_mtime
        threading.Thread(target=self._worker, daemon=True).start()

    def reload_pipeline_if_changed(self):
        """Recharge pipeline.py s'il a été modifié depuis le démarrage, puis remet en file
        les communes construites avec un ancien format de données."""
        mtime = PIPELINE_FILE.stat().st_mtime
        if mtime != self.pipeline_mtime:
            print("pipeline.py modifié : rechargement", flush=True)
            importlib.reload(pipeline)
            self.pipeline_mtime = mtime
            self.add(pipeline.outdated_communes())

    def add(self, codes):
        added = []
        with self.lock:
            for c in codes:
                if c in self.pending or (self.current and self.current["code"] == c):
                    continue
                self.pending.append(c)
                self.errors.pop(c, None)
                self.q.put(c)
                added.append(c)
        return added

    def status(self):
        with self.lock:
            return {"pending": list(self.pending), "current": dict(self.current) if self.current else None,
                    "errors": dict(self.errors), "version": self.version}

    def bump(self):
        with self.lock:
            self.version += 1

    def _worker(self):
        while True:
            code = self.q.get()
            try:
                self.reload_pipeline_if_changed()
            except Exception:
                traceback.print_exc()
            with self.lock:
                if code in self.pending:
                    self.pending.remove(code)
                self.current = {"code": code, "nom": code, "step": "démarrage"}
            def log(msg):
                print(msg, flush=True)
                with self.lock:
                    self.current["step"] = msg
            try:
                if code == REFRESH:
                    self.current["nom"] = "Mise à jour des données"
                    errors = pipeline.refresh_stale(log, on_commune=self.bump)
                    with self.lock:
                        self.errors.update(errors)
                else:
                    self.current["nom"] = pipeline.commune_geom(code).nom
                    pipeline.build_commune(code, log)
            except Exception as e:  # erreur rapportée au front, le serveur continue
                traceback.print_exc()
                with self.lock:
                    self.errors[code] = str(e)
            with self.lock:
                self.current = None
                self.version += 1


REFRESH = "__refresh__"  # tâche de mise à jour des données anciennes, dans la même file

JOBS = Jobs()


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        if "/api/status" not in self.path and not self.path.startswith("/tiles/"):
            super().log_message(fmt, *args)

    def end_headers(self):
        # le navigateur doit revérifier chaque fichier (code de l'application et données changent) ;
        # sans cela il peut garder une ancienne version d'app.js après une mise à jour
        if not self.path.startswith(("/api/", "/tiles/")):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.write_body(body)

    def write_body(self, body):
        """Envoie body ; une requête abandonnée par le navigateur (tuile sortie de la vue pendant un
        déplacement de la carte, page quittée) a fermé la connexion : rien à faire."""
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def send_gzip_if_available(self, path):
        """Sert path.gz (Content-Encoding: gzip) si le navigateur l'accepte et s'il est à jour."""
        if "gzip" not in self.headers.get("Accept-Encoding", ""):
            return False
        src = self.translate_path(path)
        gz = src + ".gz"
        if not (os.path.isfile(src) and os.path.isfile(gz)) or os.path.getmtime(gz) < os.path.getmtime(src):
            return False
        st = os.stat(src)
        last_modified = self.date_time_string(st.st_mtime)
        if self.headers.get("If-Modified-Since") == last_modified:
            self.send_response(304)
            self.end_headers()
            return True
        with open(gz, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(src)[0] or "application/octet-stream")
        self.send_header("Content-Encoding", "gzip")
        self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Last-Modified", last_modified)
        self.end_headers()
        self.write_body(body)
        return True

    def send_tile(self, path):
        """Tuile IGN du fond de carte (cache local, voir pipeline.basemap_tile)."""
        parts = path.split("/")[2:]  # /tiles/<nom>/<z>/<x>/<y>
        tile = None
        if len(parts) == 4 and all(v.isdigit() for v in parts[1:]):
            tile = pipeline.basemap_tile(parts[0], *map(int, parts[1:]))
        if tile is None:
            self.send_response(404)
            self.send_header("Cache-Control", "no-store")  # sans quoi le navigateur garderait l'erreur
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body, ctype = tile
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=604800")  # 7 jours ; le cache disque fait le reste
        self.end_headers()
        self.write_body(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path.startswith("/tiles/"):
            return self.send_tile(u.path)
        if not u.path.startswith("/api/"):
            if u.path.startswith("/data/") and self.send_gzip_if_available(u.path):
                return
            return super().do_GET()
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/api/status":
                return self._json(JOBS.status())
            if u.path == "/api/search":
                return self._json(pipeline.search_communes(q.get("q", "")))
            if u.path == "/api/at":
                return self._json(pipeline.communes_at(float(q["lon"]), float(q["lat"])))
            if u.path == "/api/freshness":
                return self._json(pipeline.freshness(int(q.get("days", pipeline.MAX_AGE_DAYS))))
            if u.path == "/api/bbox":
                hits = pipeline.communes_in_bbox(float(q["w"]), float(q["s"]), float(q["e"]), float(q["n"]))
                return self._json({"communes": hits[:MAX_BBOX_COMMUNES], "total": len(hits),
                                   "max": MAX_BBOX_COMMUNES})
        except Exception as e:
            traceback.print_exc()
            return self._json({"error": str(e)}, 500)
        self._json({"error": "inconnu"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        if path == "/api/stats":
            try:
                return self._json(pipeline.zone_stats([c for c in body.get("codes", []) if c.isalnum()], body))
            except Exception as e:
                traceback.print_exc()
                return self._json({"error": str(e)}, 500)
        if path == "/api/refresh":
            return self._json({"added": JOBS.add([REFRESH])})
        if path != "/api/build":
            return self._json({"error": "inconnu"}, 404)
        codes = body.get("codes", [])
        known = set(pipeline.communes_table().code)
        bad = [c for c in codes if c not in known]
        if bad:
            return self._json({"error": f"communes inconnues ou hors Île-de-France : {', '.join(bad)}"}, 400)
        self._json({"added": JOBS.add(codes)})

    def do_DELETE(self):
        p = urlparse(self.path).path
        if not p.startswith("/api/commune/"):
            return self._json({"error": "inconnu"}, 404)
        code = p.rsplit("/", 1)[1]
        if not code.isalnum():
            return self._json({"error": "code invalide"}, 400)
        pipeline.remove_commune(code)
        JOBS.bump()
        self._json({"removed": code})


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    pipeline.communes_table()  # charge (ou télécharge une fois) les contours des communes
    if not (pipeline.WEB_DATA / "index.json").exists():
        pipeline.update_index()
    n = pipeline.add_missing_air_ranges()
    if n:
        print(f"résumé des plages de pollution ajouté à {n} communes", flush=True)
    n = pipeline.add_missing_quartiers()
    if n:
        print(f"quartiers ajoutés à {n} communes", flush=True)
    n = pipeline.pack_missing()
    if n:
        print(f"paquet des couches créé pour {n} communes", flush=True)
    n = pipeline.compress_missing()
    if n:
        print(f"{n} fichiers de données compressés", flush=True)
    def purge():
        removed, kept, size = pipeline.purge_tiles()
        print(f"fond de carte : {kept} tuiles en cache ({size / 1e6:.0f} Mo)"
              + (f", {removed} de plus de {pipeline.TILE_MAX_AGE_DAYS} jours supprimées" if removed else ""), flush=True)
    threading.Thread(target=purge, daemon=True).start()  # en arrière-plan : le cache peut être gros
    outdated = pipeline.outdated_communes()
    if outdated:
        print(f"reconstruction des communes au format ancien : {', '.join(outdated)}", flush=True)
        JOBS.add(outdated)
    handler = partial(Handler, directory=str(pipeline.ROOT / "web"))
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
    print(f"immo_map : http://localhost:{port}/", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
