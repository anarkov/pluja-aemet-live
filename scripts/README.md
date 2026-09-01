# Spike de radar AEMET

Este script no publica ni modifica `radar/`. Descarga la composición nacional y
el radar regional oficial de Barcelona (`ba`), incluyendo la respuesta HATEOAS
y sus metadatos, dentro de `.spike/aemet-radar/` (ignorado por Git).

```powershell
$env:AEMET_API_KEY = '...'
python scripts/aemet_radar_spike.py
```

El informe resultante es `.spike/aemet-radar/SUMMARY.md`; el detalle completo,
incluyendo cabeceras, HATEOAS y los ficheros sin transformar, está en
`report.json`. Reconoce tanto imágenes como los TAR.GZ de GeoTIFF que AEMET
entrega actualmente, y extrae tamaño, EPSG/bbox, no-data, paleta, timestamps y
metadatos GDAL. Las URLs de AEMET son efímeras y no se deben reutilizar ni
publicar. El script no infiere intensidad meteorológica a partir de colores:
solo la declarará viable si el producto o sus metadatos documentan valores o
una paleta indexada con escala.
