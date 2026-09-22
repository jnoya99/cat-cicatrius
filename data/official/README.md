# data/official/

Drop manual de perímetres DARPA/ICGC si la baixada automàtica falla.

Patró remot descobert:

```text
http://www.gencat.cat/agricultura/sig/bases/incendis{YY}.zip
```

Exemples: `incendis24.zip`, `incendis23.zip`, …  
`incendis25.zip` pot tornar 404 fins que el Departament el publiqui.

Els `.zip` / `.shp` grans són a `.gitignore`. Després de deixar un zip aquí:

```bash
python scripts/fetch_official_shp.py --years 2024
# o descomprimiu a mà i:
# python scripts/fetch_official_shp.py --probe-only
```

El script intenta convertir SHP → `scars/official_{year}.geojson`.
