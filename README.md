# TORNEOS-DE-AJEDREZ

Primera versión de prueba de plataforma web para torneos de ajedrez.

## Funciones V1

- Registro/login de jugadores
- Primer usuario creado queda como admin
- Usuario de Chess.com asociado al perfil
- Crear torneos
- Inscripción automática de jugadores
- Generar ronda 1
- Detectar partidas en Chess.com por:
  - usuario 1 vs usuario 2
  - modalidad chess / chess960
  - clase blitz / rapid / bullet / daily
  - ritmo exacto
  - rated/casual
  - fecha posterior al inicio de ronda
- Carga automática de resultado
- Tabla de posiciones
- ELO interno básico
- Historial de partidas

## Ejecutar local

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Subir a Streamlit Community Cloud

Main file path:

```text
app.py
```

## Nota

Esta V1 usa SQLite local. En Streamlit Cloud puede reiniciarse la base si la app se duerme o se redeploya. Para una versión final conviene PostgreSQL.