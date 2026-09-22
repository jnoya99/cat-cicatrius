# Validació Sentinel vs oficial (2024)

Comparació a la malla de l’app (**0.001°**, vegeu `grid/app_grid.json`).

## Mètriques (nivell cel·la)

| Mètrica | Valor |
|---|---|
| IoU | 0.6218 |
| Precisió | 0.9847 |
| Recall | 0.6279 |
| F1 | 0.7668 |
| Cel·les oficials | 2045 |
| Cel·les Sentinel | 1304 |
| TP / FP / FN | 1284 / 20 / 761 |

## Àrees

| Font | ha (aprox.) |
|---|---|
| Oficial DARPA/ICGC | 1213.3 |
| Sentinel-2 dNBR | 695.4 |

> Nota: la validació només té sentit quan existeixen `scars/sentinel_2024.geojson`
> i `scars/official_2024.geojson`. El llindar dNBR per defecte és 0.12.
