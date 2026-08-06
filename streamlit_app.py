import streamlit as st
import pandas as pd
import json
import os

from rounds import parse_spreadsheet, validate_and_normalize_round, save_round_csv, save_round_supabase

st.set_page_config(page_title="Rondas - Gestión", layout="centered")
st.title("Gestión de rondas")

# Chequear si hay DATABASE_URL disponible
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    st.warning("No se encontró DATABASE_URL en las variables de entorno. Para guardar en Supabase, exporta DATABASE_URL con la URL de conexión (p. ej. postgres://...).\nPuedes seguir guardando en CSV como alternativa.")

tab1, tab2 = st.tabs(["Importar desde planilla", "Crear ronda manualmente"])

with tab1:
    st.header("Importar planilla (CSV / XLSX)")
    uploaded = st.file_uploader("Subir archivo", type=["csv", "xlsx"])
    if uploaded is not None:
        try:
            rounds = parse_spreadsheet(uploaded)
            st.success(f"Se detectaron {len(rounds)} rondas válidas")
            df_view = pd.DataFrame([
                {
                    "number": r["number"],
                    "name": r["name"],
                    "start_time": r["start_time"],
                    "end_time": r["end_time"],
                    "metadata": json.dumps(r["metadata"]),
                    "tournament_id": r.get("tournament_id"),
                } for r in rounds
            ])
            st.dataframe(df_view)

            save_target = "Supabase (Postgres)" if DATABASE_URL else "CSV"
            if DATABASE_URL:
                choice = st.radio("Guardar en", ("Supabase (Postgres)", "CSV"))
            else:
                choice = st.radio("Guardar en", ("CSV",))

            if st.button("Guardar rondas"):
                if choice == "CSV":
                    for r in rounds:
                        save_round_csv(r, path="rounds.csv")
                    st.success("Rondas guardadas en rounds.csv")
                else:
                    for r in rounds:
                        try:
                            save_round_supabase(r, db_url=DATABASE_URL)
                        except Exception as e:
                            st.error(f"Error guardando en Supabase: {e}")
                            break
                    else:
                        st.success("Rondas guardadas en la base de datos (Supabase)")

        except Exception as e:
            st.error(f"Error al parsear planilla: {e}")

with tab2:
    st.header("Crear una ronda manualmente")
    with st.form("form_manual"):
        number = st.number_input("Número de ronda", min_value=1, step=1, value=1)
        name = st.text_input("Nombre (opcional)")
        start_time = st.text_input("Inicio (ISO o texto) - opcional", value="")
        end_time = st.text_input("Fin (ISO o texto) - opcional", value="")
        tournament_id = st.text_input("Tournament ID (opcional)")
        metadata_text = st.text_area("Metadata / emparejamientos (JSON) - opcional", height=120)
        submit = st.form_submit_button("Crear ronda")
        if submit:
            raw = {
                "number": number,
                "name": name,
                "start_time": start_time or None,
                "end_time": end_time or None,
                "metadata": None,
                "tournament_id": tournament_id or None,
            }
            if metadata_text.strip():
                try:
                    raw["metadata"] = json.loads(metadata_text)
                except Exception:
                    raw["metadata"] = {"raw": metadata_text}

            try:
                r = validate_and_normalize_round(raw)
                if DATABASE_URL:
                    target = st.radio("Guardar en", ("Supabase (Postgres)", "CSV"))
                else:
                    target = "CSV"

                if target == "CSV":
                    save_round_csv(r, path="rounds.csv")
                    st.success("Ronda guardada en rounds.csv")
                else:
                    try:
                        save_round_supabase(r, db_url=DATABASE_URL)
                        st.success("Ronda guardada en la base de datos (Supabase)")
                    except Exception as e:
                        st.error(f"Error guardando en Supabase: {e}")

                st.write(r)
            except Exception as e:
                st.error(f"Error de validación: {e}")
