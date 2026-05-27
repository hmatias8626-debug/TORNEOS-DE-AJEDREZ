import streamlit as st
import sqlite3
import hashlib
import requests
import pandas as pd
import datetime as dt
from pathlib import Path

APP_TITLE = "Torneos de Ajedrez"
DB_PATH = Path("torneos_ajedrez.db")
API_BASE = "https://api.chess.com/pub"
DEFAULT_PASSWORD = "12345"
HEADERS = {"User-Agent": "torneos-ajedrez-reset/1.0"}

st.set_page_config(page_title=APP_TITLE, layout="wide")

# ---------------- DB ----------------

def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def q(sql, params=(), one=False):
    con = db()
    cur = con.cursor()
    cur.execute(sql, params)
    rows = cur.fetchall()
    con.close()
    return rows[0] if one and rows else (None if one else rows)

def exec_sql(sql, params=()):
    con = db()
    cur = con.cursor()
    cur.execute(sql, params)
    con.commit()
    last = cur.lastrowid
    con.close()
    return last

def init_db():
    con = db()
    cur = con.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        password_hash TEXT,
        chesscom_user TEXT UNIQUE,
        display_name TEXT,
        role TEXT DEFAULT 'player',
        elo INTEGER DEFAULT 1200,
        avatar_url TEXT,
        account_status TEXT DEFAULT 'pending',
        must_change_password INTEGER DEFAULT 1,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS tournaments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        description TEXT,
        status TEXT DEFAULT 'playing',
        rules TEXT DEFAULT 'chess',
        time_class TEXT DEFAULT 'blitz',
        time_control TEXT DEFAULT '600',
        rated_filter TEXT DEFAULT 'any',
        strict_colors INTEGER DEFAULT 1,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS rounds (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tournament_id INTEGER,
        number INTEGER,
        start_datetime TEXT,
        end_datetime TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS registrations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tournament_id INTEGER,
        user_id INTEGER,
        status TEXT DEFAULT 'active',
        wo_count INTEGER DEFAULT 0,
        UNIQUE(tournament_id, user_id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS matches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tournament_id INTEGER,
        round_id INTEGER,
        white_user_id INTEGER,
        black_user_id INTEGER,
        status TEXT DEFAULT 'pending',
        result TEXT,
        result_type TEXT DEFAULT 'normal',
        chesscom_url TEXT,
        game_uuid TEXT,
        locked INTEGER DEFAULT 0,
        detected_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS elo_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        match_id INTEGER,
        user_id INTEGER,
        old_elo INTEGER,
        new_elo INTEGER,
        delta INTEGER,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)

    con.commit()
    con.close()

# ---------------- helpers ----------------

def norm(s):
    return (str(s) if s is not None else "").strip().lower()

def hash_password(p):
    return hashlib.sha256(str(p).encode("utf-8")).hexdigest()

def is_staff(user):
    return user and user["role"] in ("superadmin", "admin", "moderator")

def get_user(uid):
    row = q("SELECT * FROM users WHERE id=?", (uid,), one=True)
    return dict(row) if row else None

def chess_profile(username):
    try:
        r = requests.get(f"{API_BASE}/player/{norm(username)}", headers=HEADERS, timeout=20)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None

def sync_chess_profile(uid, chess_user):
    p = chess_profile(chess_user)
    if p and p.get("avatar"):
        exec_sql("UPDATE users SET avatar_url=? WHERE id=?", (p.get("avatar"), uid))

def get_or_create_player(chess_user, display_name=None):
    chess_user = norm(chess_user)
    row = q("SELECT * FROM users WHERE chesscom_user=?", (chess_user,), one=True)
    if row:
        return row["id"], False

    username = chess_user
    if q("SELECT id FROM users WHERE username=?", (username,), one=True):
        username = f"{chess_user}_{dt.datetime.now().strftime('%H%M%S')}"

    uid = exec_sql("""
        INSERT INTO users(username,password_hash,chesscom_user,display_name,role,account_status,must_change_password)
        VALUES(?,?,?,?,?,?,?)
    """, (username, hash_password(DEFAULT_PASSWORD), chess_user, display_name or chess_user, "player", "pending", 1))
    sync_chess_profile(uid, chess_user)
    return uid, True

def create_user(username, password, chess_user, display_name):
    chess_user = norm(chess_user)
    username = norm(username) or chess_user
    display_name = display_name or username

    count = q("SELECT COUNT(*) c FROM users", one=True)["c"]
    existing = q("SELECT * FROM users WHERE chesscom_user=?", (chess_user,), one=True)

    if existing:
        exec_sql("""
            UPDATE users
            SET username=?, password_hash=?, display_name=?, account_status='active', must_change_password=0
            WHERE id=?
        """, (username, hash_password(password), display_name, existing["id"]))
        return existing["id"]

    role = "superadmin" if count == 0 else "player"
    uid = exec_sql("""
        INSERT INTO users(username,password_hash,chesscom_user,display_name,role,account_status,must_change_password)
        VALUES(?,?,?,?,?,?,?)
    """, (username, hash_password(password), chess_user, display_name, role, "active", 0))
    sync_chess_profile(uid, chess_user)
    return uid

def login(username, password):
    username = norm(username)
    row = q("SELECT * FROM users WHERE username=? OR chesscom_user=?", (username, username), one=True)
    if not row:
        return None, "Usuario no encontrado."
    if row["account_status"] == "suspended":
        return None, "Usuario suspendido."
    if row["password_hash"] != hash_password(password):
        return None, "Contraseña incorrecta."
    if row["account_status"] == "pending":
        exec_sql("UPDATE users SET account_status='active' WHERE id=?", (row["id"],))
        row = q("SELECT * FROM users WHERE id=?", (row["id"],), one=True)
    return dict(row), None

def update_password(uid, password, must_change=0):
    exec_sql("UPDATE users SET password_hash=?, must_change_password=?, account_status='active' WHERE id=?",
             (hash_password(password), must_change, uid))

def read_uploaded_csv(file):
    raw = file.getvalue()
    for enc in ["utf-8", "utf-8-sig", "latin-1", "cp1252"]:
        for sep in [",", ";", "\t"]:
            try:
                import io
                text = raw.decode(enc)
                df = pd.read_csv(io.StringIO(text), sep=sep)
                if len(df.columns) > 1:
                    df.columns = [str(c).strip() for c in df.columns]
                    return df
            except Exception:
                pass
    raise Exception("No pude leer el CSV. Guardalo como CSV UTF-8 o CSV separado por punto y coma.")

# ---------------- Chess.com ----------------

def chess_archives(username):
    r = requests.get(f"{API_BASE}/player/{norm(username)}/games/archives", headers=HEADERS, timeout=20)
    if r.status_code != 200:
        return []
    return r.json().get("archives", [])

def chess_games_between(username, start_dt, end_dt):
    games = []
    archives = chess_archives(username)
    months_needed = set()
    cur = dt.datetime(start_dt.year, start_dt.month, 1)
    while cur <= end_dt:
        months_needed.add(f"{cur.year}/{cur.month:02d}")
        if cur.month == 12:
            cur = dt.datetime(cur.year + 1, 1, 1)
        else:
            cur = dt.datetime(cur.year, cur.month + 1, 1)

    for url in archives:
        if any(m in url for m in months_needed):
            try:
                r = requests.get(url, headers=HEADERS, timeout=20)
                if r.status_code == 200:
                    games.extend(r.json().get("games", []))
            except Exception:
                pass
    return games

def parse_ts(ts):
    try:
        return dt.datetime.fromtimestamp(int(ts))
    except Exception:
        return None

def validate_game(game, white_user, black_user, t, start_dt, end_dt):
    white = norm(game.get("white", {}).get("username"))
    black = norm(game.get("black", {}).get("username"))

    if t["strict_colors"]:
        if white != norm(white_user) or black != norm(black_user):
            return False, "color/usuario distinto"
    else:
        if {white, black} != {norm(white_user), norm(black_user)}:
            return False, "usuarios distintos"

    if game.get("rules") != t["rules"]:
        return False, f"modalidad distinta: {game.get('rules')}"
    if game.get("time_class") != t["time_class"]:
        return False, f"clase distinta: {game.get('time_class')}"
    if str(game.get("time_control")) != str(t["time_control"]):
        return False, f"ritmo distinto: {game.get('time_control')}"

    if t["rated_filter"] == "rated" and not game.get("rated"):
        return False, "no es rated"
    if t["rated_filter"] == "casual" and game.get("rated"):
        return False, "no es casual"

    started = parse_ts(game.get("start_time") or game.get("end_time"))
    if not started:
        return False, "sin fecha"
    if started < start_dt or started > end_dt:
        return False, "fuera de rango"

    return True, "ok"

def score_from_game_for_white(game):
    wr = game.get("white", {}).get("result", "")
    br = game.get("black", {}).get("result", "")
    draws = {"agreed","repetition","stalemate","50move","insufficient","timevsinsufficient"}
    if wr == "win":
        return 1.0
    if br == "win":
        return 0.0
    if wr in draws or br in draws:
        return 0.5
    return None

def result_text(score):
    if score == 1.0:
        return "1-0"
    if score == 0.0:
        return "0-1"
    if score == 0.5:
        return "1/2-1/2"
    return ""

def expected_score(ra, rb):
    return 1 / (1 + 10 ** ((rb - ra) / 400))

def elo_delta(ra, rb, score, k=32):
    return round(k * (score - expected_score(ra, rb)))

def apply_elo(match_id, white_id, black_id, white_score):
    wu = get_user(white_id)
    bu = get_user(black_id)
    if not wu or not bu:
        return
    black_score = 1 - white_score if white_score in (0,1) else 0.5
    dw = elo_delta(wu["elo"], bu["elo"], white_score)
    db = elo_delta(bu["elo"], wu["elo"], black_score)
    exec_sql("UPDATE users SET elo=? WHERE id=?", (wu["elo"] + dw, white_id))
    exec_sql("UPDATE users SET elo=? WHERE id=?", (bu["elo"] + db, black_id))
    exec_sql("INSERT INTO elo_history(match_id,user_id,old_elo,new_elo,delta) VALUES(?,?,?,?,?)",
             (match_id, white_id, wu["elo"], wu["elo"] + dw, dw))
    exec_sql("INSERT INTO elo_history(match_id,user_id,old_elo,new_elo,delta) VALUES(?,?,?,?,?)",
             (match_id, black_id, bu["elo"], bu["elo"] + db, db))

def scan_tournament(tid):
    t = q("SELECT * FROM tournaments WHERE id=?", (tid,), one=True)
    matches = q("""
        SELECT m.*, r.number round_number, r.start_datetime, r.end_datetime,
               wu.chesscom_user white_chess, bu.chesscom_user black_chess
        FROM matches m
        JOIN rounds r ON r.id=m.round_id
        JOIN users wu ON wu.id=m.white_user_id
        JOIN users bu ON bu.id=m.black_user_id
        WHERE m.tournament_id=? AND m.status='pending' AND m.locked=0
    """, (tid,))

    found = 0
    debug = []

    for m in matches:
        start_dt = dt.datetime.fromisoformat(m["start_datetime"])
        end_dt = dt.datetime.fromisoformat(m["end_datetime"])
        games = chess_games_between(m["white_chess"], start_dt, end_dt)

        best_reason = "no se encontraron partidas del usuario en el rango"
        for g in games:
            ok, reason = validate_game(g, m["white_chess"], m["black_chess"], t, start_dt, end_dt)
            if ok:
                score = score_from_game_for_white(g)
                if score is None:
                    best_reason = "partida encontrada pero sin resultado final interpretable"
                    continue
                res = result_text(score)
                exec_sql("""
                    UPDATE matches
                    SET status='finished', result=?, chesscom_url=?, game_uuid=?, locked=1, detected_at=?
                    WHERE id=?
                """, (res, g.get("url"), g.get("uuid"), dt.datetime.now().isoformat(timespec="seconds"), m["id"]))
                apply_elo(m["id"], m["white_user_id"], m["black_user_id"], score)
                found += 1
                best_reason = "detectada"
                break
            else:
                best_reason = reason

        debug.append({
            "Cruce": f"{m['white_chess']} vs {m['black_chess']}",
            "Estado": best_reason
        })

    return found, debug

# ---------------- import fixture ----------------

def import_fixture_csv(df, created_by, rules, time_class, time_control, rated_filter, strict_colors):
    required = {"torneo", "ronda", "fecha_inicio", "fecha_fin", "blancas_chesscom", "negras_chesscom"}
    missing = required - set(df.columns)
    if missing:
        raise Exception(f"Faltan columnas: {', '.join(missing)}")

    created_matches = 0
    created_players = 0

    for torneo_name, group in df.groupby("torneo"):
        torneo_name = str(torneo_name).strip()

        tid = exec_sql("""
            INSERT INTO tournaments(name,description,status,rules,time_class,time_control,rated_filter,strict_colors)
            VALUES(?,?,?,?,?,?,?,?)
        """, (torneo_name, "Fixture importado", "playing", rules, time_class, str(time_control), rated_filter, 1 if strict_colors else 0))

        for ronda, rg in group.groupby("ronda"):
            ronda = int(ronda)
            fi = pd.to_datetime(rg["fecha_inicio"], errors="coerce").min()
            ff = pd.to_datetime(rg["fecha_fin"], errors="coerce").max()
            if pd.isna(fi) or pd.isna(ff):
                continue
            rid = exec_sql("""
                INSERT INTO rounds(tournament_id,number,start_datetime,end_datetime)
                VALUES(?,?,?,?)
            """, (tid, ronda, fi.to_pydatetime().isoformat(timespec="seconds"), ff.to_pydatetime().isoformat(timespec="seconds")))

            for _, row in rg.iterrows():
                w_id, wc = get_or_create_player(row["blancas_chesscom"])
                b_id, bc = get_or_create_player(row["negras_chesscom"])
                created_players += int(wc) + int(bc)
                exec_sql("INSERT OR IGNORE INTO registrations(tournament_id,user_id) VALUES(?,?)", (tid, w_id))
                exec_sql("INSERT OR IGNORE INTO registrations(tournament_id,user_id) VALUES(?,?)", (tid, b_id))
                exec_sql("""
                    INSERT INTO matches(tournament_id,round_id,white_user_id,black_user_id)
                    VALUES(?,?,?,?)
                """, (tid, rid, w_id, b_id))
                created_matches += 1

    return created_matches, created_players

# ---------------- stats ----------------

def standings(tid):
    regs = q("""
        SELECT u.id, u.display_name, u.chesscom_user, u.elo
        FROM registrations r JOIN users u ON u.id=r.user_id
        WHERE r.tournament_id=?
    """, (tid,))
    table = []
    for u in regs:
        rows = q("""
            SELECT * FROM matches
            WHERE tournament_id=? AND status='finished'
            AND (white_user_id=? OR black_user_id=?)
        """, (tid, u["id"], u["id"]))
        pts = w = d = l = 0
        for m in rows:
            if m["white_user_id"] == u["id"]:
                if m["result"] == "1-0": pts += 1; w += 1
                elif m["result"] == "0-1": l += 1
                elif m["result"] == "1/2-1/2": pts += 0.5; d += 1
            else:
                if m["result"] == "0-1": pts += 1; w += 1
                elif m["result"] == "1-0": l += 1
                elif m["result"] == "1/2-1/2": pts += 0.5; d += 1
        table.append({"Jugador": u["display_name"], "Chess.com": u["chesscom_user"], "ELO": u["elo"], "PJ": len(rows), "G": w, "E": d, "P": l, "Puntos": pts})
    table.sort(key=lambda x: (x["Puntos"], x["ELO"]), reverse=True)
    return table

# ---------------- UI ----------------

init_db()

if "user" not in st.session_state:
    st.session_state.user = None

st.markdown("""
<style>
.login-wrap {max-width: 430px; margin: 0 auto;}
[data-testid="stTextInput"] {max-width: 430px;}
.stButton > button {max-width: 180px;}
</style>
""", unsafe_allow_html=True)

st.title("♟️ Torneos de Ajedrez — RESET limpio")

if not st.session_state.user:
    st.markdown('<div class="login-wrap">', unsafe_allow_html=True)
    tab1, tab2 = st.tabs(["Ingresar", "Crear cuenta"])
    with tab1:
        u = st.text_input("Usuario Chess.com", key="login_user")
        p = st.text_input("Contraseña", type="password", key="login_pass")
        if st.button("Ingresar"):
            user, err = login(u, p)
            if user:
                st.session_state.user = user
                st.rerun()
            else:
                st.error(err)
    with tab2:
        chess = st.text_input("Usuario Chess.com", key="reg_chess")
        name = st.text_input("Nombre visible", key="reg_name")
        pw = st.text_input("Contraseña", type="password", key="reg_pw")
        if st.button("Crear cuenta"):
            if chess and pw:
                create_user(chess, pw, chess, name or chess)
                st.success("Cuenta creada. Ahora ingresá.")
            else:
                st.warning("Falta usuario o contraseña.")
    st.markdown("</div>", unsafe_allow_html=True)
    st.stop()

user = get_user(st.session_state.user["id"])
st.sidebar.success(f"{user['display_name']} | {user['role']} | ELO {user['elo']}")
if user["avatar_url"]:
    st.sidebar.image(user["avatar_url"], width=100)
if st.sidebar.button("Cerrar sesión"):
    st.session_state.user = None
    st.rerun()

if user["must_change_password"]:
    st.warning("Tu contraseña es temporal. Cambiala para continuar.")
    n1 = st.text_input("Nueva contraseña", type="password")
    n2 = st.text_input("Repetir nueva contraseña", type="password")
    if st.button("Cambiar contraseña"):
        if n1 and n1 == n2:
            update_password(user["id"], n1, 0)
            st.success("Contraseña cambiada.")
            st.rerun()
        else:
            st.error("No coinciden.")
    st.stop()

menu = ["Torneos", "Importar fixture", "Mi perfil", "Ranking", "Admin usuarios"] if is_staff(user) else ["Torneos", "Mi perfil", "Ranking"]
choice = st.sidebar.radio("Menú", menu)

if choice == "Importar fixture":
    st.header("Importar fixture pendiente")
    st.write("Columnas: torneo, ronda, fecha_inicio, fecha_fin, blancas_chesscom, negras_chesscom")

    c1, c2, c3 = st.columns(3)
    rules = "chess" if c1.selectbox("Modalidad", ["Ajedrez normal", "Chess960"]) == "Ajedrez normal" else "chess960"
    time_class = c2.selectbox("Clase", ["blitz", "rapid", "bullet", "daily"])
    time_control = c3.text_input("Ritmo", value="600")
    rated_filter = {"Cualquiera":"any", "Solo rated":"rated", "Solo casual":"casual"}[st.selectbox("Rated/Casual", ["Cualquiera", "Solo rated", "Solo casual"])]
    strict = st.checkbox("Colores exactos", value=True)

    template = pd.DataFrame([{
        "torneo": "TORNEO N°11",
        "ronda": 1,
        "fecha_inicio": "2026-02-27 00:00",
        "fecha_fin": "2026-04-03 23:59",
        "blancas_chesscom": "matiasbulacio",
        "negras_chesscom": "juan123"
    }])
    st.download_button("Descargar plantilla", template.to_csv(index=False).encode("utf-8"), "fixture.csv", "text/csv")

    file = st.file_uploader("Subir CSV", type=["csv"])
    if file:
        df = read_uploaded_csv(file)
        st.dataframe(df, use_container_width=True)
        if st.button("Importar"):
            try:
                m, p = import_fixture_csv(df, user["id"], rules, time_class, time_control, rated_filter, strict)
                st.success(f"Importado. Cruces: {m}. Jugadores nuevos: {p}.")
            except Exception as e:
                st.error(e)

elif choice == "Torneos":
    st.header("Torneos")
    tours = q("SELECT * FROM tournaments ORDER BY id DESC")
    for t in tours:
        with st.expander(f"{t['name']} — {t['status']} — {t['time_class']} — {t['time_control']}", expanded=True):
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Ritmo", t["time_control"])
            c2.metric("Clase", t["time_class"])
            c3.metric("Modalidad", t["rules"])
            c4.metric("Colores", "Exactos" if t["strict_colors"] else "Flexibles")

            if is_staff(user):
                with st.expander("Modificar torneo"):
                    nt = st.text_input("Ritmo", value=str(t["time_control"]), key=f"tc_{t['id']}")
                    nc = st.selectbox("Clase", ["blitz", "rapid", "bullet", "daily"], index=["blitz","rapid","bullet","daily"].index(t["time_class"]) if t["time_class"] in ["blitz","rapid","bullet","daily"] else 0, key=f"cl_{t['id']}")
                    if st.button("Guardar cambios", key=f"save_{t['id']}"):
                        exec_sql("UPDATE tournaments SET time_control=?, time_class=? WHERE id=?", (nt, nc, t["id"]))
                        st.success("Actualizado.")
                        st.rerun()

                if st.button("Buscar resultados Chess.com", key=f"scan_{t['id']}"):
                    found, debug = scan_tournament(t["id"])
                    st.success(f"Resultados detectados: {found}")
                    st.dataframe(debug, use_container_width=True)

            st.subheader("Cruces")
            ms = q("""
                SELECT m.*, r.number, wu.display_name wn, wu.chesscom_user wc, bu.display_name bn, bu.chesscom_user bc
                FROM matches m JOIN rounds r ON r.id=m.round_id
                JOIN users wu ON wu.id=m.white_user_id
                JOIN users bu ON bu.id=m.black_user_id
                WHERE m.tournament_id=?
                ORDER BY r.number, m.id
            """, (t["id"],))
            st.dataframe([{
                "Ronda": m["number"], "Blancas": m["wc"], "Negras": m["bc"],
                "Estado": m["status"], "Resultado": m["result"] or "", "Link": m["chesscom_url"] or ""
            } for m in ms], use_container_width=True)

            st.subheader("Tabla")
            st.dataframe(standings(t["id"]), use_container_width=True)

elif choice == "Mi perfil":
    st.header("Mi perfil")
    st.metric("ELO", user["elo"])
    st.write(f"Chess.com: **{user['chesscom_user']}**")
    st.write(f"Rol: **{user['role']}**")
    if st.button("Sincronizar avatar"):
        sync_chess_profile(user["id"], user["chesscom_user"])
        st.rerun()

elif choice == "Ranking":
    st.header("Ranking")
    us = q("SELECT display_name,chesscom_user,elo,role,account_status FROM users ORDER BY elo DESC")
    st.dataframe([dict(u) for u in us], use_container_width=True)

elif choice == "Admin usuarios":
    st.header("Admin usuarios")
    us = q("SELECT * FROM users ORDER BY role DESC, display_name")
    st.dataframe([{
        "ID": u["id"], "Nombre": u["display_name"], "Chess": u["chesscom_user"],
        "Usuario": u["username"], "Rol": u["role"], "Estado": u["account_status"], "Clave temporal": u["must_change_password"]
    } for u in us], use_container_width=True)

    st.subheader("Crear jugador con clave 12345")
    new_ch = st.text_input("Chess.com")
    if st.button("Crear jugador"):
        if new_ch:
            get_or_create_player(new_ch)
            st.success("Creado o ya existente.")
            st.rerun()

    labels = {f"{u['display_name']} ({u['chesscom_user']})": u["id"] for u in us}
    if labels:
        st.subheader("Resetear contraseña")
        sel = st.selectbox("Usuario", list(labels.keys()))
        if st.button("Resetear a 12345"):
            update_password(labels[sel], DEFAULT_PASSWORD, 1)
            st.success("Reseteado.")
            st.rerun()