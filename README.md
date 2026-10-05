# immo_map

Carte interactive des zones situées à moins de N minutes à pied ou à vélo d'une gare RER, Transilien ou d'une
station de métro, sous des seuils de pollution de l'air et de bruit, pour n'importe quelle commune
d'Île-de-France.

## Lancer l'application

```sh
./run.sh            # puis ouvrir http://localhost:8000/   (autre port : ./run.sh 8080)
```

`run.sh` choisit un environnement Python, installe les dépendances manquantes puis démarre le serveur
(voir « Serveur » ci-dessous). Pour ajouter une commune depuis l'application :
- la rechercher par nom ;
- cliquer sur la carte hors des communes chargées ;
- ou cliquer sur « Ajouter les communes visibles » (communes visibles à au moins 30 %, 12 au maximum).

Une nouvelle commune prend de quelques secondes (données déjà en cache) à quelques minutes (dalles OSM et
cartes à télécharger), puis s'affiche d'elle-même.

## Utilisation de l'application

### Menu (à gauche)

Le menu s'élargit ou se rétrécit en glissant son bord droit (double-clic : largeur par défaut) ; le bouton
« « » le masque entièrement, « ☰ Menu » sur la carte le rouvre. Ses réglages sont conservés d'une visite à
l'autre (navigateur), comme la dernière vue de la carte.

| Rubrique | Contenu |
|---|---|
| Communes | liste repliable (clic sur le titre) : case pour inclure ou non la commune, centrage, retrait ; recherche, « Ajouter les communes visibles », suivi des constructions. « ⚠ route » / « ⚠ fer » : bruit connu sur moins de 90 % de la commune |
| Trajet jusqu'à une gare | filtre activable, à pied ou à vélo, réseaux RER / Transilien / Métro, seuil de 3 à 20 min (pas de 1 min) |
| Pollution de l'air | un curseur par polluant (NO₂, PM2.5, PM10, moyennes annuelles), repères OMS et UE 2030 |
| Bruit des transports | Lden routier et ferroviaire maximal (de < 75 à < 45 dB), indice global Bruitparif (3 niveaux) |
| Affichage | couche de contexte (temps de trajet, polluants, bruits), zone retenue, contour de la zone atteignable |
| Données | âge des données et bouton de mise à jour de celles de plus de 6 mois (voir « Serveur ») |
| Zone retenue | repliable : surface retenue totale et par commune, part de la surface respectant chaque critère seul |

### Carte

- **Zone retenue** (tous les critères respectés) : claire, teintée de vert, cernée d'un trait ; le reste des
  communes téléchargées est assombri. Avec une couche de contexte, la zone n'est pas teintée et son trait est noir.
- **Communes non téléchargées** : voile gris hachuré. Un clic dessus propose « Ajouter cette commune ».
- **Contour pointillé bleu** : zone atteignable dans le temps choisi (temps de trajet seul).
- **Gares** : seules les 3 plus proches de la souris sont affichées, avec leurs accès et leur nom (rose : RER,
  bleu : Transilien, jaune : métro) ; la gare retenue pour le point survolé est agrandie.
- **Encadré en haut à droite** (point sous la souris, pointeur en croix) : gare la plus rapide à atteindre,
  temps à pied et à vélo (arrondis à la minute), bruit routier, ferroviaire et indice global, NO₂ / PM2.5 /
  PM10 (pastille verte sous la recommandation OMS, jaune jusqu'à la valeur limite UE 2030, rouge au-delà),
  et la liste des raisons d'exclusion quand le point est hors de la zone retenue.
- **Noms des communes** téléchargées, dessinés au-dessus des zones à partir du zoom 12.
- Les contours sont lissés et simplifiés selon le zoom (moins de détail en vue large).

## Serveur

`scripts/server.py` (lancé par `run.sh`) sert l'application et les données, construit les communes à la
demande et calcule les surfaces de la zone retenue. Il écoute uniquement sur `127.0.0.1`.

### Démarrage

1. **Environnement Python** (`run.sh`) : `.venv` s'il fonctionne avec le Python de la machine et contient
   toutes les dépendances ; sinon `.venv-local`, créé au besoin (il faut `python3-venv`), où les paquets
   manquants de `requirements.txt` sont installés.
2. **Contours des communes d'Île-de-France** : chargés depuis `data/raw/idf_communes.gpkg` (téléchargés une
   fois sur geo.api.gouv.fr) ; ils servent à la recherche et aux requêtes « commune sous un point ».
3. **Index** : `web/data/index.json` est créé s'il manque.
4. **Mises à niveau des données existantes** : résumé des plages de pollution ajouté aux communes qui ne
   l'ont pas, versions compressées `.gz` créées ou rafraîchies, et communes produites avec un format de
   données antérieur (`DATA_FORMAT` dans `pipeline.py`) mises en file de reconstruction.

### Fichiers servis

- `web/` : l'application (`index.html`, `app.js`, `style.css`, Leaflet dans `web/vendor/`).
- `web/data/` : les données. Les couches de chaque commune sont aussi regroupées dans un seul fichier
  (`layers.pack`, une requête par commune au lieu d'une vingtaine). Chaque fichier existe aussi en version
  compressée (`.gz`, ~5 fois plus petite), envoyée avec `Content-Encoding: gzip` aux navigateurs qui
  l'acceptent. Le serveur crée au démarrage les paquets et versions compressées manquants.
- Tous les fichiers servis portent `Cache-Control: no-cache` : le navigateur revérifie chaque fichier (requête
  conditionnelle, réponse 304 s'il n'a pas changé) et ne garde donc jamais une ancienne version d'`app.js`
  ou des données après une mise à jour.

### API (JSON)

| Requête | Rôle |
|---|---|
| `GET /api/search?q=nogent` | communes d'Île-de-France dont le nom ou le code INSEE correspond |
| `GET /api/at?lon=…&lat=…` | commune sous un point (clic sur la carte hors des communes chargées) |
| `GET /api/bbox?w=…&s=…&e=…&n=…` | communes visibles à au moins 30 % dans une emprise (12 au maximum) |
| `POST /api/build` `{"codes": [...]}` | met des communes (codes INSEE) en file de construction |
| `DELETE /api/commune/<code>` | retire une commune (supprime ses données) |
| `GET /api/status` | état de la file : commune en cours et étape, communes en attente, erreurs, version |
| `POST /api/stats` | surfaces de la zone retenue par commune (voir ci-dessous) |
| `GET /api/freshness` | âge des données en cache : par source, fichiers de plus de 6 mois, communes à mettre à jour |
| `POST /api/refresh` | met en file la mise à jour des données de plus de 6 mois (voir ci-dessous) |

### File de construction

Les constructions sont traitées une par une par un seul fil d'exécution (elles partagent les caches de
téléchargement). L'application interroge `/api/status` toutes les 1,5 s pendant une construction (5 s sinon) ;
quand le numéro de version change, elle recharge l'index et affiche les communes nouvelles ou reconstruites.

Avant chaque construction, le serveur vérifie si `scripts/pipeline.py` a été modifié depuis son
chargement : si oui, il le recharge et remet en file les communes au format de données antérieur. Une
modification du pipeline ne demande donc pas de redémarrer le serveur ; une modification de `server.py`, si.

### Âge des données et mise à jour

Chaque fichier téléchargé (`data/raw/`) est daté de son téléchargement. `GET /api/freshness` les regroupe par
source : gares et accès IDFM, contours des communes, indice air-bruit, bruit DRIEAT (communs à toutes les
communes), réseau OSM, pollution Airparif, bruit routier et ferroviaire Bruitparif (propres à chaque commune).
Une commune est à mettre à jour si l'une de ses données a plus de 6 mois (`MAX_AGE_DAYS` dans `pipeline.py`),
si une donnée commune a plus de 6 mois, ou si une mise à jour précédente a été interrompue avant elle.

Dans l'application, la section « Données » (bas du menu) affiche le bouton « Mettre à jour les données de plus
de 6 mois », grisé avec une explication quand rien n'est à mettre à jour, et le détail par source.
La mise à jour (`POST /api/refresh`) passe par la file de construction :

1. chaque fichier de plus de 6 mois est retéléchargé dans un fichier temporaire, qui ne remplace l'ancien
   qu'une fois complet ; si le téléchargement échoue, l'ancien fichier est conservé ;
2. les conversions dérivées (carte air-bruit, couches DRIEAT) sont refaites de la même façon ;
3. les communes concernées sont reconstruites une à une à côté de leur version actuelle, qui reste servie
   jusqu'au remplacement. La liste des communes restant à faire est gardée dans `data/raw/refresh_state.json`
   pour reprendre une mise à jour interrompue.

Aucune donnée n'est donc effacée avant que sa nouvelle version soit disponible.

### Surfaces calculées par le serveur

Le navigateur ne charge les données détaillées d'une commune que lorsqu'elle est visible à l'écran (avec une
marge de 15 %), quatre communes à la fois au plus, les plus proches du centre de la vue d'abord ; un
chargement qui échoue est réessayé quelques secondes plus tard. Au démarrage, il ne reçoit que le résumé
(`meta.json`) et le contour de chaque commune, et la carte rouvre sur la dernière vue utilisée. Le bloc « Zone retenue » additionne pourtant toutes les communes
actives : il est calculé par le serveur, avec les mêmes règles que l'application.

```json
POST /api/stats
{"codes": ["94068", "94015"], "mode": "walk", "networks": ["rer", "transilien"],
 "walk": 10, "air": {"no2": 20}, "bp": 3, "route": 60, "fer": 999}
```

`walk` : minutes (ou `null` sans filtre de temps) ; `air` : seuils en µg/m³ des polluants filtrés ;
`bp` : indice Bruitparif maximal (1 à 3) ; `route`, `fer` : Lden strictement inférieur (999 = pas de
filtre). La réponse donne, par commune, la surface totale, la surface retenue et la surface respectant
chaque critère pris seul (m²). Les couches nécessaires sont gardées en mémoire après le premier appel.

Servie par un simple serveur statique (`python3 -m http.server -d web`), l'application fonctionne en lecture
seule : pas d'ajout de communes, et les surfaces ne portent que sur les communes chargées à l'écran.

## Construire en ligne de commande

```sh
.venv-local/bin/python scripts/build_data.py 94068 94015    # codes INSEE (ou .venv/bin/python)
.venv-local/bin/python scripts/build_data.py                # reconstruit toutes les communes présentes
```

Paramètres en tête de `scripts/pipeline.py` : durées, rayon de recherche des gares, année Airparif,
taille de cellule…

## Organisation

- `run.sh` : environnement Python et lancement du serveur.
- `scripts/pipeline.py` : téléchargement et préparation d'une commune ; écrit `web/data/communes/<code>/`,
  `web/data/index.json` et `web/data/global/` (gares et accès de toutes les communes).
- `scripts/server.py` : serveur local (voir « Serveur »).
- `scripts/build_data.py` : construction en ligne de commande.
- `scripts/data_archive.py` : sauvegarde et restauration des données hors dépôt.
- `web/` : application (Leaflet, sans étape de build).

## Données

| Critère | Source | Détail |
|---|---|---|
| Temps à pied jusqu'à une gare | OpenStreetMap (Overpass, dalles en cache dans `data/raw/osm/`) + entrées de gares et bouches de métro IDFM | plus court chemin sur le réseau piéton, 4,5 km/h, depuis chaque entrée ; temps réel par cellule (s) |
| Temps à vélo jusqu'à une gare | OpenStreetMap | plus court chemin vers la gare, sens uniques respectés sauf contresens cyclables, 15 km/h (6 km/h sur voies piétonnes), escaliers et voies interdites exclus |
| Gares et stations | IDFM, `emplacement-des-gares-idf` | gares RER (A–E), Transilien (H, J, K, L, N, P, R, U, V) et stations de métro (1 à 14, 3bis, 7bis) à moins de 4,5 km de la commune ; temps calculé par réseau, combiné dans l'application selon les réseaux cochés |
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
- Overpass (téléchargement du réseau OSM) limite le nombre de requêtes par adresse IP : avant chaque requête,
  le pipeline consulte la page d'état du serveur et attend le créneau libre ; les miroirs ne servent qu'en
  dernier recours. Une dalle de Paris peut peser 20 Mo. Une fois les dalles en cache, une commune voisine
  ne demande plus rien à Overpass.
- En WCS 2.0, le GeoServer d'Airparif échoue sur certaines emprises. Le pipeline utilise donc
  WCS 1.0 et vérifie qu'il reçoit bien un GeoTIFF.

## Dépôt git et données

Dépôt : https://github.com/kunzjacq/paris_realestate_map. Il ne contient que le code. Les données sont hors dépôt (`.gitignore`) :

| Dossier | Contenu | Taille |
|---|---|---|
| `web/data/` | communes construites, index, gares (couches en `.bin` et regroupées dans `layers.pack`, plus les `.gz`) | ~15 Mo par commune (2,1 Go pour 142 communes) |
| `data/raw/` | téléchargements en cache : dalles OSM, rasters Airparif, cartes de bruit, gares IDFM… | ~3 Go pour 142 communes |

Tout se régénère avec les scripts, mais certaines sources sont lentes ou parfois indisponibles (Overpass,
Airparif). `scripts/data_archive.py` sauvegarde ces données dans une archive `.tar.gz` et les restaure ;
il n'utilise que la bibliothèque standard (Python ≥ 3.12) et ne demande pas d'environnement virtuel.

```sh
python3 scripts/data_archive.py save                     # web/data + data/raw (~500 Mo pour 142 communes)
python3 scripts/data_archive.py save --no-cache          # web/data seulement (suffit pour l'appli)
python3 scripts/data_archive.py save mes-donnees.tar.gz  # nom d'archive choisi
python3 scripts/data_archive.py restore immo_map-data-AAAAMMJJ.tar.gz
```

**`save`** crée par défaut `immo_map-data-AAAAMMJJ.tar.gz` à la racine du projet (ignoré par git). Sont omis
les fichiers recalculables : les versions compressées `.gz` et les paquets `layers.pack` de `web/data/`
(recréés au démarrage du serveur) et `data/raw/airbruit2024.gpkg` (conversion de `airbruit2024.zip`, refaite à la demande). Avec `--no-cache`,
seul `web/data/` est archivé : l'application fonctionne, mais ajouter ou reconstruire une commune
retéléchargera ses données.

**`restore`** extrait l'archive à la racine du projet ; il refuse une archive contenant des chemins hors de
`web/data/` et `data/raw/`. Les fichiers existants de même nom sont remplacés, les autres conservés.
Ensuite, `./run.sh` recrée les versions compressées et reconstruit les éventuelles communes d'un format
antérieur.

Pour repartir d'un clone du dépôt :

```sh
git clone git@github.com:kunzjacq/paris_realestate_map.git immo_map && cd immo_map
python3 scripts/data_archive.py restore /chemin/immo_map-data-AAAAMMJJ.tar.gz
./run.sh                                   # crée l'environnement Python au premier lancement
```

Sans archive, le clone démarre sans commune : ajoutez-les depuis l'application, ou avec
`scripts/build_data.py <codes INSEE>`.
