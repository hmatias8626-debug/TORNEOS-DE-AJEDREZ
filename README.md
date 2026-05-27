# TORNEOS-DE-AJEDREZ V2

Versión online de prueba.

## Qué agrega V2

- Cantidad de rondas suizas configurable.
- Fecha/hora inicio y fin por ronda.
- Ronda 1 manual, aleatoria o por ELO.
- Sistema suizo básico desde ronda 2.
- Detección automática por ventana de ronda.
- Resultado detectado queda bloqueado.
- Copas configurables:
  - Copa Oro
  - Copa Plata
  - Copa Bronce
  - más copas si se desea
- Playoffs iniciales por ranking.
- Historial y ELO interno.

## Ejecutar local

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Streamlit Community Cloud

Main file path:

```text
app.py
```

## Nota importante

Esta V2 usa SQLite local. En Streamlit Community Cloud la base puede reiniciarse ante redeploy/sleep. Para producción real conviene migrar a PostgreSQL/Supabase.