import os
import json
from datetime import datetime
from typing import List, Dict, Any, Optional

import pandas as pd


# --- Utilidades de fecha y validación ---
def parse_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    try:
        # pandas puede manejar múltiples formatos
        return pd.to_datetime(value).to_pydatetime()
    except Exception:
        try:
            return datetime.fromisoformat(str(value))
        except Exception:
            return None


def validate_and_normalize_round(raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normaliza y valida una ronda. Campos esperados:
      - number (obligatorio)
      - name (opcional)
      - start_time, end_time (opcionales)
      - metadata (opcional: dict o JSON string)
      - tournament_id (opcional)
    Devuelve dict listo para guardar.
    Lanza ValueError en caso de error de validación.
    """
    r: Dict[str, Any] = {}

    if "number" not in raw or raw["number"] in (None, ""):
        raise ValueError("Falta 'number' de la ronda")
    try:
        rnum = int(raw["number"])
        if rnum <= 0:
            raise ValueError("El número debe ser positivo")
    except Exception:
        raise ValueError("Número de ronda inválido")
    r["number"] = rnum

    r["name"] = raw.get("name") or f"Ronda {rnum}"
    r["start_time"] = parse_datetime(raw.get("start_time"))
    r["end_time"] = parse_datetime(raw.get("end_time"))

    meta = raw.get("metadata")
    if isinstance(meta, str) and meta.strip():
        try:
            r["metadata"] = json.loads(meta)
        except Exception:
            r["metadata"] = {"raw": meta}
    else:
        r["metadata"] = meta or None

    # tournament_id opcional
    if "tournament_id" in raw and raw["tournament_id"] not in (None, ""):
        try:
            r["tournament_id"] = int(raw["tournament_id"])
        except Exception:
            # dejar como string si no es int
            r["tournament_id"] = raw["tournament_id"]
    else:
        r["tournament_id"] = raw.get("tournament_id")

    return r


# --- Parser de planilla (CSV / XLSX) ---
def parse_spreadsheet(fileobj) -> List[Dict[str, Any]]:
    """
    fileobj: archivo subido en Streamlit (BytesIO), soporta csv/xlsx
    Espera columnas: number (obligatoria), name, start_time, end_time, metadata, tournament_id (opc)
    Devuelve lista de rondas normalizadas o lanza ValueError si hay errores.
    """
    name = getattr(fileobj, "name", "")
    if name.lower().endswith(".csv"):
        df = pd.read_csv(fileobj)
    else:
        df = pd.read_excel(fileobj)

    if "number" not in df.columns:
        raise ValueError("La planilla debe incluir la columna 'number'")

    rounds: List[Dict[str, Any]] = []
    errors: List[str] = []
    for idx, row in df.iterrows():
        raw = {
            "number": row.get("number"),
            "name": row.get("name", ""),
            "start_time": row.get("start_time", None),
            "end_time": row.get("end_time", None),
            "metadata": row.get("metadata", None),
            "tournament_id": row.get("tournament_id", None),
        }
        try:
            nr = validate_and_normalize_round(raw)
            rounds.append(nr)
        except Exception as e:
            errors.append(f"Fila {idx+1}: {e}")

    if errors:
        raise ValueError("Errores en planilla:\n" + "\n".join(errors))
    return rounds


# --- Guardado en CSV como fallback ---
def save_round_csv(round_obj: Dict[str, Any], path: str = "rounds.csv") -> None:
    import pandas as _pd

    row = {
        "number": round_obj["number"],
        "name": round_obj.get("name"),
        "start_time": round_obj.get("start_time").isoformat() if round_obj.get("start_time") else "",
        "end_time": round_obj.get("end_time").isoformat() if round_obj.get("end_time") else "",
        "metadata": json.dumps(round_obj.get("metadata")) if round_obj.get("metadata") is not None else "",
        "tournament_id": round_obj.get("tournament_id"),
    }
    df = _pd.DataFrame([row])
    header = not os.path.exists(path)
    df.to_csv(path, mode="a", header=header, index=False)


# --- Guardado en Supabase/Postgres usando SQLAlchemy ---
def save_round_supabase(round_obj: Dict[str, Any], db_url: Optional[str] = None) -> None:
    """
    Inserta la ronda en la tabla 'rounds' usando la URL de DB indicada por env var DATABASE_URL.
    Requiere que exportes DATABASE_URL en la máquina donde corre Streamlit.
    """
    db_url = db_url or os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DATABASE_URL")
    if not db_url:
        raise RuntimeError("No se encontró DATABASE_URL en el entorno. Setea la variable de entorno con la URL de la base Supabase.")

    from sqlalchemy import create_engine, Column, Integer, String, DateTime, JSON, MetaData, Table
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.exc import SQLAlchemyError

    engine = create_engine(db_url)
    metadata = MetaData()

    rounds_table = Table(
        "rounds",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("number", Integer, nullable=False),
        Column("name", String(200)),
        Column("start_time", DateTime, nullable=True),
        Column("end_time", DateTime, nullable=True),
        Column("metadata", JSONB, nullable=True),
        Column("tournament_id", String(100), nullable=True),
        extend_existing=True,
    )

    # Crear la tabla si no existe (solo si tenés permisos)
    metadata.create_all(engine)

    ins = rounds_table.insert().values(
        number=round_obj.get("number"),
        name=round_obj.get("name"),
        start_time=round_obj.get("start_time"),
        end_time=round_obj.get("end_time"),
        metadata=round_obj.get("metadata"),
        tournament_id=str(round_obj.get("tournament_id")) if round_obj.get("tournament_id") is not None else None,
    )
    conn = engine.connect()
    try:
        conn.execute(ins)
        conn.commit()
    except SQLAlchemyError as e:
        # Re-raise con contexto
        raise
    finally:
        conn.close()
