# TORNEOS-DE-AJEDREZ V10.3 TEST

Cambios:
- Vista jugador más rápida: usa datos guardados y cache leve de tabla.
- El motor queda separado en Panel Admin → Motor Chess.com.
- El usuario no ve el proceso interno de búsqueda.
- Jugador puede solicitar revisión sobre una partida finalizada con link.
- Admin/Moderador ve Panel Admin → Revisiones.
- Decisión admin:
  - Mantener resultado / sin sanción.
  - Aplicar 0-0 y advertencia al jugador sancionado.
- Reglamento:
  - sanción = resultado oficial 0-0
  - nadie suma punto
  - solo el sancionado recibe advertencia
  - segunda advertencia = descalificado del torneo
- Crea tablas:
  - review_reports
  - player_warnings

Nota:
- La reversión exacta de ELO de partidas sancionadas queda para módulo posterior.
