# TORNEOS-DE-AJEDREZ V9

V9 agrega:
- Sistema de puntos configurable:
  - victoria
  - empate
  - derrota
  - BYE/libre
  - WO
- BYE/libre:
  - se importa usando negras_chesscom vacío, 0, BYE, LIBRE, free, descanso
  - suma puntos de tabla
  - no modifica ELO
  - no cuenta como partida real
  - no se busca en Chess.com
- Tabla con:
  - puntos
  - Buchholz
  - Buc1
  - BYE separado
  - PJ reales separado
- Orden de tabla:
  - puntos
  - Buchholz
  - Buc1
  - ELO
- Mantiene Supabase/PostgreSQL V8.
