# immo_map

Carte interactive des zones situées à moins de N minutes à pied ou à vélo d'une gare RER ou Transilien, sous des seuils
de pollution de l'air et de bruit, pour n'importe quelle commune d'Île-de-France.

## Lancer l'application

```sh
./run.sh            # puis ouvrir http://localhost:8000/
```

`run.sh` démarre `scripts/server.py`. Au premier lancement, si le venv `.venv` ne fonctionne pas avec le
Python de la machine, il crée `.venv-local` et y installe `requirements.txt` (il faut `python3-venv`). Ce serveur sert l'application (`web/`) et construit les
communes à la demande. Pour ajouter une commune depuis l'application :
- la rechercher par nom ;
- cliquer sur la carte hors des communes chargées ;
- ou cliquer sur « Ajouter les communes visibles » (12 au maximum).

Une nouvelle commune prend de quelques secondes à quelques minutes (dalles OSM et cartes à télécharger), puis
s'affiche d'elle-même. Les téléchargements sont partagés entre communes et mis en cache dans `data/raw/`.

Servi par un simple serveur statique (`python3 -m http.server -d web`), l'application fonctionne
en lecture seule avec les communes déjà construites.

## Construire en ligne de commande

```sh
.venv-local/bin/python scripts/build_data.py 94068 94015    # codes INSEE (ou .venv/bin/python)
.venv-local/bin/python scripts/build_data.py                # reconstruit toutes les communes présentes
```

Paramètres en tête de `scripts/pipeline.py` : durées, rayon de recherche des gares, année Airparif,
taille de cellule…

## Organisation

- `scripts/pipeline.py` : téléchargement et préparation d'une commune ;
  écrit `web/data/communes/<code>/`, `web/data/index.json` et `web/data/global/`.
- `scripts/server.py` : serveur local, API et file de construction. Les fichiers de données sont aussi
  écrits compressés (`.gz`, ~5 fois plus petits) et servis ainsi aux navigateurs qui acceptent gzip.
- Chargement à la demande : au démarrage, le navigateur ne reçoit que le résumé et le contour de chaque
  commune ; les couches détaillées d'une commune sont chargées quand elle devient visible. Les surfaces
  du bloc « Zone retenue » sont calculées par le serveur (`/api/stats`) pour toutes les communes actives.
  La carte rouvre sur la dernière vue utilisée.
- `web/` : application (Leaflet, sans dépendance de build).

## Données

| Critère | Source | Détail |
|---|---|---|
| Temps à pied jusqu'à une gare | OpenStreetMap (Overpass, dalles en cache dans `data/raw/osm/`) + entrées de gares IDFM | plus court chemin sur le réseau piéton, 4,5 km/h, depuis chaque entrée ; temps réel par cellule (s) |
| Temps à vélo jusqu'à une gare | OpenStreetMap | plus court chemin vers la gare, sens uniques respectés sauf contresens cyclables, 15 km/h (6 km/h sur voies piétonnes), escaliers et voies interdites exclus |
| Gares | IDFM, `emplacement-des-gares-idf` | gares RER (A–E) et Transilien (H, J, K, L, N, P, R, U, V) à moins de 4,5 km de la commune ; temps de marche calculé par réseau, combiné dans l'application selon les réseaux cochés |
| NO₂, PM2.5, PM10 | Airparif, WCS 1.0 `namek.airparif.fr` | moyennes annuelles 2025 modélisées, 6,25 m |
| Bruit routier | Bruitparif, carte stratégique de bruit E4 consolidée (`CSB4_w4echConso_Route_A_Lden`, MapProxy `raster.bruitparif.fr`) | Lden en 8 classes (< 45, 45-50, …, ≥ 75 dB), toutes rues ; images WMS reconverties en classes par leur couleur |
| Bruit ferroviaire | Bruitparif, CSB E4 consolidée (`CSB4_w4echConso_Fer_A_Lden`), complétée par la DRIEAT (CSB E4 2022, SNCF et RATP) | Lden en 8 classes ; dans chaque cellule, la valeur la plus élevée des deux sources |
| Indice global (option) | Bruitparif/Airparif, cartographie air-bruit 2024 | 3 niveaux, route + fer + avion |
| Contours des communes | geo.api.gouv.fr | |

Toutes les couches sont rééchantillonnées sur une grille Web Mercator d'environ 10 m par commune.

## Limites

- Bruit routier et ferroviaire : Bruitparif ne publie ces cartes que sous forme d'images. Chaque pixel est reconverti dans
  la classe dont la couleur de légende est la plus proche (tuiles de niveau 16, ~2,4 m). Les pixels de
  couleur mélangée (bords de classes) sont ignorés, puis chaque cellule de 10 m prend la classe majoritaire.
  Hors agglomération, seuls les grands axes sont cartographiés ; la commune est alors signalée « ⚠ route ».
- Bruit ferroviaire : la DRIEAT ne publie pas de carte pour la Seine-et-Marne, les Yvelines, ni pour
  l'Essonne et le Val-d'Oise hors Métropole du Grand Paris ; la carte Bruitparif comble ces manques.
- Format des données : `DATA_FORMAT` dans `scripts/pipeline.py`. Au démarrage, et après une modification
  de `pipeline.py`, le serveur reconstruit les communes produites avec un format antérieur.
- Temps de trajet : calculés localement (scipy) sur le réseau OSM, à vitesse constante : ni feux, ni dénivelé,
  ni temps pour garer le vélo. Vitesses réglables en tête de `scripts/pipeline.py` (`WALK_SPEED_KMH`,
  `BIKE_SPEED_KMH`, `BIKE_SLOW_KMH`). Les seuils comparent le temps arrondi à la minute, comme l'affichage.
- En WCS 2.0, le GeoServer d'Airparif échoue sur certaines emprises. Le pipeline utilise donc
  WCS 1.0 et vérifie qu'il reçoit bien un GeoTIFF.

## Dépôt git et données

Le dépôt ne contient que le code. Les données sont hors dépôt (`.gitignore`) : les communes construites
(`web/data/`, ~150 Mo) et les téléchargements en cache (`data/raw/`, ~1,6 Go) se régénèrent avec les
scripts, mais certaines sources sont lentes ou parfois indisponibles (Overpass, Airparif). Pour les
conserver ou les transférer :

```sh
.venv/bin/python scripts/data_archive.py sauver                 # web/data + data/raw
.venv/bin/python scripts/data_archive.py sauver --sans-cache    # web/data seulement (suffit pour l'appli)
.venv/bin/python scripts/data_archive.py restaurer immo_map-donnees-AAAAMMJJ.tar.gz
```

Sans archive, un dépôt fraîchement cloné démarre vide : `./run.sh` puis ajouter les communes depuis
l'application (ou `scripts/build_data.py <codes INSEE>`).
