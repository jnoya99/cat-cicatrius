# Validació Sentinel vs oficial (2024)

Comparació a la malla de l’app (**0.001°**, vegeu `grid/app_grid.json`).

## Mètriques (nivell cel·la)

| Mètrica | Valor |
|---|---|
| IoU | 0.6114 |
| Precisió | 0.9400 |
| Recall | 0.6362 |
| F1 | 0.7588 |
| Cel·les oficials | 2045 |
| Cel·les Sentinel | 1384 |
| TP / FP / FN | 1301 / 83 / 744 |

## Àrees

| Font | ha (aprox.) |
|---|---|
| Oficial DARPA/ICGC | 1213.3 |
| Sentinel-2 dNBR | 657.1 |

> Nota: la validació només té sentit quan existeixen `scars/sentinel_2024.geojson`
> i `scars/official_2024.geojson`. El llindar dNBR per defecte és 0.12.
