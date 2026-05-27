import streamlit as st
import sqlite3
import hashlib
import requests
import datetime as dt
import time
import random
from pathlib import Path

APP_TITLE = "Torneos de Ajedrez"
DB_PATH = Path("torneos_ajedrez.db")
API_BASE = "https://api.chess.com/pub"
HEADERS = {"User-Agent": "torneos-ajedrez-streamlit/1.0"}

st.set_page_config(page_title=APP_TITLE, layout="wide")

# ---------------- DB ----------------

def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    con = db()
    cur = con.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        chesscom_user TEXT UNIQUE,
        display_name TEXT,
        role TEXT DEFAULT 'player',
        elo INTEGER DEFAULT 1200,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS tournaments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        description TEXT,
        status TEXT DEFAULT 'open',
        platform TEXT DEFAULT 'chess.com',
        rules TEXT DEFAULT 'chess',
        time_class TEXT DEFAULT 'blitz',
        time_control TEXT DEFAULT '300',
        rated_filter TEXT DEFAULT 'any',
        created_by INTEGER,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        round_started_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS registrations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tournament_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(tournament_id, user_id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS rounds (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tournament_id INTEGER NOT NULL,
        number INTEGER NOT NULL,
        status TEXT DEFAULT 'active',
        started_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS matches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tournament_id INTEGER NOT NULL,
        round_id INTEGER NOT NULL,
        white_user_id INTEGER,
        black_user_id INTEGER,
        status TEXT DEFAULT 'pending',
        result TEXT,
        chesscom_url TEXT,
        game_uuid TEXT,
        detected_at TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(round_id, white_user_id, black_user_id)
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

# ---------------- AUTH ----------------

def hash_password(p):
    return hashlib.sha256(p.encode("utf-8")).hexdigest()

def create_user(username, password, chesscom_user, display_name):
    username = username.strip().lower()
    chesscom_user = chesscom_user.strip().lower()
    display_name = display_name.strip() or username
    role = "admin" if q("SELECT COUNT(*) c FROM users", one=True)["c"] == 0 else "player"
    return exec_sql(
        "INSERT INTO users(username,password_hash,chesscom_user,display_name,role) VALUES(?,?,?,?,?)",
        (username, hash_password(password), chesscom_user, display_name, role)
    )

def login(username, password):
    username = username.strip().lower()
    row = q("SELECT * FROM users WHERE username=?", (username,), one=True)
    if row and row["password_hash"] == hash_password(password):
        return dict(row)
    return None

def get_user(uid):
    row = q("SELECT * FROM users WHERE id=?", (uid,), one=True)
    return dict(row) if row else None

# ---------------- CHESS.COM ----------------

def get_json(url):
    r = requests.get(url, headers=HEADERS, timeout=20)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()

def chess_archives(username):
    data = get_json(f"{API_BASE}/player/{username}/games/archives")
    return data.get("archives", []) if data else []

def chess_games(username, months_back=2):
    archives = chess_archives(username)
    games = []
    for url in archives[-months_back:]:
        data = get_json(url)
        if data:
            games.extend(data.get("games", []))
    games.sort(key=lambda g: g.get("end_time") or g.get("start_time") or 0, reverse=True)
    return games

def norm(s):
    return (s or "").strip().lower()

def parse_ts(ts):
    try:
        return dt.datetime.fromtimestamp(int(ts))
    except Exception:
        return None

def match_players(game, u1, u2):
    white = norm(game.get("white", {}).get("username"))
    black = norm(game.get("black", {}).get("username"))
    return {white, black} == {norm(u1), norm(u2)}

def validate_chess_game(game, u1, u2, t, round_started):
    if not match_players(game, u1, u2):
        return False, "usuarios no coinciden"

    if game.get("rules") != t["rules"]:
        return False, f"modalidad incorrecta: {game.get('rules')}"

    if game.get("time_class") != t["time_class"]:
        return False, f"clase incorrecta: {game.get('time_class')}"

    if str(game.get("time_control")) != str(t["time_control"]):
        return False, f"ritmo incorrecto: {game.get('time_control')}"

    if t["rated_filter"] == "rated" and not game.get("rated"):
        return False, "no es rated"

    if t["rated_filter"] == "casual" and game.get("rated"):
        return False, "no es casual"

    started = parse_ts(game.get("start_time") or game.get("end_time"))
    if round_started and started:
        rs = dt.datetime.fromisoformat(round_started)
        if started < rs:
            return False, "partida anterior a la ronda"

    return True, "ok"

def result_for_user(game, chess_user):
    chess_user = norm(chess_user)
    white = norm(game.get("white", {}).get("username"))
    black = norm(game.get("black", {}).get("username"))

    wr = game.get("white", {}).get("result", "")
    br = game.get("black", {}).get("result", "")

    if chess_user == white:
        my, opp = wr, br
    else:
        my, opp = br, wr

    draws = {"agreed","repetition","stalemate","50move","insufficient","timevsinsufficient"}
    if my == "win":
        return 1.0
    if my in draws:
        return 0.5
    if opp == "win":
        return 0.0
    return None

def expected_score(ra, rb):
    return 1 / (1 + 10 ** ((rb - ra) / 400))

def elo_delta(ra, rb, score, k=32):
    return round(k * (score - expected_score(ra, rb)))

# ---------------- TOURNAMENT LOGIC ----------------

def create_tournament(name, desc, rules, time_class, time_control, rated_filter, created_by):
    return exec_sql("""
        INSERT INTO tournaments(name,description,rules,time_class,time_control,rated_filter,created_by)
        VALUES(?,?,?,?,?,?,?)
    """, (name, desc, rules, time_class, time_control, rated_filter, created_by))

def standings(tid):
    regs = q("""
        SELECT u.id, u.display_name, u.chesscom_user, u.elo
        FROM registrations r
        JOIN users u ON u.id=r.user_id
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
                if m["result"] == "1-0":
                    pts += 1; w += 1
                elif m["result"] == "0-1":
                    l += 1
                elif m["result"] == "1/2-1/2":
                    pts += 0.5; d += 1
            else:
                if m["result"] == "0-1":
                    pts += 1; w += 1
                elif m["result"] == "1-0":
                    l += 1
                elif m["result"] == "1/2-1/2":
                    pts += 0.5; d += 1
        table.append({
            "Jugador": u["display_name"],
            "Chess.com": u["chesscom_user"],
            "ELO": u["elo"],
            "PJ": len(rows),
            "G": w,
            "E": d,
            "P": l,
            "Puntos": pts
        })
    table.sort(key=lambda x: (x["Puntos"], x["ELO"]), reverse=True)
    return table

def generate_round_one(tid):
    existing = q("SELECT * FROM rounds WHERE tournament_id=?", (tid,))
    if existing:
        raise Exception("Este torneo ya tiene rondas generadas.")

    players = q("""
        SELECT u.id FROM registrations r
        JOIN users u ON u.id=r.user_id
        WHERE r.tournament_id=?
    """, (tid,))
    ids = [p["id"] for p in players]
    if len(ids) < 2:
        raise Exception("Necesitás al menos 2 jugadores inscriptos.")

    random.shuffle(ids)
    rid = exec_sql("INSERT INTO rounds(tournament_id,number,status,started_at) VALUES(?,1,'active',?)",
                   (tid, dt.datetime.now().isoformat(timespec="seconds")))

    exec_sql("UPDATE tournaments SET status='playing', round_started_at=? WHERE id=?",
             (dt.datetime.now().isoformat(timespec="seconds"), tid))

    for i in range(0, len(ids)-1, 2):
        exec_sql("""
            INSERT INTO matches(tournament_id,round_id,white_user_id,black_user_id)
            VALUES(?,?,?,?)
        """, (tid, rid, ids[i], ids[i+1]))

    if len(ids) % 2 == 1:
        bye = ids[-1]
        mid = exec_sql("""
            INSERT INTO matches(tournament_id,round_id,white_user_id,black_user_id,status,result)
            VALUES(?,?,?,?,?,?)
        """, (tid, rid, bye, None, "finished", "BYE"))

    return rid

def apply_match_result(match, game, white_user, black_user):
    if match["status"] == "finished":
        return

    white_score = result_for_user(game, white_user["chesscom_user"])
    if white_score is None:
        return

    if white_score == 1:
        result = "1-0"
    elif white_score == 0:
        result = "0-1"
    else:
        result = "1/2-1/2"

    exec_sql("""
        UPDATE matches
        SET status='finished', result=?, chesscom_url=?, game_uuid=?, detected_at=?
        WHERE id=?
    """, (result, game.get("url"), game.get("uuid"), dt.datetime.now().isoformat(timespec="seconds"), match["id"]))

    # ELO
    wu = get_user(white_user["id"])
    bu = get_user(black_user["id"])
    ws = white_score
    bs = 1 - ws if ws in (0,1) else 0.5

    dw = elo_delta(wu["elo"], bu["elo"], ws)
    db_ = elo_delta(bu["elo"], wu["elo"], bs)

    exec_sql("UPDATE users SET elo=? WHERE id=?", (wu["elo"] + dw, wu["id"]))
    exec_sql("UPDATE users SET elo=? WHERE id=?", (bu["elo"] + db_, bu["id"]))

    exec_sql("INSERT INTO elo_history(match_id,user_id,old_elo,new_elo,delta) VALUES(?,?,?,?,?)",
             (match["id"], wu["id"], wu["elo"], wu["elo"] + dw, dw))
    exec_sql("INSERT INTO elo_history(match_id,user_id,old_elo,new_elo,delta) VALUES(?,?,?,?,?)",
             (match["id"], bu["id"], bu["elo"], bu["elo"] + db_, db_))

def scan_tournament(tid):
    t = q("SELECT * FROM tournaments WHERE id=?", (tid,), one=True)
    matches = q("""
        SELECT * FROM matches
        WHERE tournament_id=? AND status!='finished' AND black_user_id IS NOT NULL
    """, (tid,))
    found = []
    errors = []
    for m in matches:
        wu = get_user(m["white_user_id"])
        bu = get_user(m["black_user_id"])
        try:
            games = chess_games(wu["chesscom_user"], months_back=2)
            for g in games:
                ok, reason = validate_chess_game(g, wu["chesscom_user"], bu["chesscom_user"], t, t["round_started_at"])
                if ok:
                    apply_match_result(m, g, wu, bu)
                    found.append((m["id"], g.get("url")))
                    break
        except Exception as e:
            errors.append(f"{wu['chesscom_user']} vs {bu['chesscom_user']}: {e}")
    return found, errors

# ---------------- UI ----------------

init_db()

if "user" not in st.session_state:
    st.session_state.user = None

st.title("♟️ Torneos de Ajedrez")

if not st.session_state.user:
    tab_login, tab_reg = st.tabs(["Ingresar", "Registrarme"])

    with tab_login:
        st.subheader("Ingresar")
        u = st.text_input("Usuario", key="login_user")
        p = st.text_input("Contraseña", type="password", key="login_pass")
        if st.button("Ingresar"):
            user = login(u, p)
            if user:
                st.session_state.user = user
                st.rerun()
            else:
                st.error("Usuario o contraseña incorrectos.")

    with tab_reg:
        st.subheader("Crear cuenta")
        nu = st.text_input("Usuario para la app")
        nd = st.text_input("Nombre visible")
        nc = st.text_input("Usuario de Chess.com")
        np = st.text_input("Contraseña", type="password")
        if st.button("Registrarme"):
            try:
                if not nu or not np or not nc:
                    st.warning("Completá usuario, contraseña y usuario Chess.com.")
                else:
                    create_user(nu, np, nc, nd)
                    st.success("Usuario creado. Ahora ingresá.")
            except Exception as e:
                st.error(f"No pude crear el usuario: {e}")

    st.stop()

user = get_user(st.session_state.user["id"])
st.sidebar.success(f"{user['display_name']} | {user['role']} | ELO {user['elo']}")
if st.sidebar.button("Cerrar sesión"):
    st.session_state.user = None
    st.rerun()

menu = st.sidebar.radio(
    "Menú",
    ["Torneos", "Crear torneo", "Mi perfil", "Ranking general"],
    index=0
)

if menu == "Crear torneo":
    st.header("Crear torneo")
    if user["role"] != "admin":
        st.warning("Solo el primer usuario creado queda como admin para esta V1.")
    else:
        name = st.text_input("Nombre del torneo")
        desc = st.text_area("Descripción")
        rules_label = st.selectbox("Modalidad", ["Ajedrez normal", "Chess960"])
        rules = "chess" if rules_label == "Ajedrez normal" else "chess960"
        tc = st.selectbox("Clase", ["blitz", "rapid", "bullet", "daily"])
        tcontrol = st.text_input("Ritmo exacto", value="300", help="300=5+0, 600=10+0, 180+2=3+2")
        rated_label = st.selectbox("Rated/Casual", ["Cualquiera", "Solo rated", "Solo casual"])
        rated = {"Cualquiera":"any", "Solo rated":"rated", "Solo casual":"casual"}[rated_label]

        if st.button("Crear torneo"):
            if name:
                create_tournament(name, desc, rules, tc, tcontrol, rated, user["id"])
                st.success("Torneo creado.")
            else:
                st.warning("Poné un nombre.")

elif menu == "Torneos":
    st.header("Torneos")
    tournaments = q("SELECT * FROM tournaments ORDER BY id DESC")
    if not tournaments:
        st.info("Todavía no hay torneos.")
    for t in tournaments:
        with st.expander(f"{t['name']} — {t['status']} — {t['rules']} {t['time_class']} {t['time_control']}", expanded=True):
            st.write(t["description"] or "")
            regs = q("""
                SELECT u.display_name, u.chesscom_user FROM registrations r
                JOIN users u ON u.id=r.user_id
                WHERE r.tournament_id=?
                ORDER BY u.display_name
            """, (t["id"],))
            already = q("SELECT * FROM registrations WHERE tournament_id=? AND user_id=?", (t["id"], user["id"]), one=True)

            c1, c2, c3 = st.columns(3)
            c1.metric("Inscriptos", len(regs))
            c2.metric("Estado", t["status"])
            c3.metric("Ritmo", t["time_control"])

            if t["status"] == "open":
                if already:
                    st.success("Ya estás inscripto.")
                else:
                    if st.button(f"Inscribirme en {t['name']}", key=f"reg_{t['id']}"):
                        try:
                            exec_sql("INSERT INTO registrations(tournament_id,user_id) VALUES(?,?)", (t["id"], user["id"]))
                            st.success("Inscripción realizada.")
                            st.rerun()
                        except Exception as e:
                            st.error(e)

            st.write("**Jugadores inscriptos:**")
            st.dataframe([dict(r) for r in regs], use_container_width=True)

            if user["role"] == "admin":
                cA, cB = st.columns(2)
                if cA.button("Generar ronda 1", key=f"gen_{t['id']}"):
                    try:
                        generate_round_one(t["id"])
                        st.success("Ronda 1 generada.")
                        st.rerun()
                    except Exception as e:
                        st.error(e)

                if cB.button("Buscar resultados Chess.com", key=f"scan_{t['id']}"):
                    with st.spinner("Buscando partidas..."):
                        found, errors = scan_tournament(t["id"])
                    if found:
                        st.success(f"Partidas detectadas: {len(found)}")
                    else:
                        st.info("No se detectaron partidas nuevas.")
                    for e in errors:
                        st.warning(e)
                    st.rerun()

            st.write("**Cruces:**")
            matches = q("""
                SELECT m.*, 
                       wu.display_name white_name, wu.chesscom_user white_chess,
                       bu.display_name black_name, bu.chesscom_user black_chess
                FROM matches m
                LEFT JOIN users wu ON wu.id=m.white_user_id
                LEFT JOIN users bu ON bu.id=m.black_user_id
                WHERE m.tournament_id=?
                ORDER BY m.id
            """, (t["id"],))
            mrows = []
            for m in matches:
                mrows.append({
                    "Blancas": m["white_name"],
                    "Chess blancas": m["white_chess"],
                    "Negras": m["black_name"] or "BYE",
                    "Chess negras": m["black_chess"] or "",
                    "Estado": m["status"],
                    "Resultado": m["result"] or "",
                    "Link": m["chesscom_url"] or ""
                })
            st.dataframe(mrows, use_container_width=True)

            st.write("**Tabla de posiciones:**")
            st.dataframe(standings(t["id"]), use_container_width=True)

elif menu == "Mi perfil":
    st.header("Mi perfil")
    st.metric("ELO interno", user["elo"])
    st.write(f"**Nombre:** {user['display_name']}")
    st.write(f"**Usuario Chess.com:** {user['chesscom_user']}")

    matches = q("""
        SELECT m.*, t.name tournament_name,
               wu.display_name white_name, bu.display_name black_name
        FROM matches m
        JOIN tournaments t ON t.id=m.tournament_id
        LEFT JOIN users wu ON wu.id=m.white_user_id
        LEFT JOIN users bu ON bu.id=m.black_user_id
        WHERE m.status='finished' AND (m.white_user_id=? OR m.black_user_id=?)
        ORDER BY m.detected_at DESC
    """, (user["id"], user["id"]))
    rows = []
    for m in matches:
        rows.append({
            "Torneo": m["tournament_name"],
            "Blancas": m["white_name"],
            "Negras": m["black_name"] or "BYE",
            "Resultado": m["result"],
            "Link": m["chesscom_url"] or ""
        })
    st.subheader("Historial de partidas")
    st.dataframe(rows, use_container_width=True)

    elo = q("""
        SELECT old_elo, new_elo, delta, created_at
        FROM elo_history
        WHERE user_id=?
        ORDER BY id DESC
    """, (user["id"],))
    st.subheader("Historial ELO")
    st.dataframe([dict(e) for e in elo], use_container_width=True)

elif menu == "Ranking general":
    st.header("Ranking general")
    users = q("""
        SELECT display_name, chesscom_user, elo, role
        FROM users
        ORDER BY elo DESC
    """)
    st.dataframe([dict(u) for u in users], use_container_width=True)