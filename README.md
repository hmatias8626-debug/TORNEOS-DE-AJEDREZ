# TORNEOS-DE-AJEDREZ V6.1

Corrige lectura de CSV exportados desde Excel.

Ahora el importador intenta leer:
- UTF-8
- UTF-8-SIG
- Latin-1
- CP1252

y detecta separador:
- coma
- punto y coma
- tabulación

## Importar fixture pendiente

Columnas requeridas:

torneo,ronda,fecha_inicio,fecha_fin,blancas_chesscom,negras_chesscom
