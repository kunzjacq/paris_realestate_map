# immo_map

Carte interactive des zones situées à moins de N minutes à pied ou à vélo d'une gare RER, Transilien ou d'une
station de métro, sous des seuils de pollution de l'air et de bruit, pour n'importe quelle commune
d'Île-de-France.

## Lancer l'application

```sh
./run.sh            # puis ouvrir http://localhost:8000/   (autre port : ./run.sh 8080)
./run.sh --jobs 4   # communes construites en parallèle (défaut : un quart des cœurs, 8 au plus)
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
| Aller à | commune chargée (complétion, « st » vaut « saint »), puis quartier dans la liste déroulante : la carte se centre sur la commune dès qu'elle est choisie, puis sur le quartier |
| Trajet jusqu'à une gare | filtre activable, à pied ou à vélo, réseaux RER / Transilien / Métro, seuil de 3 à 20 min (pas de 1 min), propre à chaque mode : changer de mode reprend le seuil de ce mode. « Tramways » (repliable) : une case par ligne en service (T1 à T14), aucune cochée par défaut, boutons « Toutes » / « Aucune ». « Lignes en projet » : « Grand Paris Express (lignes 15 à 18) » et « Prolongements de tramway » (des lignes cochées), avec un curseur commun sur les dates d'ouverture estimées (fin 2026 à fin 2031, dernier scénario du réseau prévu) qui ne retient que les arrêts ouverts d'ici la date choisie |
| Trajet jusqu'à une destination | destination (Châtelet-Les Halles, La Défense, Gare de Lyon, Saint-Lazare, Montparnasse, Gare du Nord ; « Aucune » : pas de filtre), période (pointe du matin 7 h 30 – 9 h 30, milieu de journée 11 h – 15 h, en semaine) et durée porte à porte maximale (15 à 90 min) : trajet jusqu'à une gare dans le mode choisi (à pied ou à vélo), puis transports en commun ; avec des lignes en projet cochées, réseau prévu fin 2027 ou fin 2031 (le plus récent avant la date du curseur), durées estimées signalées au survol |
| Pollution de l'air | case « Filtrer par la pollution » (décochée : critère ignoré, réglages conservés) ; un curseur par polluant (NO₂, PM2.5, PM10, moyennes annuelles), repères OMS et UE 2030 |
| Bruit des transports | case « Filtrer par le bruit » (décochée : critère ignoré, réglages conservés) ; Lden routier et ferroviaire maximal (de < 75 à < 45 dB), indice global Bruitparif (3 niveaux) |
| Affichage | couche de contexte (temps de trajet, temps jusqu'à la destination, polluants, bruits), zone retenue, contour de la zone atteignable, quartiers |
| Communes | liste repliable (clic sur le titre) : case pour inclure ou non la commune, centrage, retrait ; recherche, « Ajouter les communes visibles », suivi des constructions. « ⚠ route » / « ⚠ fer » : bruit connu sur moins de 90 % de la commune |
| Données | âge des données et bouton de mise à jour de celles de plus de 6 mois (voir « Serveur ») |
| Zone retenue | repliable : surface retenue totale et par commune, part de la surface respectant chaque critère seul |

### Carte

- **Zone retenue** (tous les critères respectés) : claire, teintée de vert, cernée d'un trait ; le reste des
  communes téléchargées est assombri. Avec une couche de contexte, la zone n'est pas teintée et son trait est noir.
- **Communes non téléchargées** : voile gris hachuré. Un clic dessus propose « Ajouter cette commune ».
- **Contour pointillé bleu** : zone atteignable dans le temps choisi (temps de trajet seul).
- **Quartiers** (option « Quartiers » d'« Affichage ») : limites en pointillés gris, de la même épaisseur que
  celles des communes, à partir du zoom 13 ; noms en italique à partir du zoom 15 ; le quartier sous la souris
  est éclairci et cerné d'un trait plein ; l'encadré de survol indique la commune et le quartier du point.
- **Gares** : seules les 3 plus proches de la souris sont affichées, avec leurs accès et leur nom (rose : RER,
  bleu : Transilien, jaune : métro, vert : Grand Paris Express, violet : tramway ; année d'ouverture des lignes
  en projet) ; la
  gare retenue pour le point survolé est agrandie. Les gares au même endroit (correspondance, arrêt de tram
  accolé, gare en projet à moins de 300 m d'une gare existante) partagent une étiquette, une ligne par gare.
- **Encadré en bas à droite** (point sous la souris, pointeur en croix) : gare la plus rapide à atteindre,
  temps à pied et à vélo (arrondis à la minute), bruit routier, ferroviaire et indice global, NO₂ / PM2.5 /
  PM10 (pastille verte sous la recommandation OMS, jaune jusqu'à la valeur limite UE 2030, rouge au-delà),
  durée porte à porte jusqu'à la destination choisie avec la gare de départ et la ligne prise (« via
  Saint-Maur-des-Fossés - Créteil (RER A, 23 min) »), et la liste des raisons d'exclusion quand le point est
  hors de la zone retenue.
- **Noms des communes** téléchargées, dessinés au-dessus des zones à partir du zoom 13.
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
   l'ont pas ; quartiers (`quartiers.geojson`, `quartiers_limites.geojson`) calculés pour les communes qui
   n'en ont pas ou dont le calcul est antérieur (`QUARTIERS_FORMAT` dans `pipeline.py`), sans reconstruire
   les communes ; versions compressées `.gz` créées ou rafraîchies ; communes dont un groupe de couches a un
   format antérieur (`FORMATS` dans `pipeline.py`, voir « File de construction ») mises en file de
   reconstruction.
5. **Cache du fond de carte** : tuiles de plus de 6 mois supprimées (en arrière-plan).

### Fichiers servis

- `web/` : l'application (`index.html`, `app.js`, `style.css`, Leaflet dans `web/vendor/`).
- `web/data/` : les données. Les couches de base de chaque commune (RER, Transilien, métro, air, bruit) sont
  aussi regroupées dans un seul fichier (`layers.pack`, une requête par commune au lieu d'une vingtaine). Les
  couches des tramways, des lignes en projet (72 pour Paris) et des destinations (48 par commune) restent à
  part (`<couche>.bin`) : l'application ne charge que celles des réseaux cochés et de la destination choisie. Chaque fichier existe aussi en version
  compressée (`.gz`, ~5 fois plus petite), envoyée avec `Content-Encoding: gzip` aux navigateurs qui
  l'acceptent. Le serveur crée au démarrage les paquets et versions compressées manquants.
- `/tiles/<plan|ortho>/<z>/<x>/<y>` : tuiles IGN du fond de carte (Plan IGN, photo aérienne), gardées
  dans `data/raw/tiles/` au fil de la consultation : une zone déjà vue s'affiche hors ligne. Une tuile
  absente ou de plus de 6 mois (`TILE_MAX_AGE_DAYS` dans `pipeline.py`) est (re)demandée à l'IGN, avec
  quelques essais en cas d'erreur ; si l'IGN ne répond pas, l'ancienne version est servie. Seules ces deux
  couches sont relayées (pas de relais vers d'autres adresses) ; le fond OpenStreetMap reste chargé
  directement (ses règles d'usage interdisent ce stockage). Servie par un simple serveur statique,
  l'application prend les tuiles IGN en ligne.
- Les autres fichiers servis portent `Cache-Control: no-cache` : le navigateur revérifie chaque fichier (requête
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

Les communes sont construites en parallèle (`--jobs`, défaut `PARALLEL_BUILDS` : un quart des cœurs, 8 au
plus), chacune dans un processus neuf qui lit la version actuelle de `pipeline.py` ; chaque processus utilise
sa part des cœurs pour ses recherches spatiales. Les téléchargements de tous les processus sont limités à 2 à
la fois (`MAX_PARALLEL_DOWNLOADS` : quotas d'Overpass, serveurs Bruitparif et Airparif) ; un fichier
téléchargé entre-temps par un autre processus n'est pas retéléchargé. `index.json` est mis à jour par le
serveur, regroupé après les constructions terminées. Un processus mort brutalement (mémoire…) fait échouer
sa commune ; les processus sont recréés pour les suivantes. Ordres de grandeur (32 cœurs) : 8 communes
recalculées entièrement en 37 s ; Paris seule en 74 s. L'application interroge `/api/status` toutes les 1,5 s pendant une construction (5 s sinon) ;
quand le numéro de version change, elle recharge l'index et affiche les communes nouvelles ou reconstruites.

Les couches d'une commune forment cinq groupes, chacun avec son format (`FORMATS` dans `pipeline.py`, à
incrémenter quand le calcul du groupe change, avec `DATA_FORMAT`) : `grille` (contour de la commune),
`transport` (gares et temps jusqu'à la gare la plus proche de chaque réseau : réseau OSM, gares IDFM et en
projet), `destinations` (temps porte à porte : horaires GTFS ; refait avec `transport`, ses couches désignant
les gares par leur rang, mais pas l'inverse), `air` (Airparif) et `bruit` (Bruitparif, DRIEAT). Une
reconstruction ne recalcule que les groupes périmés et reprend les autres couches de la version actuelle
(fichiers et versions compressées) ; tout est recalculé si la grille change (contour de la commune
modifié). Un changement des transports ne refait donc ni la pollution ni le bruit (Vincennes : 10 s).

Avant chaque construction, le serveur vérifie si `scripts/pipeline.py` a été modifié depuis son
chargement : si oui, il le recharge et remet en file les communes dont un groupe est périmé. Une
modification du pipeline ne demande donc pas de redémarrer le serveur ; une modification de `server.py`, si.
Exception : les quartiers d'un calcul antérieur (`QUARTIERS_FORMAT`) ne sont recalculés qu'au démarrage.

### Âge des données et mise à jour

Chaque fichier téléchargé (`data/raw/`) est daté de son téléchargement. `GET /api/freshness` les regroupe par
source : gares et accès IDFM, horaires des transports (GTFS), gares du Grand Paris Express en projet, contours des communes, indice air-bruit, bruit DRIEAT (communs à toutes les
communes), réseau OSM, pollution Airparif, bruit routier et ferroviaire Bruitparif, quartiers Linternaute,
contours IRIS de l'IGN (propres à chaque commune). Les tuiles du fond de carte ont leur propre durée de vie
(voir « Fichiers servis »).
Une commune est à mettre à jour si l'une de ses données a plus de 6 mois (`MAX_AGE_DAYS` dans `pipeline.py`),
si une donnée commune a plus de 6 mois, ou si une mise à jour précédente a été interrompue avant elle.

Dans l'application, la section « Données » (bas du menu) affiche le bouton « Mettre à jour les données de plus
de 6 mois », grisé avec une explication quand rien n'est à mettre à jour, et le détail par source.
La mise à jour (`POST /api/refresh`) passe par la file de construction :

1. chaque fichier de plus de 6 mois est retéléchargé dans un fichier temporaire, qui ne remplace l'ancien
   qu'une fois complet ; si le téléchargement échoue, l'ancien fichier est conservé ;
2. les conversions dérivées (carte air-bruit, couches DRIEAT) sont refaites de la même façon ;
3. les communes concernées sont reconstruites une à une, en ne recalculant que les groupes de couches des
   sources périmées (réseau OSM ou gares : transports et destinations ; horaires : destinations ; Airparif :
   air ; Bruitparif ou DRIEAT : bruit ; quartiers ou IRIS : quartiers seulement ; contours des communes :
   tout), à côté de leur version actuelle, qui reste servie jusqu'au remplacement. La liste des communes restant à faire (avec leurs groupes) est
   gardée dans `data/raw/refresh_state.json` pour reprendre une mise à jour interrompue.

Aucune donnée n'est donc effacée avant que sa nouvelle version soit disponible.

### Surfaces calculées par le serveur

Le navigateur ne charge les données détaillées d'une commune que lorsqu'elle est visible à l'écran (avec une
marge de 15 %), quatre communes à la fois au plus, les plus proches du centre de la vue d'abord ; un
chargement qui échoue est réessayé quelques secondes plus tard. Au démarrage, il ne reçoit que le résumé
(`meta.json`) et le contour de chaque commune, et la carte rouvre sur la dernière vue utilisée. Le bloc « Zone
retenue » additionne pourtant toutes les communes actives : il est calculé par le serveur, avec les mêmes
règles que l'application.

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
.venv-local/bin/python scripts/build_data.py                # communes présentes
.venv-local/bin/python scripts/build_data.py --all 94068    # tout recalculer
.venv-local/bin/python scripts/build_data.py --jobs 4       # communes construites en parallèle (1 : dans ce processus)
```

Une commune déjà construite n'est recalculée que pour ses groupes de couches périmés (voir « File de
construction ») ; une commune à jour est laissée telle quelle, sauf avec `--all`.

Paramètres en tête de `scripts/pipeline.py` : durées, rayon de recherche des gares, année Airparif,
taille de cellule…

## Organisation

- `run.sh` : environnement Python et lancement du serveur.
- `scripts/pipeline.py` : téléchargement et préparation d'une commune ; écrit `web/data/communes/<code>/`,
  `web/data/index.json` et `web/data/global/` (gares et accès de toutes les communes).
- `scripts/server.py` : serveur local (voir « Serveur »).
- `scripts/build_data.py` : construction en ligne de commande.
- `scripts/transit.py` : durées en transports en commun de chaque gare aux destinations (horaires GTFS) ;
  lancé seul, calcule les tables et en affiche un extrait.
- `scripts/data_archive.py` : sauvegarde et restauration des données hors dépôt.
- `web/` : application (Leaflet, sans étape de build).

## Données

| Critère | Source | Détail |
|---|---|---|
| Temps à pied jusqu'à une gare | OpenStreetMap (Overpass, dalles en cache dans `data/raw/osm/`) + entrées de gares et bouches de métro IDFM | plus court chemin sur le réseau piéton, 4,5 km/h, depuis chaque entrée ; temps réel par cellule (s) |
| Temps à vélo jusqu'à une gare | OpenStreetMap | plus court chemin vers la gare, sens uniques respectés sauf contresens cyclables, 15 km/h (6 km/h sur voies piétonnes), escaliers et voies interdites exclus |
| Tramways (option) | IDFM, `emplacement-des-gares-idf` (modes `TRAMWAY` et `TRAM`) | arrêts des lignes T1 à T14 à moins de 4,5 km de la commune ; un jeu de couches de temps par ligne (`walk_tram3a`…), combiné dans l'application selon les lignes cochées |
| Temps jusqu'à une destination (option) | IDFM, horaires théoriques GTFS `offre-horaires-tc-gtfs-idfm` (cache `data/raw/idfm_gtfs.zip`, 147 Mo) | RER, Transilien, métro, tramway et TER d'un mardi de la période couverte par le fichier (`transit.py`, `SERVICE_WEEKDAY`). Pour chaque destination (`DESTINATIONS` : zones de correspondance IDFM d'arrivée), profil de tous les départs de chaque arrêt en une passe (Connection Scan Algorithm), attente et correspondances (temps de marche du GTFS) comprises ; par gare et par période (`PERIODS`), durée médiane des départs de chaque minute et ligne prise au départ. Puis, par commune, temps porte à porte en chaque point = min sur les gares de (trajet jusqu'à la gare + durée en transports), en minutes (`walk_dest_<destination>_<période>`…) avec la gare de départ. Réseau prévu (scénarios fin 2027 et fin 2031, `SCENARIOS`) : horaires estimés des lignes en projet ouvertes d'ici là (`project_timetable`), gares ordonnées le long des tracés IDFM (`projets_lignes_idf`), sections d'une même ligne raccordées ; métro : vitesses calées sur les temps de parcours annoncés (15 Sud : Pont de Sèvres – Noisy-Champs en 37 min ; 16 : Saint-Denis Pleyel – Noisy-Champs en ~26 min ; 17 : Saint-Denis Pleyel – Le Mesnil-Amelot en un peu plus de 25 min ; 18 : Orly – Versailles Chantiers en un peu plus de 33 min), un passage toutes les 2 min (15) ou 3 min (16, 17, 18) à la pointe, hors pointe selon le rapport mesuré sur la ligne 14 ; tramway : vitesse et intervalles de la ligne existante ; correspondances : marche à 1 m/s + 4 min (gare du Grand Paris Express) ou 1 min (tram). Couches `walk_dest_<destination>_<période>_<scénario>` ; part des départs empruntant une ligne en projet par gare |
| Lignes en projet (option) | IDFM, `projets_arrets_idf` et `projets_lignes_idf` (cache `data/raw/idfm_projets_*.geojson`) | gares des lignes 15 à 18 du Grand Paris Express et arrêts des prolongements de tramway (T1, T7, T8, T11, T13), avec la date de mise en service estimée de leur tronçon (opération et phase) ; une gare du Grand Paris Express desservie par plusieurs lignes ouvre avec la première ; pas d'accès connus : temps calculés depuis le point de l'arrêt. Un jeu de couches de temps par date d'ouverture ayant un arrêt à portée de la commune (`walk_gpeAAAAMMJJ`, `walk_tram1_AAAAMMJJ`…), combiné dans l'application selon la date choisie |
| Gares et stations | IDFM, `emplacement-des-gares-idf` | gares RER (A–E), Transilien (H, J, K, L, N, P, R, U, V) et stations de métro (1 à 14, 3bis, 7bis) à moins de 4,5 km de la commune ; temps calculé par réseau, combiné dans l'application selon les réseaux cochés |
| NO₂, PM2.5, PM10 | Airparif, WCS 1.0 `namek.airparif.fr` | moyennes annuelles 2025 modélisées, 6,25 m |
| Bruit routier | Bruitparif, carte stratégique de bruit E4 consolidée (`CSB4_w4echConso_Route_A_Lden`, MapProxy `raster.bruitparif.fr`) | Lden en 8 classes (< 45, 45-50, …, ≥ 75 dB), toutes rues ; images WMS reconverties en classes par leur couleur |
| Bruit ferroviaire | Bruitparif, CSB E4 consolidée (`CSB4_w4echConso_Fer_A_Lden`), complétée par la DRIEAT (CSB E4 2022, SNCF et RATP) | Lden en 8 classes ; dans chaque cellule, la valeur la plus élevée des deux sources |
| Indice global (option) | Bruitparif/Airparif, cartographie air-bruit 2024 | 3 niveaux, route + fer + avion |
| Contours des communes | geo.api.gouv.fr | |
| Quartiers | Noms et regroupement : Linternaute, carte « Liste des quartiers » des pages ville (données Yanport, endpoint `/od/map`, cache `data/raw/quartiers/`). Contours : IRIS de l'IGN (Géoplateforme, WFS `STATISTICALUNITS.IRIS:contours_iris`, cache `data/raw/iris/`) | chaque IRIS va au quartier Linternaute qui en contient la plus grande part ; découpés par le contour de la commune ; `quartiers.geojson` par commune, et `quartiers_limites.geojson` (chaque limite entre quartiers une seule fois, hors limite communale, pour des pointillés nets) ; noms corrigés au besoin dans `QUARTIER_RENAMES` (`pipeline.py`) ; à Paris, code postal de l'arrondissement ajouté au nom (« Père Lachaise-Réunion (75020) »), d'après l'IRIS qui couvre la plus grande partie du quartier |

Toutes les couches sont rééchantillonnées sur une grille Web Mercator d'environ 10 m par commune.

## Limites

- Bruit routier et ferroviaire : Bruitparif ne publie ces cartes que sous forme d'images. Chaque pixel est
  reconverti dans la classe dont la couleur de légende est la plus proche (tuiles de niveau 16, ~2,4 m). Les
  pixels de couleur mélangée (bords de classes) sont ignorés, puis chaque cellule de 10 m prend la classe
  majoritaire. Hors agglomération, seuls les grands axes sont cartographiés ; la commune est alors
  signalée « ⚠ route ».
- Bruit ferroviaire : la DRIEAT ne publie pas de carte pour la Seine-et-Marne, les Yvelines, ni pour
  l'Essonne et le Val-d'Oise hors Métropole du Grand Paris ; la carte Bruitparif comble ces manques.
- Format des données : `FORMATS` (un format par groupe de couches) dans `scripts/pipeline.py`. Au démarrage,
  et après une modification de `pipeline.py`, le serveur reconstruit les communes dont un groupe a un format
  antérieur, en ne recalculant que ce groupe.
- Temps de trajet : calculés localement (scipy) sur le réseau OSM, à vitesse constante : ni feux, ni dénivelé,
  ni temps pour garer le vélo. Vitesses réglables en tête de `scripts/pipeline.py` (`WALK_SPEED_KMH`,
  `BIKE_SPEED_KMH`, `BIKE_SLOW_KMH`). Les seuils comparent le temps arrondi à la minute, comme l'affichage.
- Overpass (téléchargement du réseau OSM) limite le nombre de requêtes par adresse IP. Le pipeline essaie en
  alternance les deux machines d'overpass-api.de (lambert, puis gall via `lz4.overpass-api.de`), qui ont
  chacune leur quota : avant chaque requête, il consulte leur page d'état et attend le créneau libre. Les
  miroirs (`OVERPASS_MIRRORS`) ne servent qu'en dernier recours, et seulement si leur page d'état répond en
  moins de 10 s. Une réponse tronquée (délai ou mémoire dépassés côté serveur, signalés par `remark` malgré
  un HTTP 200) ou sans aucune voie n'est jamais mise en cache. Une dalle de Paris peut peser 34 Mo. Une fois
  les dalles en cache, une commune voisine ne demande plus rien à Overpass.
- Fond de carte : le serveur de tuiles de l'IGN renvoie parfois une erreur 404 pour une tuile qui existe,
  que le navigateur garderait en cache 21 jours (`Cache-Control: max-age=1814400`). Le serveur local
  réessaie et ne transmet jamais d'erreur mise en cache (`no-store`) ; l'application redemande en plus
  une tuile en échec sans le cache (jusqu'à 3 essais, après 1, 3 puis 9 s). Le cache des tuiles n'a pas
  de taille maximale : ~70 Ko par tuile, soit ~1 Go pour les communes chargées jusqu'au zoom 16, bien plus
  aux zooms supérieurs (~60 Go au zoom 19 si tout était consulté).
- Lignes en projet : les dates sont les estimations publiées par IDFM, le plus souvent à l'année (« fin
  2027 ») ; elles changent au fil des chantiers et sont retéléchargées avec les autres données de plus de 6
  mois. Les gares en correspondance avec une gare existante apparaissent deux fois (gare actuelle et gare en
  projet). Les lignes existantes prolongées depuis (14, 11, 4, RER E) sont déjà dans les gares en service
  d'IDFM. Les tronçons sans date chez IDFM (T4 vers Montfermeil, une partie du T1 Ouest) sont ignorés ; le
  T11 phase 2 (2038) n'est encore qu'à l'étude. Le curseur s'arrête au dernier scénario du réseau prévu (fin 2031) :
  les tronçons ouverts après (T1 vers Colombes en 2032, T11 phase 2) ne sont pas proposés.
- Tramways : la vitesse du tram n'intervient pas (seul compte le trajet jusqu'à l'arrêt) ; un arrêt de tram
  compte comme une gare.
- Temps jusqu'à une destination : horaires théoriques d'un seul jour (mardi), sans retards ni travaux ; durée
  médiane des départs de la période (l'encadré de survol donne la gare et la ligne prise au départ). Toutes
  les gares en service comptent comme gare de départ, quels que soient les réseaux cochés, et tout le réseau
  ferré sert au trajet ; bus exclus. Les horaires sont retéléchargés avec les autres données de plus de 6
  mois (nouveau jour de référence). Réseau prévu : durées estimées (hypothèses ci-dessus, ±15–20 % sur les
  lignes en projet), sans effet des nouvelles lignes sur l'exploitation des lignes existantes ; un scénario
  comprend toutes les lignes en projet ouvertes à sa date (Grand Paris Express et tramway), quelles que
  soient les cases cochées. Les couches de destinations pèsent ~3 octets par cellule et par triplet
  (destination, période, mode), et par scénario : ~7,8 Go bruts pour 190 communes.
- En WCS 2.0, le GeoServer d'Airparif échoue sur certaines emprises. Le pipeline utilise donc
  WCS 1.0 et vérifie qu'il reçoit bien un GeoTIFF.
- Quartiers : endpoint interne et non documenté de Linternaute, qui peut changer ; licence de réutilisation
  non précisée. Ses quartiers sont des IRIS ou des regroupements d'IRIS aux contours très simplifiés (une
  vingtaine de sommets : bords communs déformés, languettes prises au voisin, écarts jusqu'à ~80 m avec la
  limite communale) ; seuls leurs noms et leur regroupement sont donc gardés, les contours venant des IRIS de
  l'IGN. Un quartier qui ne correspond pas à ses IRIS (surface commune / surface réunie < 0,6 : bandes de
  Seine à Paris, un quartier de Franconville) garde son contour Linternaute, privé des quartiers voisins ; si
  c'est le cas de la plupart des quartiers d'une commune, elle garde ceux de Linternaute (`quartiers_source`
  dans `meta.json`). Quelques quartiers gardent des bandes étroites (20-30 m) le long d'une rivière : ce sont
  des quais rattachés par l'INSEE à un autre IRIS que les maisons qui les bordent (Quai de l'Artois à La
  Prairie de Nogent, Quai Gallieni à Fourchette-Polangis), conservés tels quels. Certaines petites communes
  n'ont pas de découpage ; à Le Chesnay-Rocquencourt et Saint-Denis, seuls ~60 % et ~78 % de la commune sont
  couverts. Un échec de téléchargement n'empêche pas la construction : les quartiers manquent et le serveur
  réessaie à son démarrage suivant. Après une modification de leur calcul (`QUARTIERS_FORMAT` dans
  `pipeline.py`), les quartiers sont recalculés au démarrage du serveur, sans reconstruire les communes.

## Dépôt git et données

Dépôt : https://github.com/kunzjacq/paris_realestate_map. Il ne contient que le code. Les données sont hors
dépôt (`.gitignore`) :

| Dossier | Contenu | Taille |
|---|---|---|
| `web/data/` | communes construites, index, gares (couches en `.bin` et regroupées dans `layers.pack`, plus les `.gz`) | ~15 Mo par commune (2,6 Go pour 177 communes) |
| `data/raw/` | téléchargements en cache : dalles OSM, rasters Airparif, cartes de bruit, gares IDFM, quartiers, IRIS… | ~3,6 Go pour 177 communes |
| `data/raw/tiles/` | tuiles du fond de carte, au fil de la consultation | selon les zones vues (voir « Limites ») |

Tout se régénère avec les scripts, mais certaines sources sont lentes ou parfois indisponibles (Overpass,
Airparif). `scripts/data_archive.py` sauvegarde ces données dans une archive `.tar.gz` ou `.tar.xz` et les
restaure ; il n'utilise que la bibliothèque standard (Python ≥ 3.12) et ne demande pas d'environnement
virtuel.

```sh
python3 scripts/data_archive.py save                     # web/data + data/raw (gzip, ~580 Mo pour 177 communes)
python3 scripts/data_archive.py save --xz                # idem en xz (~445 Mo, ~30 s avec la commande xz)
python3 scripts/data_archive.py save --no-cache          # web/data seulement (suffit pour l'appli)
python3 scripts/data_archive.py save mes-donnees.tar.xz  # nom d'archive choisi (.xz : compression xz)
python3 scripts/data_archive.py restore immo_map-data-AAAAMMJJ.tar.xz
```

**`save`** crée par défaut `immo_map-data-AAAAMMJJ.tar.gz` à la racine du projet (ignoré par git). Avec `--xz`
(ou un nom finissant par `.xz`), l'archive est compressée en xz : ~25 % plus petite, créée en une trentaine de
secondes si la commande `xz` est installée (tous les cœurs), sinon en ~7 min par le module `lzma` de Python
(un seul cœur). Sont omis les fichiers recalculables : les versions compressées `.gz` et les paquets
`layers.pack` de `web/data/` (recréés au démarrage du serveur), `data/raw/airbruit2024.gpkg` (conversion de
`airbruit2024.zip`, refaite à la demande), les tuiles du fond de carte (`data/raw/tiles/`, retéléchargées à
la demande) et les fichiers tirés des horaires (`data/raw/gtfs_rail_*.npz`, `data/raw/transit_*.json`). Avec `--no-cache`, seul `web/data/` est archivé : l'application fonctionne, mais ajouter ou
reconstruire une commune retéléchargera ses données.

**`restore`** reconnaît la compression (gzip, xz, bzip2 ou aucune) d'après le contenu du fichier, quel
que soit son nom, et extrait l'archive à la racine du projet ; il refuse une archive contenant des chemins
hors de `web/data/` et `data/raw/`. Les fichiers existants de même nom sont remplacés, les autres conservés.
Ensuite, `./run.sh` recrée les versions compressées et reconstruit les éventuelles communes d'un format
antérieur (seulement les groupes de couches périmés).

Pour repartir d'un clone du dépôt :

```sh
git clone git@github.com:kunzjacq/paris_realestate_map.git immo_map && cd immo_map
python3 scripts/data_archive.py restore /chemin/immo_map-data-AAAAMMJJ.tar.gz
./run.sh                                   # crée l'environnement Python au premier lancement
```

Sans archive, le clone démarre sans commune : ajoutez-les depuis l'application, ou avec
`scripts/build_data.py <codes INSEE>`.
