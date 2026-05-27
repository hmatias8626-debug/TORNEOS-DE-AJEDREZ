# TORNEOS-DE-AJEDREZ V4

Agrega:
- Importar torneos anteriores desde CSV.
- Crear jugadores históricos automáticamente.
- Vincular cuenta nueva con historial si coincide el usuario Chess.com.
- Perfil más completo:
  - ranking
  - torneos jugados
  - partidas jugadas
  - G/E/P
  - rendimiento
  - historial
  - historial ELO
- Roles:
  - superadmin
  - admin
  - player
- Fixture libre y suizo.
- Detección con colores estrictos y fechas/rondas.

## CSV histórico

Columnas requeridas:

```text
torneo,ronda,fecha,blancas_chesscom,negras_chesscom,resultado,link_chesscom
```

Resultados válidos:

```text
1-0
0-1
1/2-1/2
0.5-0.5
```