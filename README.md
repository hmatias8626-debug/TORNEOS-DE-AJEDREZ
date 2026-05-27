# TORNEOS-DE-AJEDREZ V3

Versión con:
- Roles: superadmin, admin, player.
- Primer usuario creado = superadmin.
- Superadmin puede crear otros superadmins.
- Admin puede crear torneos, escanear resultados y cancelar torneos.
- Perfil de jugador con avatar de Chess.com.
- Sincronización de perfil desde Chess.com.
- Torneo suizo por rondas.
- Torneo fixture libre: cada jugador tiene X rivales y puede jugar en cualquier orden dentro del rango.
- Validación estricta de colores:
  - white.username debe coincidir con el jugador de blancas del fixture.
  - black.username debe coincidir con el jugador de negras del fixture.
- Validación de fechas por ronda/fixture.
- Resultado detectado queda bloqueado.
- ELO interno.

## Ejecutar

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Streamlit Community Cloud

Main file path:

```text
app.py
```