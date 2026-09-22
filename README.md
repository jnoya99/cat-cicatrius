# cat-cicatrius

Cicatrius d’**incendis forestals a Catalunya** per al [Bolets Explorador](https://github.com/jnoya99).  
Parcel·les cremades alineades a la **malla de l’app** (WGS84, pas **0.001°**), publicades a `docs/` per raw / jsDelivr / GitHub Pages — mateix esperit que [`mc-dades-acumulades`](https://github.com/jnoya99/mc-dades-acumulades).

## Per què (pla estacional, NO FIRMS diari)

Els bolets de primavera (p. ex. múrgoles) reaccionen a cicatrius de l’estiu **amb mesos de retard**. No cal un cron diari de hotspots:

1. **Localitzar incendis** de tant en tant (taules Gencat + perímetres Bombers quan calgui).
2. **Geometria** (prioritat):
   - (a) SHP oficial DARPA/ICGC quan es publica;
   - (b) si no, Sentinel-2 pre/post **dNBR** via CDSE/openEO;
   - (c) EFFIS WFS opcional per a incendis grans;
   - evitar buffers minúsculs.
3. **Publicar 1–2 cops** abans de la temporada de múrgoles (p. ex. **1 nov** i **1 feb** Europe/Madrid).

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
| **Action estacional** | Cron `0 6 1 11,2 *` (06:00 UTC el **1 nov** i **1 feb**) + `workflow_dispatch` |
| `scripts/ingest_gencat.py` | Taules Socrata → `events/*.csv` |
| `scripts/fetch_official_shp.py` | Baixa `incendis{YY}.zip` de gencat.cat → GeoJSON a `scars/` |
| `scripts/fetch_effis.py` | WFS EFFIS bbox Catalunya (best-effort) |
| `scripts/map_scars_openeo.py` | Esquelet CDSE openEO dNBR (salta si no hi ha secrets) |
| `scripts/build_burned_cells.py` | Polígons → cel·les 0.001° → `docs/burned_cells.parquet` |
| `scripts/publish_docs.py` | Manifest + checksums a `docs/manifest.json` |
| **GitHub Pages / raw / jsDelivr** | Consumeix `docs/` |

> **Horari:** el cron de GitHub Actions és sempre UTC. `0 6 1 11,2 *` → ~07:00 Europe/Madrid (CET) el 1 de novembre i el 1 de febrer.

## Producte `docs/burned_cells.parquet`

Només cel·les cremades / intersectades (sparses):

| Columna | Tipus | Notes |
|---|---|---|
| `lon` / `lat` | float | Arrodonits a 0.001° |
| `dense_index` | int | `jj * NLON + ii` |
| `burn_year` | int | Any del foc |
| `year_minus_1` / `year_minus_2` | 0/1 | Relatius a `reference_year` del manifest |
| `frac_burned` | 0–1 | Fracció de la cel·la |
| `severity` | 0–3 o null | Opcional |
| `source` | string | `official` \| `sentinel` \| `effis` \| `bombers` |
| `confidence` | string | `low` \| `medium` \| `high` |
| `fire_id` | string \| null | Id amunt |

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
- CDSE: https://dataspace.copernicus.eu/ — secrets `CDSE_USER` / `CDSE_PASSWORD` (no inventar credencials)

## Avís per correu (GitHub)

Al final de cada `seasonal-build` (cron o manual), el workflow obre i tanca un **issue** etiquetat `avis-estacional` amb `@jnoya99`. Això fa que GitHub t’enviï un **correu** (notificació de participació), tant si ha anat bé com si ha fallat.

Comprova a GitHub → **Settings** → **Notifications**:
- correu activat per a **Participating**;
- i, opcional, a **Actions** les fallades de workflow.

---

## Secrets (opcional, pas Sentinel)

Al repositori GitHub → Settings → Secrets and variables → Actions:

- `CDSE_USER`
- `CDSE_PASSWORD`

Sense ells, el workflow **continua** amb Gencat + SHP oficial + EFFIS.

## Ús local

```bash
cd cat-cicatrius
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python scripts/ingest_gencat.py
python scripts/fetch_official_shp.py          # best-effort
python scripts/fetch_effis.py                 # best-effort
python scripts/map_scars_openeo.py            # SKIP sense CDSE_* 
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
