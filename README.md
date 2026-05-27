# TORNEOS-DE-AJEDREZ V6

Agrega sobre V5:

## Importar fixture pendiente

Permite cargar un CSV con cruces todavía no resueltos para que el motor busque automáticamente las partidas en Chess.com.

Columnas requeridas:

torneo,ronda,fecha_inicio,fecha_fin,blancas_chesscom,negras_chesscom

Luego:
Torneos → Buscar resultados Chess.com

Validaciones:
- blancas exactas
- negras exactas
- modalidad
- ritmo
- rated/casual
- fecha dentro del rango de ronda
