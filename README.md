# TORNEOS-DE-AJEDREZ V8 SUPABASE

V8 usa PostgreSQL/Supabase como base persistente.

## Streamlit Secrets

En Streamlit Cloud → Manage app → Settings → Secrets pegar:

```toml
DB_HOST = "db.xxxxxxxxxxxxxxxxxxxx.supabase.co"
DB_PORT = "5432"
DB_NAME = "postgres"
DB_USER = "postgres"
DB_PASSWORD = "TU_PASSWORD"
```

Si no existen esos secrets, la app usa SQLite local como fallback.

## Incluye

- Usuarios persistentes.
- Torneos persistentes.
- Rondas y resultados persistentes.
- Importar fixture completo.
- Importar rondas a torneo existente.
- Editar fechas de rondas.
- Editar ritmo regular/playoff.
- Corrección de usuarios Chess.com.
- Alias Chess.com.
- Detección automática/manual.
- Revisión de colores invertidos.
- Usuarios inexistentes en Chess.com en diagnóstico.
- Resultados manuales.
- WO por vencimiento.
