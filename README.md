# cat-cicatrius

Cicatrius d’**incendis forestals a Catalunya** per al [Bolets Explorador](https://github.com/jnoya99).  
Parcel·les cremades alineades a la **malla de l’app** (WGS84, pas **0.001°**), publicades a `docs/` per raw / jsDelivr / GitHub Pages — mateix esperit que [`mc-dades-acumulades`](https://github.com/jnoya99/mc-dades-acumulades).

## Per què (pla estacional, NO FIRMS diari)

Els bolets de primavera (p. ex. múrgoles) reaccionen a cicatrius de l’estiu **amb mesos de retard**. No cal un cron diari de hotspots:

1. **Localitzar incendis** de tant en tant (taules Gencat + perímetres Bombers quan calgui).
2. **Geometria** (prioritat **per any**):
   - (a) SHP oficial DARPA/ICGC quan `scars/official_{year}.geojson` existeix → cel·les d’aquell any **principalment oficials**;
   - (b) Sentinel-2 pre/post **dNBR** (region growing + màscara bosc/matoll) només omple anys/àrees **sense** cobertura oficial (no sobreescriu cel·les oficials);
   - (c) EFFIS WFS opcional quan no hi ha oficial de l’any;
   - evitar buffers minúsculs.
3. **Publicar un cop al mes** (dia **1**, Europe/Madrid) per refrescar oficial/EFFIS/producte; abans de la temporada de múrgoles això ja cobreix nov–feb.

## Malla de l’app (crítica)

L’UI diu “~100 m”, però les cel·les són **~0.001°** (~80–110 m). **No** useu ids UTM EPSG:25831 de 100 m.

Constants a [`grid/app_grid.json`](grid/app_grid.json):

| Constant | Valor |
|---|---|
| CRS | WGS84 lon/lat (EPSG:4326) |
| `STEP` | `0.001` ° |
| `MIN_LON` | `0.15` |
| `MIN_LAT` | `40.42` |
| `NLON` | `3271` |
| cel·les forestals (aprox.) | `1_338_311` (sparses) |

Lookup:

```text
ii = round((lon - MIN_LON) / STEP)
jj = round((lat - MIN_LAT) / STEP)
dense_index = jj * NLON + ii
```

## Què fa

| Peça | Descripció |
|---|---|
| **Action estacional** | Cron `17 6 1 * *` (06:17 UTC el **dia 1 de cada mes**) + `workflow_dispatch` |
| `scripts/ingest_gencat.py` | Taules Socrata → `events/*.csv` |
| `scripts/fetch_official_shp.py` | Baixa `incendis{YY}.zip` de gencat.cat → GeoJSON a `scars/` |
| `scripts/fetch_effis.py` | WFS EFFIS bbox Catalunya (best-effort) |
| `scripts/map_scars_openeo.py` | CDSE openEO Sentinel-2 dNBR per AOI: region growing (core 0.35 / grow 0.22) + filtre bosc/matoll ESA WorldCover 2021 (classes 10+20); salta si no hi ha secrets |
| `scripts/validate_against_official.py` | IoU/P/R/F1 Sentinel vs oficial (malla 0.001°) |
| `scripts/build_burned_cells.py` | Polígons → cel·les 0.001° → `docs/burned_cells.parquet` (oficial primer per any; sentinel només omple forats) |
| `scripts/publish_docs.py` | Manifest + checksums a `docs/manifest.json` |
| **GitHub Pages / raw / jsDelivr** | Consumeix `docs/` |

> **Horari:** el cron de GitHub Actions és sempre UTC. `17 6 1 * *` → 06:17 UTC el dia **1 de cada mes** (~07:17 CET / ~08:17 CEST Europe/Madrid).
>
> Les execucions mensuals refresquen oficial/EFFIS/producte. El pas Sentinel (CDSE openEO) només corre si hi ha secrets `CDSE_CLIENT_*` i **pot consumir crèdits cada mes** — acceptat per tenir actualitzacions mensuals.

## Producte `docs/burned_cells.parquet`

Només cel·les cremades / intersectades (sparses):

| Columna | Tipus | Notes |
|---|---|---|
| `lon` / `lat` | float | Arrodonits a 0.001° |
| `dense_index` | int | `jj * NLON + ii` |
| `burn_year` | int | Any del foc |
| `year_minus_1` / `year_minus_2` | 0/1 | Relatius a `reference_year` del manifest |
| `frac_burned` | 0–1 | Fracció de la cel·la |
| `severity` | `baixa`/`moderada`/`alta` o null | Severitat dNBR (només Sentinel; oficial/EFFIS → null) |
| `dnbr` | float o null | Mitjana dNBR dels píxels cremats (si hi ha raster) |
| `source` | string | `official` \| `sentinel` \| `effis` \| `bombers` |
| `confidence` | string | `low` \| `medium` \| `high` |
| `fire_id` | string \| null | Id d’incendi; cicatrius Sentinel desconnectades → `{aoi}_{part}` (p. ex. `561276_18`) |


### Severitat (`severity`) — classes dNBR

Per a cicatrius **Sentinel** (openEO dNBR), la severitat es deriva de la mitjana de dNBR dels píxels cremats (classes aprox. USGS/MTBS):

| Classe | dNBR (aprox.) | Etiqueta |
|---|---|---|
| baixa | 0,10 – 0,27 | `baixa` |
| moderada | 0,27 – 0,44 | `moderada` |
| alta | ≥ 0,44 | `alta` |

Si el polígon Sentinel existeix però el GeoTIFF dNBR ja no és al disc (execucions anteriors), es fa un **proxy local** `moderada` (`severity_method=threshold_proxy`): el region-grow exigeix un nucli ≥ 0,35, dins la banda moderada. Les cel·les **oficials / EFFIS** no tenen dNBR → `severity` i `dnbr` queden `null`.

### URLs de consum (un cop activat Pages / després del primer push)

```text
https://jnoya99.github.io/cat-cicatrius/burned_cells.parquet
https://raw.githubusercontent.com/jnoya99/cat-cicatrius/main/docs/burned_cells.parquet
https://cdn.jsdelivr.net/gh/jnoya99/cat-cicatrius@main/docs/burned_cells.parquet
https://jnoya99.github.io/cat-cicatrius/manifest.json
```

## Fonts (gratuïtes)

- Gencat any en curs: https://analisi.transparenciacatalunya.cat/resource/9r29-e8ha.json  
- Gencat històric 2011–2024: https://analisi.transparenciacatalunya.cat/resource/bks7-dkfd.json  
- Cartografia oficial: https://agricultura.gencat.cat/ca/serveis/cartografia-sig/bases-cartografiques/boscos/incendis-forestals/  
  - SHP directe (patró): `http://www.gencat.cat/agricultura/sig/bases/incendis{YY}.zip` (p. ex. `incendis24.zip`)  
  - Si el zip de l’any no existeix encara: deixeu el fitxer a mà a `data/official/`  
- EFFIS WFS: https://maps.effis.emergency.copernicus.eu/effis (capes `modis.ba.poly.{year}`, bbox ~0.15,40.5,3.35,42.9; WFS 1.0.0)  
- CDSE: https://dataspace.copernicus.eu/ — autenticació openEO amb client OAuth (no inventar credencials)

## Avís per correu (GitHub)

Al final de cada `seasonal-build` (cron o manual), el workflow obre i tanca un **issue** etiquetat `avis-estacional` amb `@jnoya99`. Això fa que GitHub t’enviï un **correu** (notificació de participació), tant si ha anat bé com si ha fallat.

Comprova a GitHub → **Settings** → **Notifications**:
- correu activat per a **Participating**;
- i, opcional, a **Actions** les fallades de workflow.

---

## Secrets (opcional, pas Sentinel)

### Opció B — client OAuth CDSE per a openEO (recomanada)

L’automatització openEO necessita un client OAuth; `CDSE_USER` / `CDSE_PASSWORD`
per si sols **no són suficients**. Al [Sentinel Hub Dashboard](https://shapps.dataspace.copernicus.eu/dashboard/),
ves a **User Settings → OAuth clients**, crea un client i copia el seu client ID i secret.
Després, al repositori GitHub → **Settings → Secrets and variables → Actions**, crea:

- `CDSE_CLIENT_ID`
- `CDSE_CLIENT_SECRET`

Sense aquests dos secrets, el workflow **continua** amb Gencat + SHP oficial + EFFIS.

## Ús local

```bash
cd cat-cicatrius
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python scripts/ingest_gencat.py
python scripts/fetch_official_shp.py          # best-effort
python scripts/fetch_effis.py                 # best-effort
python scripts/map_scars_openeo.py            # SKIP sense CDSE_CLIENT_*
python scripts/build_burned_cells.py
python scripts/publish_docs.py
```

Prova ràpida de la malla:

```bash
python scripts/grid_utils.py
```

## Estructura

```text
grid/app_grid.json      # constants de la malla
events/                 # CSV Gencat
scars/                  # GeoJSON de perímetres (oficial / EFFIS / dNBR)
docs/                   # producte publicat (parquet + geojson + manifest)
data/official/          # drop manual de SHP/ZIP (gitignored els binaris grans)
scripts/                # pipeline
.github/workflows/seasonal_build.yml
```

## Atribucions

- Generalitat de Catalunya / Transparència Catalunya (taules)
- DARPA + ICGC (perímetres oficials)
- EFFIS / Copernicus EMS
- CDSE / Sentinel-2 (quan s’activi)
- NASA FIRMS (només si s’afegeix algun dia; **no** forma part del pla actual)

Codi: MIT (vegeu `LICENSE`). Les dades respecten la llicència de cada proveïdor.

## Sentinel dNBR (region growing + bosc/matoll)

`map_scars_openeo.py` ja no usa un sol llindar binari:

| Paràmetre | Defecte | Notes |
|---|---|---|
| `--threshold-core` | `0.35` | Nucli cremat segur |
| `--threshold-grow` | `0.22` | Expansió només contiguous al nucli |
| `--threshold` | — | Alias legacy: posa core i grow al mateix valor |
| `--min-ha` | `1.0` | Descarta fragments polígonitzats petits |
| `--min-seed-ha` | `1.0` | Només processa llavors amb àrea de la geometria (abans del buffer) **> 1 ha** |
| `--max-aois` | `0` | `0` = sense límit (totes les llavors que passen `min-seed-ha`); >0 limita a les N més grans |
| `--post-windows` | `2` | Finestres post-foc (+7–30 / +30–60 d), sempre retallades a avui (UTC) |
| Buffers cerca | 800 m oficial / 1200 m EFFIS | Clip a llavor ⊕ 300 m |
| `--forest-mask` | on | ESA WorldCover 2021 via CDSE openEO (`ESA_WORLDCOVER_10M_2021_V2`); classes **10** (arbres) i **20** (matoll); exclou conreu, urbà, aigua, nues |
| `--morph-core` | on | Opening 1px només al nucli (abans de créixer) |

El producte `build_burned_cells.py` priorita `official_{year}`: si existeix, les cel·les d’aquell any són oficials (sentinel només omple cel·les sense oficial; EFFIS es salta per a anys amb oficial). Així, amb `reference_year=2026`, `year_minus_2` (2024) surt gairebé tot `source=official` si hi ha `official_2024.geojson`.

## Proves 2024 / 2025 / 2026

**Política operativa (defecte):** processar **totes** les llavors d’incendi amb àrea de llavor **> 1 ha** (`--min-seed-ha 1`, `--max-aois 0`), amb **2 finestres post-foc** (`--post-windows 2`) retallades a avui (UTC). No hi ha límit per defecte de 8 AOIs.

Workflow manual **`prova-sentinel`** (GitHub → Actions → *prova-sentinel* → *Run workflow*):

| Input | Valors | Notes |
|---|---|---|
| `year` | `2024` / `2025` / `2026` | 2024 valida contra `official_2024`; llavors només del mateix any |
| `max_aois` | defecte `0` | `0` = sense límit; poseu p. ex. `8` només per proves ràpides de crèdits |

Passos del workflow: checkout → Python 3.12 → `pip install -r requirements.txt` (inclou `openeo` + `rasterio`) → fetch official/EFFIS → `map_scars_openeo.py` → (2024) `validate_against_official.py` → `build_burned_cells.py` → `publish_docs.py` → commit `docs/` + `scars/sentinel_*.geojson` → avís per correu (issue).

Secrets necessaris (Settings → Secrets → Actions): **`CDSE_CLIENT_ID`** i **`CDSE_CLIENT_SECRET`** (client OAuth creat al [Sentinel Hub Dashboard](https://shapps.dataspace.copernicus.eu/dashboard/), a **User Settings → OAuth clients**). `CDSE_USER` / `CDSE_PASSWORD` no són suficients per a l’automatització openEO.

Artefactes típics:
- `scars/sentinel_{year}.geojson` (+ per AOI `scars/sentinel_{year}_{id}.geojson`)
- `docs/validation_2024.json` / `docs/validation_2024.md` (només any 2024)

CLI local (sense secrets → skip net exit 0):

```bash
python scripts/map_scars_openeo.py --year 2024 --max-aois 0 \
  --min-seed-ha 1 --post-windows 2 \
  --threshold-core 0.35 --threshold-grow 0.22 --forest-mask --dry-run
python scripts/validate_against_official.py --year 2024
```

