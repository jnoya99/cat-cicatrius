# Validació Sentinel vs oficial (2024)

Comparació a la malla de l’app (**0.001°**, vegeu `grid/app_grid.json`).

## Mètriques (nivell cel·la)

| Mètrica | Valor |
|---|---|
| IoU | 0.4227 |
| Precisió | 0.9931 |
| Recall | 0.4240 |
| F1 | 0.5942 |
| Cel·les oficials | 2045 |
| Cel·les Sentinel | 873 |
| TP / FP / FN | 867 / 6 / 1178 |

## Àrees

| Font | ha (aprox.) |
|---|---|
| Oficial DARPA/ICGC | 1213.3 |
| Sentinel-2 dNBR | 397.5 |

> Nota: la validació només té sentit quan existeixen `scars/sentinel_2024.geojson`
> i `scars/official_2024.geojson`. El llindar dNBR per defecte és 0.12.
