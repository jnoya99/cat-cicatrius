# Validació Sentinel vs oficial (2024)

Comparació a la malla de l’app (**0.001°**, vegeu `grid/app_grid.json`).

## Mètriques (nivell cel·la)

| Mètrica | Valor |
|---|---|
| IoU | 0.0287 |
| Precisió | 0.0293 |
| Recall | 0.5834 |
| F1 | 0.0558 |
| Cel·les oficials | 2045 |
| Cel·les Sentinel | 40748 |
| TP / FP / FN | 1193 / 39555 / 852 |

## Àrees

| Font | ha (aprox.) |
|---|---|
| Oficial DARPA/ICGC | 1213.3 |
| Sentinel-2 dNBR | 12010.1 |

> Nota: la validació només té sentit quan existeixen `scars/sentinel_2024.geojson`
> i `scars/official_2024.geojson`. El llindar dNBR per defecte és 0.12.
