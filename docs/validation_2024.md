# Validació Sentinel vs oficial (2024)

Comparació a la malla de l’app (**0.001°**, vegeu `grid/app_grid.json`).

## Mètriques (nivell cel·la)

| Mètrica | Valor |
|---|---|
| IoU | 0.6113 |
| Precisió | 0.9976 |
| Recall | 0.6122 |
| F1 | 0.7588 |
| Cel·les oficials | 2045 |
| Cel·les Sentinel | 1255 |
| TP / FP / FN | 1252 / 3 / 793 |

## Àrees

| Font | ha (aprox.) |
|---|---|
| Oficial DARPA/ICGC | 1213.3 |
| Sentinel-2 dNBR | 655.5 |

> Nota: la validació només té sentit quan existeixen `scars/sentinel_2024.geojson`
> i `scars/official_2024.geojson`. El llindar dNBR per defecte és 0.12.
