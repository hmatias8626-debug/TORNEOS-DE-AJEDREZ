import streamlit as st
import sqlite3
import hashlib
import requests
import datetime as dt
import time
import random
from pathlib import Path
from itertools import combinations

APP_TITLE = "Torneos de Ajedrez"
DB_PATH = Path("torneos_ajedrez.db")
API_BASE = "https://api.chess.com/pub"
HEADERS = {"User-Agent": "torneos-ajedrez-streamlit/2.0"}

st.set_page_config(page_title=APP_TITLE, layout="wide")

# =========================
# DB
# =========================

def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def column_exists(table, column):
    con = db()
    cur = con.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    cols = [r[1] for r in cur.fetchall()]
    con.close()
    return column in cols

def ensure_column(table, column, definition):
    if not column_exists(table, column):
        con = db()
        cur = con.cursor()
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        con.commit()
        con.close()

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
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
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

    cur.execute("""
    CREATE TABLE IF NOT EXISTS playoff_cups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tournament_id INTEGER,
        cup_name TEXT,
        start_rank INTEGER,
        end_rank INTEGER
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS round_windows (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tournament_id INTEGER,
        round_number INTEGER,
        start_datetime TEXT,
        end_datetime TEXT,
        UNIQUE(tournament_id, round_number)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS admin_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        match_id INTEGER,
        admin_user_id INTEGER,
        old_result TEXT,
        new_result TEXT,
        reason TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS playoff_matches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tournament_id INTEGER,
        cup_id INTEGER,
        stage TEXT,
        white_user_id INTEGER,
        black_user_id INTEGER,
        winner_user_id INTEGER,
        status TEXT DEFAULT 'pending',
        result TEXT,
        chesscom_url TEXT,
        game_uuid TEXT,
        detected_at TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)

    con.commit()
    con.close()

    # migrations for old V1 DB
    ensure_column("tournaments", "swiss_rounds", "INTEGER DEFAULT 5")
    ensure_column("tournaments", "pairing_mode_round1", "TEXT DEFAULT 'random'")
    ensure_column("tournaments", "playoff_enabled", "INTEGER DEFAULT 1")
    ensure_column("matches", "locked", "INTEGER DEFAULT 0")
    ensure_column("matches", "manual_pairing", "INTEGER DEFAULT 0")
    ensure_column("matches", "bye", "INTEGER DEFAULT 0")

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

def exec_many(sql, rows):
    con = db()
    cur = con.cursor()
    cur.executemany(sql, rows)
    con.commit()
    con.close()

# =========================
# AUTH
# =========================

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
    row = q("SELECT * FROM users WHERE username=?", (username.strip().lower(),), one=True)
    if row and row["password_hash"] == hash_password(password):
        return dict(row)
    return None

def get_user(uid):
    if not uid:
        return None
    row = q("SELECT * FROM users WHERE id=?", (uid,), one=True)
    return dict(row) if row else None

# =========================
# CHESS.COM
# =========================

def norm(s):
    return (s or "").strip().lower()

def get_json(url):
    r = requests.get(url, headers=HEADERS, timeout=20)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()

@st.cache_data(ttl=60)
def chess_archives(username):
    data = get_json(f"{API_BASE}/player/{username}/games/archives")
    return data.get("archives", []) if data else []

@st.cache_data(ttl=60)
def chess_games(username, months_back=2):
    archives = chess_archives(username)
    games = []
    for url in archives[-months_back:]:
        data = get_json(url)
        if data:
            games.extend(data.get("games", []))
    games.sort(key=lambda g: g.get("end_time") or g.get("start_time") or 0, reverse=True)
    return games

def parse_ts(ts):
    try:
        return dt.datetime.fromtimestamp(int(ts))
    except Exception:
        return None

def match_players(game, u1, u2):
    white = norm(game.get("white", {}).get("username"))
    black = norm(game.get("black", {}).get("username"))
    return {white, black} == {norm(u1), norm(u2)}

def validate_chess_game(game, u1, u2, t, start_dt, end_dt):
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
    if start_dt and started and started < start_dt:
        return False, "partida anterior al inicio de ronda"
    if end_dt and started and started > end_dt:
        return False, "partida posterior al fin de ronda"

    return True, "ok"

def result_for_user(game, chess_user):
    chess_user = norm(chess_user)
    white = norm(game.get("white", {}).get("username"))

    wr = game.get("white", {}).get("result", "")
    br = game.get("black", {}).get("result", "")
    my, opp = (wr, br) if chess_user == white else (br, wr)

    draws = {"agreed","repetition","stalemate","50move","insufficient","timevsinsufficient"}
    if my == "win":
        return 1.0
    if my in draws:
        return 0.5
    if opp == "win":
        return 0.0
    return None

# =========================
# ELO / STANDINGS
# =========================

def expected_score(ra, rb):
    return 1 / (1 + 10 ** ((rb - ra) / 400))

def elo_delta(ra, rb, score, k=32):
    return round(k * (score - expected_score(ra, rb)))

def apply_elo(match_id, white_user_id, black_user_id, white_score):
    wu = get_user(white_user_id)
    bu = get_user(black_user_id)
    if not wu or not bu:
        return

    black_score = 1 - white_score if white_score in (0,1) else 0.5

    dw = elo_delta(wu["elo"], bu["elo"], white_score)
    db_ = elo_delta(bu["elo"], wu["elo"], black_score)

    exec_sql("UPDATE users SET elo=? WHERE id=?", (wu["elo"] + dw, wu["id"]))
    exec_sql("UPDATE users SET elo=? WHERE id=?", (bu["elo"] + db_, bu["id"]))

    exec_sql("INSERT INTO elo_history(match_id,user_id,old_elo,new_elo,delta) VALUES(?,?,?,?,?)",
             (match_id, wu["id"], wu["elo"], wu["elo"] + dw, dw))
    exec_sql("INSERT INTO elo_history(match_id,user_id,old_elo,new_elo,delta) VALUES(?,?,?,?,?)",
             (match_id, bu["id"], bu["elo"], bu["elo"] + db_, db_))

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
            if m["result"] == "BYE":
                if m["white_user_id"] == u["id"]:
                    pts += 1; w += 1
                continue
            if m["white_user_id"] == u["id"]:
                if m["result"] == "1-0": pts += 1; w += 1
                elif m["result"] == "0-1": l += 1
                elif m["result"] == "1/2-1/2": pts += 0.5; d += 1
            else:
                if m["result"] == "0-1": pts += 1; w += 1
                elif m["result"] == "1-0": l += 1
                elif m["result"] == "1/2-1/2": pts += 0.5; d += 1

        table.append({
            "Rank": 0,
            "Jugador": u["display_name"],
            "User ID": u["id"],
            "Chess.com": u["chesscom_user"],
            "ELO": u["elo"],
            "PJ": len(rows),
            "G": w,
            "E": d,
            "P": l,
            "Puntos": pts
        })
    table.sort(key=lambda x: (x["Puntos"], x["ELO"], x["G"]), reverse=True)
    for i, r in enumerate(table, start=1):
        r["Rank"] = i
    return table

# =========================
# TOURNAMENT LOGIC
# =========================

def create_tournament(name, desc, rules, time_class, time_control, rated_filter, swiss_rounds, pairing_mode, playoff_enabled, cups, windows, created_by):
    tid = exec_sql("""
        INSERT INTO tournaments(name,description,rules,time_class,time_control,rated_filter,swiss_rounds,pairing_mode_round1,playoff_enabled,created_by)
        VALUES(?,?,?,?,?,?,?,?,?,?)
    """, (name, desc, rules, time_class, time_control, rated_filter, swiss_rounds, pairing_mode, 1 if playoff_enabled else 0, created_by))

    for name_cup, start_rank, end_rank in cups:
        exec_sql("INSERT INTO playoff_cups(tournament_id,cup_name,start_rank,end_rank) VALUES(?,?,?,?)",
                 (tid, name_cup, start_rank, end_rank))

    for rn, start_dt, end_dt in windows:
        exec_sql("INSERT INTO round_windows(tournament_id,round_number,start_datetime,end_datetime) VALUES(?,?,?,?)",
                 (tid, rn, start_dt, end_dt))

    return tid

def registered_players(tid):
    return q("""
        SELECT u.* FROM registrations r
        JOIN users u ON u.id=r.user_id
        WHERE r.tournament_id=?
        ORDER BY u.elo DESC, u.display_name
    """, (tid,))

def existing_opponents(tid, uid):
    rows = q("""
        SELECT white_user_id, black_user_id FROM matches
        WHERE tournament_id=? AND black_user_id IS NOT NULL
        AND (white_user_id=? OR black_user_id=?)
    """, (tid, uid, uid))
    opp = set()
    for r in rows:
        opp.add(r["black_user_id"] if r["white_user_id"] == uid else r["white_user_id"])
    return opp

def current_round_number(tid):
    row = q("SELECT MAX(number) n FROM rounds WHERE tournament_id=?", (tid,), one=True)
    return row["n"] if row and row["n"] else 0

def round_window(tid, rn):
    row = q("SELECT * FROM round_windows WHERE tournament_id=? AND round_number=?", (tid, rn), one=True)
    if not row:
        return None, None
    start = dt.datetime.fromisoformat(row["start_datetime"]) if row["start_datetime"] else None
    end = dt.datetime.fromisoformat(row["end_datetime"]) if row["end_datetime"] else None
    return start, end

def create_round(tid, number):
    start, _ = round_window(tid, number)
    started_at = start.isoformat(timespec="seconds") if start else dt.datetime.now().isoformat(timespec="seconds")
    rid = exec_sql("INSERT INTO rounds(tournament_id,number,status,started_at) VALUES(?,?,?,?)",
                   (tid, number, "active", started_at))
    exec_sql("UPDATE tournaments SET status='playing', round_started_at=? WHERE id=?", (started_at, tid))
    return rid

def add_match(tid, rid, white_id, black_id, manual=0, bye=0, status="pending", result=None):
    return exec_sql("""
        INSERT INTO matches(tournament_id,round_id,white_user_id,black_user_id,manual_pairing,bye,status,result,locked)
        VALUES(?,?,?,?,?,?,?,?,?)
    """, (tid, rid, white_id, black_id, manual, bye, status, result, 1 if status == "finished" else 0))

def generate_round_one_auto(tid):
    if current_round_number(tid) > 0:
        raise Exception("Este torneo ya tiene rondas.")
    players = list(registered_players(tid))
    if len(players) < 2:
        raise Exception("Necesitás al menos 2 jugadores.")

    t = q("SELECT * FROM tournaments WHERE id=?", (tid,), one=True)
    rid = create_round(tid, 1)

    if t["pairing_mode_round1"] == "elo":
        # primera mitad contra segunda mitad
        half = (len(players)+1)//2
        top = players[:half]
        bottom = players[half:]
        random.shuffle(bottom)
        pairs = list(zip(top, bottom))
        bye_player = top[-1] if len(top) > len(bottom) else None
    else:
        random.shuffle(players)
        pairs = [(players[i], players[i+1]) for i in range(0, len(players)-1, 2)]
        bye_player = players[-1] if len(players) % 2 else None

    for a,b in pairs:
        if random.choice([True, False]):
            add_match(tid, rid, a["id"], b["id"])
        else:
            add_match(tid, rid, b["id"], a["id"])

    if bye_player:
        add_match(tid, rid, bye_player["id"], None, bye=1, status="finished", result="BYE")

def generate_swiss_round(tid):
    rn = current_round_number(tid) + 1
    t = q("SELECT * FROM tournaments WHERE id=?", (tid,), one=True)
    if rn > t["swiss_rounds"]:
        raise Exception("Ya se jugaron todas las rondas suizas.")
    prev = q("SELECT * FROM rounds WHERE tournament_id=? AND number=?", (tid, rn-1), one=True)
    if prev:
        unfinished = q("SELECT COUNT(*) c FROM matches WHERE round_id=? AND status!='finished'", (prev["id"],), one=True)["c"]
        if unfinished:
            raise Exception("Hay partidas pendientes en la ronda anterior.")

    table = standings(tid)
    ids = [r["User ID"] for r in table]
    paired = set()
    pairs = []
    bye = None

    for uid in ids:
        if uid in paired:
            continue
        opponents = existing_opponents(tid, uid)
        candidate = None
        for other in ids:
            if other == uid or other in paired:
                continue
            if other not in opponents:
                candidate = other
                break
        if candidate is None:
            for other in ids:
                if other != uid and other not in paired:
                    candidate = other
                    break
        if candidate:
            pairs.append((uid, candidate))
            paired.add(uid); paired.add(candidate)
        else:
            bye = uid
            paired.add(uid)

    rid = create_round(tid, rn)
    for a,b in pairs:
        if random.choice([True, False]):
            add_match(tid, rid, a, b)
        else:
            add_match(tid, rid, b, a)
    if bye:
        add_match(tid, rid, bye, None, bye=1, status="finished", result="BYE")

def manual_round_one_form(tid):
    players = registered_players(tid)
    if len(players) < 2:
        st.warning("Necesitás al menos 2 jugadores.")
        return

    st.info("Elegí los cruces manuales de ronda 1. Los jugadores no seleccionados no se emparejan.")
    names = {f"{p['display_name']} ({p['chesscom_user']})": p["id"] for p in players}
    used = set()
    pairs = []

    max_pairs = len(players)//2
    for i in range(max_pairs):
        c1, c2 = st.columns(2)
        opts1 = [""] + [k for k,v in names.items() if v not in used]
        white = c1.selectbox(f"Mesa {i+1} - Blancas", opts1, key=f"mw_{tid}_{i}")
        if white:
            temp_used = used | {names[white]}
        else:
            temp_used = used
        opts2 = [""] + [k for k,v in names.items() if v not in temp_used]
        black = c2.selectbox(f"Mesa {i+1} - Negras", opts2, key=f"mb_{tid}_{i}")
        if white and black:
            pairs.append((names[white], names[black]))
            used.add(names[white]); used.add(names[black])

    if st.button("Guardar ronda 1 manual", key=f"save_manual_{tid}"):
        if current_round_number(tid) > 0:
            st.error("Este torneo ya tiene rondas.")
            return
        if not pairs:
            st.error("No cargaste cruces.")
            return
        rid = create_round(tid, 1)
        for w,b in pairs:
            add_match(tid, rid, w, b, manual=1)
        st.success("Ronda 1 manual creada.")
        st.rerun()

def apply_match_result(match, game, white_user, black_user):
    fresh = q("SELECT * FROM matches WHERE id=?", (match["id"],), one=True)
    if fresh["status"] == "finished" or fresh["locked"]:
        return False

    white_score = result_for_user(game, white_user["chesscom_user"])
    if white_score is None:
        return False

    if white_score == 1: result = "1-0"
    elif white_score == 0: result = "0-1"
    else: result = "1/2-1/2"

    exec_sql("""
        UPDATE matches
        SET status='finished', result=?, chesscom_url=?, game_uuid=?, detected_at=?, locked=1
        WHERE id=?
    """, (result, game.get("url"), game.get("uuid"), dt.datetime.now().isoformat(timespec="seconds"), match["id"]))

    apply_elo(match["id"], white_user["id"], black_user["id"], white_score)
    return True

def scan_tournament(tid):
    t = q("SELECT * FROM tournaments WHERE id=?", (tid,), one=True)
    matches = q("""
        SELECT m.*, r.number round_number
        FROM matches m
        JOIN rounds r ON r.id=m.round_id
        WHERE m.tournament_id=? 
        AND m.status!='finished'
        AND IFNULL(m.locked,0)=0
        AND m.black_user_id IS NOT NULL
    """, (tid,))
    found, errors = [], []
    for m in matches:
        wu = get_user(m["white_user_id"])
        bu = get_user(m["black_user_id"])
        start_dt, end_dt = round_window(tid, m["round_number"])
        try:
            games = chess_games(wu["chesscom_user"], months_back=3)
            for g in games:
                ok, reason = validate_chess_game(g, wu["chesscom_user"], bu["chesscom_user"], t, start_dt, end_dt)
                if ok:
                    if apply_match_result(m, g, wu, bu):
                        found.append((m["id"], g.get("url")))
                    break
        except Exception as e:
            errors.append(f"{wu['chesscom_user']} vs {bu['chesscom_user']}: {e}")
    return found, errors

def generate_playoffs(tid):
    t = q("SELECT * FROM tournaments WHERE id=?", (tid,), one=True)
    if not t["playoff_enabled"]:
        raise Exception("Este torneo no tiene playoffs activados.")
    if current_round_number(tid) < t["swiss_rounds"]:
        raise Exception("Todavía no terminaron las rondas suizas.")
    unfinished = q("SELECT COUNT(*) c FROM matches WHERE tournament_id=? AND status!='finished'", (tid,), one=True)["c"]
    if unfinished:
        raise Exception("Hay partidas suizas pendientes.")

    existing = q("SELECT COUNT(*) c FROM playoff_matches WHERE tournament_id=?", (tid,), one=True)["c"]
    if existing:
        raise Exception("Los playoffs ya fueron generados.")

    table = standings(tid)
    cups = q("SELECT * FROM playoff_cups WHERE tournament_id=? ORDER BY start_rank", (tid,))
    for cup in cups:
        players = [r for r in table if cup["start_rank"] <= r["Rank"] <= cup["end_rank"]]
        ids = [p["User ID"] for p in players]
        if len(ids) < 2:
            continue

        # Llave de 8: 1v8, 4v5, 2v7, 3v6. Si no son 8, empareja extremos.
        if len(ids) == 8:
            order = [(0,7),(3,4),(1,6),(2,5)]
        else:
            order = [(i, len(ids)-1-i) for i in range(len(ids)//2)]

        for a,b in order:
            exec_sql("""
                INSERT INTO playoff_matches(tournament_id,cup_id,stage,white_user_id,black_user_id)
                VALUES(?,?,?,?,?)
            """, (tid, cup["id"], "Cuartos" if len(ids) >= 8 else "Inicial", ids[a], ids[b]))

# =========================
# UI
# =========================

init_db()

if "user" not in st.session_state:
    st.session_state.user = None

st.title("♟️ Torneos de Ajedrez — V2")

if not st.session_state.user:
    tab_login, tab_reg = st.tabs(["Ingresar", "Registrarme"])
    with tab_login:
        u = st.text_input("Usuario")
        p = st.text_input("Contraseña", type="password")
        if st.button("Ingresar"):
            user = login(u, p)
            if user:
                st.session_state.user = user
                st.rerun()
            else:
                st.error("Usuario o contraseña incorrectos.")
    with tab_reg:
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

menu = st.sidebar.radio("Menú", ["Torneos", "Crear torneo", "Mi perfil", "Ranking general"], index=0)

if menu == "Crear torneo":
    st.header("Crear torneo V2")
    if user["role"] != "admin":
        st.warning("Solo admin puede crear torneos.")
    else:
        name = st.text_input("Nombre del torneo")
        desc = st.text_area("Descripción")

        c1, c2, c3 = st.columns(3)
        with c1:
            rules = "chess" if st.selectbox("Modalidad", ["Ajedrez normal", "Chess960"]) == "Ajedrez normal" else "chess960"
        with c2:
            tc = st.selectbox("Clase", ["blitz", "rapid", "bullet", "daily"])
        with c3:
            tcontrol = st.text_input("Ritmo exacto", value="300", help="300=5+0, 600=10+0, 180+2=3+2")

        rated_label = st.selectbox("Rated/Casual", ["Cualquiera", "Solo rated", "Solo casual"])
        rated = {"Cualquiera":"any", "Solo rated":"rated", "Solo casual":"casual"}[rated_label]

        swiss_rounds = st.number_input("Cantidad de rondas suizas", min_value=1, max_value=20, value=5)
        pairing_mode = st.selectbox("Ronda 1", ["manual", "random", "elo"], format_func=lambda x: {"manual":"Manual", "random":"Aleatoria", "elo":"Por ELO"}[x])

        st.subheader("Fechas por ronda")
        windows = []
        today = dt.date.today()
        for rn in range(1, int(swiss_rounds)+1):
            with st.expander(f"Ronda {rn}", expanded=(rn==1)):
                c1, c2, c3, c4 = st.columns(4)
                sd = c1.date_input(f"Inicio fecha R{rn}", value=today, key=f"sd_{rn}")
                stime = c2.time_input(f"Inicio hora R{rn}", value=dt.time(0,0), key=f"st_{rn}")
                ed = c3.date_input(f"Fin fecha R{rn}", value=today + dt.timedelta(days=7), key=f"ed_{rn}")
                etime = c4.time_input(f"Fin hora R{rn}", value=dt.time(23,59), key=f"et_{rn}")
                start_dt = dt.datetime.combine(sd, stime).isoformat(timespec="seconds")
                end_dt = dt.datetime.combine(ed, etime).isoformat(timespec="seconds")
                windows.append((rn, start_dt, end_dt))

        playoff_enabled = st.checkbox("Activar playoffs/copas", value=True)
        cups = []
        if playoff_enabled:
            st.subheader("Copas")
            cup_count = st.number_input("Cantidad de copas", min_value=1, max_value=10, value=3)
            next_rank = 1
            default_names = ["Copa Oro", "Copa Plata", "Copa Bronce", "Copa Cobre", "Copa Promoción"]
            for i in range(int(cup_count)):
                c1, c2, c3 = st.columns(3)
                cup_name = c1.text_input(f"Nombre copa {i+1}", value=default_names[i] if i < len(default_names) else f"Copa {i+1}")
                size = c2.number_input(f"Clasificados copa {i+1}", min_value=2, max_value=64, value=8, step=2)
                start_rank = next_rank
                end_rank = next_rank + int(size) - 1
                c3.write(f"Ranks: {start_rank} al {end_rank}")
                cups.append((cup_name, start_rank, end_rank))
                next_rank = end_rank + 1

        if st.button("Crear torneo V2"):
            if not name:
                st.warning("Poné un nombre.")
            else:
                create_tournament(name, desc, rules, tc, tcontrol, rated, int(swiss_rounds), pairing_mode, playoff_enabled, cups, windows, user["id"])
                st.success("Torneo creado.")
                st.rerun()

elif menu == "Torneos":
    st.header("Torneos")
    tournaments = q("SELECT * FROM tournaments ORDER BY id DESC")
    if not tournaments:
        st.info("Todavía no hay torneos.")

    for t in tournaments:
        with st.expander(f"{t['name']} — {t['status']} — {t['rules']} {t['time_class']} {t['time_control']} — {t['swiss_rounds']} rondas", expanded=True):
            st.write(t["description"] or "")
            regs = q("""
                SELECT u.display_name, u.chesscom_user, u.elo FROM registrations r
                JOIN users u ON u.id=r.user_id
                WHERE r.tournament_id=?
                ORDER BY u.elo DESC
            """, (t["id"],))
            already = q("SELECT * FROM registrations WHERE tournament_id=? AND user_id=?", (t["id"], user["id"]), one=True)

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Inscriptos", len(regs))
            c2.metric("Estado", t["status"])
            c3.metric("Rondas suizas", t["swiss_rounds"])
            c4.metric("Ritmo", t["time_control"])

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
                st.write("**Administración:**")
                a,b,c,d = st.columns(4)
                if a.button("Generar ronda 1 auto", key=f"gen1_{t['id']}"):
                    try:
                        generate_round_one_auto(t["id"])
                        st.success("Ronda 1 generada.")
                        st.rerun()
                    except Exception as e:
                        st.error(e)

                if b.button("Generar siguiente suiza", key=f"genswiss_{t['id']}"):
                    try:
                        generate_swiss_round(t["id"])
                        st.success("Siguiente ronda generada.")
                        st.rerun()
                    except Exception as e:
                        st.error(e)

                if c.button("Buscar resultados Chess.com", key=f"scan_{t['id']}"):
                    with st.spinner("Buscando partidas dentro del rango de cada ronda..."):
                        found, errors = scan_tournament(t["id"])
                    if found:
                        st.success(f"Partidas detectadas y bloqueadas: {len(found)}")
                    else:
                        st.info("No se detectaron partidas nuevas.")
                    for e in errors:
                        st.warning(e)
                    st.rerun()

                if d.button("Generar playoffs", key=f"playoffs_{t['id']}"):
                    try:
                        generate_playoffs(t["id"])
                        st.success("Playoffs generados.")
                        st.rerun()
                    except Exception as e:
                        st.error(e)

                if t["pairing_mode_round1"] == "manual" and current_round_number(t["id"]) == 0:
                    with st.expander("Crear ronda 1 manual"):
                        manual_round_one_form(t["id"])

            st.write("**Ventanas de rondas:**")
            wins = q("SELECT round_number, start_datetime, end_datetime FROM round_windows WHERE tournament_id=? ORDER BY round_number", (t["id"],))
            st.dataframe([dict(w) for w in wins], use_container_width=True)

            st.write("**Cruces suizos:**")
            matches = q("""
                SELECT m.*, r.number round_number,
                       wu.display_name white_name, wu.chesscom_user white_chess,
                       bu.display_name black_name, bu.chesscom_user black_chess
                FROM matches m
                JOIN rounds r ON r.id=m.round_id
                LEFT JOIN users wu ON wu.id=m.white_user_id
                LEFT JOIN users bu ON bu.id=m.black_user_id
                WHERE m.tournament_id=?
                ORDER BY r.number, m.id
            """, (t["id"],))
            mrows = []
            for m in matches:
                mrows.append({
                    "Ronda": m["round_number"],
                    "Blancas": m["white_name"],
                    "Chess blancas": m["white_chess"],
                    "Negras": m["black_name"] or "BYE",
                    "Chess negras": m["black_chess"] or "",
                    "Estado": m["status"],
                    "Bloqueada": "Sí" if m["locked"] else "No",
                    "Resultado": m["result"] or "",
                    "Link": m["chesscom_url"] or ""
                })
            st.dataframe(mrows, use_container_width=True)

            st.write("**Tabla de posiciones:**")
            table = standings(t["id"])
            visible_table = [{k:v for k,v in r.items() if k != "User ID"} for r in table]
            st.dataframe(visible_table, use_container_width=True)

            st.write("**Copas configuradas:**")
            cups_rows = q("SELECT cup_name, start_rank, end_rank FROM playoff_cups WHERE tournament_id=? ORDER BY start_rank", (t["id"],))
            st.dataframe([dict(r) for r in cups_rows], use_container_width=True)

            st.write("**Playoffs:**")
            pm = q("""
                SELECT pm.*, pc.cup_name,
                       wu.display_name white_name, bu.display_name black_name, win.display_name winner_name
                FROM playoff_matches pm
                JOIN playoff_cups pc ON pc.id=pm.cup_id
                LEFT JOIN users wu ON wu.id=pm.white_user_id
                LEFT JOIN users bu ON bu.id=pm.black_user_id
                LEFT JOIN users win ON win.id=pm.winner_user_id
                WHERE pm.tournament_id=?
                ORDER BY pc.start_rank, pm.id
            """, (t["id"],))
            st.dataframe([{
                "Copa": r["cup_name"],
                "Etapa": r["stage"],
                "Blancas": r["white_name"],
                "Negras": r["black_name"],
                "Estado": r["status"],
                "Resultado": r["result"] or "",
                "Ganador": r["winner_name"] or "",
                "Link": r["chesscom_url"] or ""
            } for r in pm], use_container_width=True)

elif menu == "Mi perfil":
    st.header("Mi perfil")
    st.metric("ELO interno", user["elo"])
    st.write(f"**Nombre:** {user['display_name']}")
    st.write(f"**Usuario Chess.com:** {user['chesscom_user']}")

    matches = q("""
        SELECT m.*, t.name tournament_name, r.number round_number,
               wu.display_name white_name, bu.display_name black_name
        FROM matches m
        JOIN tournaments t ON t.id=m.tournament_id
        JOIN rounds r ON r.id=m.round_id
        LEFT JOIN users wu ON wu.id=m.white_user_id
        LEFT JOIN users bu ON bu.id=m.black_user_id
        WHERE m.status='finished' AND (m.white_user_id=? OR m.black_user_id=?)
        ORDER BY m.detected_at DESC
    """, (user["id"], user["id"]))
    st.subheader("Historial de partidas")
    st.dataframe([{
        "Torneo": m["tournament_name"],
        "Ronda": m["round_number"],
        "Blancas": m["white_name"],
        "Negras": m["black_name"] or "BYE",
        "Resultado": m["result"],
        "Bloqueada": "Sí" if m["locked"] else "No",
        "Link": m["chesscom_url"] or ""
    } for m in matches], use_container_width=True)

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