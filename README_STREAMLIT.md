# TORNEOS-DE-AJEDREZ - Streamlit: carga manual de rondas y desde planilla

Se agregó una interfaz Streamlit que permite:
- Importar rondas desde una planilla (CSV / XLSX).
- Crear rondas manualmente mediante un formulario.
- Guardar las rondas en Supabase (Postgres) usando la variable de entorno DATABASE_URL, o en rounds.csv como fallback.

Archivos añadidos:
- rounds.py: lógica común de parsing/validación y guardado (CSV / Supabase)
- streamlit_app.py: interfaz Streamlit con dos pestañas
- requirements.txt: dependencias para correr la app

Instrucciones rápidas:
1. Exportar la URL de conexión a la base Supabase en la variable de entorno DATABASE_URL. Ejemplo (Linux/macOS):

   export DATABASE_URL="postgres://usuario:pass@host:5432/dbname"

   O configurar `DATABASE_URL` en los secrets/variables de entorno del despliegue.

2. Instalar dependencias:

   pip install -r requirements.txt

3. Ejecutar la app:

   streamlit run streamlit_app.py

Notas:
- La tabla `rounds` se crea automáticamente si la conexión tiene permisos, con columnas: id, number, name, start_time, end_time, metadata (jsonb), tournament_id.
- Si ya tenés una tabla rounds con un esquema distinto, ajusta `rounds.py::save_round_supabase` para coincidir con tu esquema.
