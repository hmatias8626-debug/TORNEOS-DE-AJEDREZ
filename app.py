import streamlit as st
import sqlite3
import hashlib
import requests
import datetime as dt
import random
import pandas as pd
from pathlib import Path

APP_TITLE = "Torneos de Ajedrez"
DB_PATH = Path("torneos_ajedrez.db")
API_BASE = "https://api.chess.com/pub"
HEADERS = {"User-Agent": "torneos-ajedrez-streamlit/4.0"}

st.set_page_config(page_title=APP_TITLE, layout="wide")

# =========================
# DB
# =========================

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
        avatar_url TEXT,
        country TEXT,
        chess_title TEXT,
        chess_status TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS tournaments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        description TEXT,
        status TEXT DEFAULT 'open',
        tournament_type TEXT DEFAULT 'swiss',
        platform TEXT DEFAULT 'chess.com',
        rules TEXT DEFAULT 'chess',
        time_class TEXT DEFAULT 'blitz',
        time_control TEXT DEFAULT '300',
        rated_filter TEXT DEFAULT 'any',
        swiss_rounds INTEGER DEFAULT 5,
        free_fixture_games_per_player INTEGER DEFAULT 5,
        pairing_mode_round1 TEXT DEFAULT 'random',
        pairing_mode_free TEXT DEFAULT 'random',
        strict_colors INTEGER DEFAULT 1,
        playoff_enabled INTEGER DEFAULT 1,
        historical INTEGER DEFAULT 0,
        created_by INTEGER,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
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
    CREATE TABLE IF NOT EXISTS matches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tournament_id INTEGER NOT NULL,
        round_id INTEGER,
        white_user_id INTEGER,
        black_user_id INTEGER,
        status TEXT DEFAULT 'pending',
        result TEXT,
        chesscom_url TEXT,
        game_uuid TEXT,
        detected_at TEXT,
        locked INTEGER DEFAULT 0,
        manual_pairing INTEGER DEFAULT 0,
        bye INTEGER DEFAULT 0,
        imported INTEGER DEFAULT 0,
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
    con.commit()
    con.close()

    for table, col, definition in [
        ("users", "avatar_url", "TEXT"), ("users", "country", "TEXT"), ("users", "chess_title", "TEXT"), ("users", "chess_status", "TEXT"),
        ("tournaments", "tournament_type", "TEXT DEFAULT 'swiss'"), ("tournaments", "free_fixture_games_per_player", "INTEGER DEFAULT 5"),
        ("tournaments", "pairing_mode_free", "TEXT DEFAULT 'random'"), ("tournaments", "strict_colors", "INTEGER DEFAULT 1"),
        ("tournaments", "swiss_rounds", "INTEGER DEFAULT 5"), ("tournaments", "pairing_mode_round1", "TEXT DEFAULT 'random'"),
        ("tournaments", "playoff_enabled", "INTEGER DEFAULT 1"), ("tournaments", "historical", "INTEGER DEFAULT 0"),
        ("matches", "locked", "INTEGER DEFAULT 0"), ("matches", "manual_pairing", "INTEGER DEFAULT 0"),
        ("matches", "bye", "INTEGER DEFAULT 0"), ("matches", "imported", "INTEGER DEFAULT 0"),
    ]:
        ensure_column(table, col, definition)

# =========================
# CHESS.COM / USERS
# =========================

def norm(s):
    return (s or "").strip().lower()

def get_json(url):
    r = requests.get(url, headers=HEADERS, timeout=20)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()

@st.cache_data(ttl=300)
def chess_profile(username):
    if not username:
        return None
    return get_json(f"{API_BASE}/player/{norm(username)}")

@st.cache_data(ttl=60)
def chess_archives(username):
    data = get_json(f"{API_BASE}/player/{norm(username)}/games/archives")
    return data.get("archives", []) if data else []

@st.cache_data(ttl=60)
def chess_games(username, months_back=3):
    games = []
    for url in chess_archives(username)[-months_back:]:
        data = get_json(url)
        if data:
            games.extend(data.get("games", []))
    games.sort(key=lambda g: g.get("end_time") or g.get("start_time") or 0, reverse=True)
    return games

def sync_chess_profile(user_id, chesscom_user):
    try:
        p = chess_profile(chesscom_user)
        if not p:
            return False, "No encontré el perfil de Chess.com."
        exec_sql("UPDATE users SET avatar_url=?, country=?, chess_title=?, chess_status=? WHERE id=?",
                 (p.get("avatar"), p.get("country"), p.get("title"), p.get("status"), user_id))
        return True, "Perfil sincronizado."
    except Exception as e:
        return False, str(e)

def hash_password(p):
    return hashlib.sha256(p.encode("utf-8")).hexdigest()

def is_superadmin(user):
    return user and user["role"] == "superadmin"

def is_admin(user):
    return user and user["role"] in ("superadmin", "admin")

def get_user(uid):
    row = q("SELECT * FROM users WHERE id=?", (uid,), one=True)
    return dict(row) if row else None

def get_or_create_player_by_chess(chesscom_user, display_name=None):
    chesscom_user = norm(chesscom_user)
    row = q("SELECT * FROM users WHERE chesscom_user=?", (chesscom_user,), one=True)
    if row:
        return dict(row)["id"], False

    # Jugador histórico no reclamado: no puede iniciar sesión, pero conserva historial.
    username = f"hist_{chesscom_user}"
    i = 1
    while q("SELECT id FROM users WHERE username=?", (username,), one=True):
        i += 1
        username = f"hist_{chesscom_user}_{i}"

    uid = exec_sql("""
        INSERT INTO users(username,password_hash,chesscom_user,display_name,role,elo)
        VALUES(?,?,?,?,?,?)
    """, (username, hash_password("NO_LOGIN_HISTORICO"), chesscom_user, display_name or chesscom_user, "player", 1200))
    sync_chess_profile(uid, chesscom_user)
    return uid, True

def create_user(username, password, chesscom_user, display_name):
    username = username.strip().lower()
    chesscom_user = norm(chesscom_user)
    display_name = display_name.strip() or username
    count = q("SELECT COUNT(*) c FROM users", one=True)["c"]

    existing = q("SELECT * FROM users WHERE chesscom_user=?", (chesscom_user,), one=True)
    if existing and str(existing["username"]).startswith("hist_"):
        role = "superadmin" if count == 0 else existing["role"]
        exec_sql("""
            UPDATE users
            SET username=?, password_hash=?, display_name=?, role=?
            WHERE id=?
        """, (username, hash_password(password), display_name, role, existing["id"]))
        sync_chess_profile(existing["id"], chesscom_user)
        return existing["id"]

    role = "superadmin" if count == 0 else "player"
    uid = exec_sql("""
        INSERT INTO users(username,password_hash,chesscom_user,display_name,role)
        VALUES(?,?,?,?,?)
    """, (username, hash_password(password), chesscom_user, display_name, role))
    sync_chess_profile(uid, chesscom_user)
    return uid

def login(username, password):
    row = q("SELECT * FROM users WHERE username=?", (username.strip().lower(),), one=True)
    if row and not str(row["username"]).startswith("hist_") and row["password_hash"] == hash_password(password):
        return dict(row)
    return None

def user_label(u):
    return f"{u['display_name']} ({u['chesscom_user']})"

# =========================
# GAME VALIDATION / ELO
# =========================

def parse_ts(ts):
    try:
        return dt.datetime.fromtimestamp(int(ts))
    except Exception:
        return None

def validate_chess_game(game, white_user, black_user, t, start_dt, end_dt):
    white = norm(game.get("white", {}).get("username"))
    black = norm(game.get("black", {}).get("username"))

    if t["strict_colors"]:
        if white != norm(white_user) or black != norm(black_user):
            return False, "usuarios/colores no coinciden"
    else:
        if {white, black} != {norm(white_user), norm(black_user)}:
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
        return False, "partida anterior al rango"
    if end_dt and started and started > end_dt:
        return False, "partida posterior al rango"

    return True, "ok"

def result_for_user(game, chess_user):
    chess_user = norm(chess_user)
    white = norm(game.get("white", {}).get("username"))
    wr = game.get("white", {}).get("result", "")
    br = game.get("black", {}).get("result", "")
    my, opp = (wr, br) if chess_user == white else (br, wr)
    draws = {"agreed","repetition","stalemate","50move","insufficient","timevsinsufficient"}
    if my == "win": return 1.0
    if my in draws: return 0.5
    if opp == "win": return 0.0
    return None

def expected_score(ra, rb):
    return 1 / (1 + 10 ** ((rb - ra) / 400))

def elo_delta(ra, rb, score, k=32):
    return round(k * (score - expected_score(ra, rb)))

def apply_elo(match_id, white_user_id, black_user_id, white_score):
    wu = get_user(white_user_id)
    bu = get_user(black_user_id)
    if not wu or not bu:
        return
    black_score = 1 - white_score if white_score in (0, 1) else 0.5
    dw = elo_delta(wu["elo"], bu["elo"], white_score)
    db_ = elo_delta(bu["elo"], wu["elo"], black_score)
    exec_sql("UPDATE users SET elo=? WHERE id=?", (wu["elo"] + dw, wu["id"]))
    exec_sql("UPDATE users SET elo=? WHERE id=?", (bu["elo"] + db_, bu["id"]))
    exec_sql("INSERT INTO elo_history(match_id,user_id,old_elo,new_elo,delta) VALUES(?,?,?,?,?)",
             (match_id, wu["id"], wu["elo"], wu["elo"] + dw, dw))
    exec_sql("INSERT INTO elo_history(match_id,user_id,old_elo,new_elo,delta) VALUES(?,?,?,?,?)",
             (match_id, bu["id"], bu["elo"], bu["elo"] + db_, db_))

def score_from_result(result):
    if result == "1-0": return 1.0
    if result == "0-1": return 0.0
    if result in ("1/2-1/2", "0.5-0.5", "½-½"): return 0.5
    return None

# =========================
# STATS
# =========================

def player_stats(user_id):
    rows = q("""
        SELECT * FROM matches
        WHERE status='finished' AND (white_user_id=? OR black_user_id=?)
    """, (user_id, user_id))
    pj = len(rows)
    w = d = l = pts = 0
    for m in rows:
        if m["result"] == "BYE":
            if m["white_user_id"] == user_id:
                w += 1; pts += 1
            continue
        if m["white_user_id"] == user_id:
            if m["result"] == "1-0": w += 1; pts += 1
            elif m["result"] == "0-1": l += 1
            elif m["result"] == "1/2-1/2": d += 1; pts += 0.5
        else:
            if m["result"] == "0-1": w += 1; pts += 1
            elif m["result"] == "1-0": l += 1
            elif m["result"] == "1/2-1/2": d += 1; pts += 0.5
    perf = round((pts / pj) * 100, 1) if pj else 0
    tournaments = q("""
        SELECT COUNT(DISTINCT tournament_id) c FROM matches
        WHERE status='finished' AND (white_user_id=? OR black_user_id=?)
    """, (user_id, user_id), one=True)["c"]
    rank_row = q("SELECT id FROM users ORDER BY elo DESC")
    rank = next((i for i, r in enumerate(rank_row, 1) if r["id"] == user_id), None)
    return {"PJ": pj, "G": w, "E": d, "P": l, "Puntos": pts, "Rendimiento %": perf, "Torneos": tournaments, "Ranking": rank or "-"}

def standings(tid):
    regs = q("""
        SELECT u.id, u.display_name, u.chesscom_user, u.elo
        FROM registrations r JOIN users u ON u.id=r.user_id
        WHERE r.tournament_id=?
    """, (tid,))
    table = []
    for u in regs:
        rows = q("""
            SELECT * FROM matches WHERE tournament_id=? AND status='finished'
            AND (white_user_id=? OR black_user_id=?)
        """, (tid, u["id"], u["id"]))
        pts = w = d = l = 0
        for m in rows:
            if m["result"] == "BYE" and m["white_user_id"] == u["id"]:
                pts += 1; w += 1
            elif m["white_user_id"] == u["id"]:
                if m["result"] == "1-0": pts += 1; w += 1
                elif m["result"] == "0-1": l += 1
                elif m["result"] == "1/2-1/2": pts += 0.5; d += 1
            else:
                if m["result"] == "0-1": pts += 1; w += 1
                elif m["result"] == "1-0": l += 1
                elif m["result"] == "1/2-1/2": pts += 0.5; d += 1
        table.append({"Rank": 0, "User ID": u["id"], "Jugador": u["display_name"], "Chess.com": u["chesscom_user"], "ELO": u["elo"], "PJ": len(rows), "G": w, "E": d, "P": l, "Puntos": pts})
    table.sort(key=lambda x: (x["Puntos"], x["ELO"], x["G"]), reverse=True)
    for i, r in enumerate(table, 1):
        r["Rank"] = i
    return table

# =========================
# TOURNAMENT
# =========================

def round_window(tid, rn):
    row = q("SELECT * FROM round_windows WHERE tournament_id=? AND round_number=?", (tid, rn), one=True)
    if not row:
        return None, None
    return dt.datetime.fromisoformat(row["start_datetime"]), dt.datetime.fromisoformat(row["end_datetime"])

def current_round_number(tid):
    row = q("SELECT MAX(number) n FROM rounds WHERE tournament_id=?", (tid,), one=True)
    return row["n"] if row and row["n"] else 0

def registered_players(tid):
    return q("""
        SELECT u.* FROM registrations r JOIN users u ON u.id=r.user_id
        WHERE r.tournament_id=? ORDER BY u.elo DESC, u.display_name
    """, (tid,))

def create_round(tid, rn):
    start, _ = round_window(tid, rn)
    rid = exec_sql("INSERT INTO rounds(tournament_id,number,status,started_at) VALUES(?,?,?,?)", (tid, rn, "active", start.isoformat(timespec="seconds")))
    exec_sql("UPDATE tournaments SET status='playing' WHERE id=?", (tid,))
    return rid

def add_match(tid, rid, white_id, black_id, manual=0, bye=0, status="pending", result=None, imported=0):
    return exec_sql("""
        INSERT INTO matches(tournament_id,round_id,white_user_id,black_user_id,manual_pairing,bye,status,result,locked,imported)
        VALUES(?,?,?,?,?,?,?,?,?,?)
    """, (tid, rid, white_id, black_id, manual, bye, status, result, 1 if status == "finished" else 0, imported))

def create_tournament(data, cups, windows):
    tid = exec_sql("""
        INSERT INTO tournaments(name,description,tournament_type,rules,time_class,time_control,rated_filter,swiss_rounds,free_fixture_games_per_player,pairing_mode_round1,pairing_mode_free,strict_colors,playoff_enabled,historical,created_by)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (data["name"], data["desc"], data["type"], data["rules"], data["time_class"], data["time_control"], data["rated_filter"], data["swiss_rounds"], data["free_games"], data["pairing_round1"], data["pairing_free"], 1 if data["strict_colors"] else 0, 1 if data["playoff"] else 0, data.get("historical",0), data["created_by"]))
    for cup_name, start_rank, end_rank in cups:
        exec_sql("INSERT INTO playoff_cups(tournament_id,cup_name,start_rank,end_rank) VALUES(?,?,?,?)", (tid, cup_name, start_rank, end_rank))
    for rn, start_dt, end_dt in windows:
        exec_sql("INSERT INTO round_windows(tournament_id,round_number,start_datetime,end_datetime) VALUES(?,?,?,?)", (tid, rn, start_dt, end_dt))
    return tid

def generate_free_fixture(tid):
    t = q("SELECT * FROM tournaments WHERE id=?", (tid,), one=True)
    if current_round_number(tid) > 0:
        raise Exception("Este torneo ya tiene fixture generado.")
    players = list(registered_players(tid))
    if len(players) < 2:
        raise Exception("Necesitás al menos 2 jugadores.")
    games_per_player = int(t["free_fixture_games_per_player"])
    rid = create_round(tid, 1)
    ids = [p["id"] for p in players]
    counts = {uid: 0 for uid in ids}
    all_pairs = [(a,b) for i,a in enumerate(ids) for b in ids[i+1:]]
    if t["pairing_mode_free"] == "elo":
        elo = {p["id"]: p["elo"] for p in players}
        all_pairs.sort(key=lambda ab: abs(elo[ab[0]] - elo[ab[1]]))
    else:
        random.shuffle(all_pairs)
    for a,b in all_pairs:
        if counts[a] < games_per_player and counts[b] < games_per_player:
            w, bl = (a,b) if random.choice([True,False]) else (b,a)
            add_match(tid, rid, w, bl)
            counts[a] += 1; counts[b] += 1
        if all(c >= games_per_player for c in counts.values()):
            break

def generate_round_one_auto(tid):
    if current_round_number(tid) > 0:
        raise Exception("Este torneo ya tiene rondas.")
    players = list(registered_players(tid))
    if len(players) < 2:
        raise Exception("Necesitás al menos 2 jugadores.")
    t = q("SELECT * FROM tournaments WHERE id=?", (tid,), one=True)
    rid = create_round(tid, 1)
    if t["pairing_mode_round1"] == "elo":
        half = (len(players)+1)//2
        pairs = list(zip(players[:half], players[half:]))
        bye = players[half-1] if len(players[:half]) > len(players[half:]) else None
    else:
        random.shuffle(players)
        pairs = [(players[i], players[i+1]) for i in range(0, len(players)-1, 2)]
        bye = players[-1] if len(players) % 2 else None
    for a,b in pairs:
        w, bl = (a["id"], b["id"]) if random.choice([True,False]) else (b["id"], a["id"])
        add_match(tid, rid, w, bl)
    if bye:
        add_match(tid, rid, bye["id"], None, bye=1, status="finished", result="BYE")

def apply_match_result(match, game, wu, bu):
    fresh = q("SELECT * FROM matches WHERE id=?", (match["id"],), one=True)
    if fresh["status"] == "finished" or fresh["locked"]:
        return False
    white_score = result_for_user(game, wu["chesscom_user"])
    if white_score is None:
        return False
    result = "1-0" if white_score == 1 else "0-1" if white_score == 0 else "1/2-1/2"
    exec_sql("UPDATE matches SET status='finished', result=?, chesscom_url=?, game_uuid=?, detected_at=?, locked=1 WHERE id=?",
             (result, game.get("url"), game.get("uuid"), dt.datetime.now().isoformat(timespec="seconds"), match["id"]))
    apply_elo(match["id"], wu["id"], bu["id"], white_score)
    return True

def scan_tournament(tid):
    t = q("SELECT * FROM tournaments WHERE id=?", (tid,), one=True)
    matches = q("""
        SELECT m.*, IFNULL(r.number,1) round_number
        FROM matches m LEFT JOIN rounds r ON r.id=m.round_id
        WHERE m.tournament_id=? AND m.status!='finished' AND IFNULL(m.locked,0)=0 AND m.black_user_id IS NOT NULL
    """, (tid,))
    found, errors = [], []
    for m in matches:
        wu = get_user(m["white_user_id"])
        bu = get_user(m["black_user_id"])
        start_dt, end_dt = round_window(tid, m["round_number"])
        try:
            for g in chess_games(wu["chesscom_user"], months_back=3):
                ok, _ = validate_chess_game(g, wu["chesscom_user"], bu["chesscom_user"], t, start_dt, end_dt)
                if ok and apply_match_result(m, g, wu, bu):
                    found.append((m["id"], g.get("url")))
                    break
        except Exception as e:
            errors.append(f"{wu['chesscom_user']} vs {bu['chesscom_user']}: {e}")
    return found, errors

def import_history_csv(df, created_by):
    required = {"torneo","ronda","fecha","blancas_chesscom","negras_chesscom","resultado"}
    missing = required - set(df.columns)
    if missing:
        raise Exception(f"Faltan columnas: {', '.join(missing)}")

    imported = 0
    for torneo_name, group in df.groupby("torneo"):
        tid_row = q("SELECT * FROM tournaments WHERE name=? AND historical=1", (str(torneo_name),), one=True)
        if tid_row:
            tid = tid_row["id"]
        else:
            dates = pd.to_datetime(group["fecha"], errors="coerce")
            start = dates.min().to_pydatetime() if not dates.isna().all() else dt.datetime.now()
            end = dates.max().to_pydatetime() if not dates.isna().all() else dt.datetime.now()
            tid = create_tournament({
                "name": str(torneo_name), "desc": "Torneo histórico importado", "type": "historical",
                "rules": "chess", "time_class": "blitz", "time_control": "300", "rated_filter": "any",
                "swiss_rounds": int(group["ronda"].max()) if "ronda" in group else 1,
                "free_games": 0, "pairing_round1": "manual", "pairing_free": "manual",
                "strict_colors": True, "playoff": False, "historical": 1, "created_by": created_by
            }, [], [(1, start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds"))])

        for _, row in group.iterrows():
            ronda = int(row.get("ronda", 1))
            r = q("SELECT * FROM rounds WHERE tournament_id=? AND number=?", (tid, ronda), one=True)
            if not r:
                rid = exec_sql("INSERT INTO rounds(tournament_id,number,status,started_at) VALUES(?,?,?,?)", (tid, ronda, "finished", str(row.get("fecha"))))
            else:
                rid = r["id"]

            w_id, _ = get_or_create_player_by_chess(row["blancas_chesscom"])
            b_id, _ = get_or_create_player_by_chess(row["negras_chesscom"])

            try:
                exec_sql("INSERT OR IGNORE INTO registrations(tournament_id,user_id) VALUES(?,?)", (tid, w_id))
                exec_sql("INSERT OR IGNORE INTO registrations(tournament_id,user_id) VALUES(?,?)", (tid, b_id))
            except Exception:
                pass

            result = str(row["resultado"]).strip()
            if result == "0.5-0.5" or result == "½-½":
                result = "1/2-1/2"
            if score_from_result(result) is None:
                continue

            exists = q("""
                SELECT id FROM matches WHERE tournament_id=? AND round_id=? AND white_user_id=? AND black_user_id=? AND result=?
            """, (tid, rid, w_id, b_id, result), one=True)
            if exists:
                continue

            mid = add_match(tid, rid, w_id, b_id, manual=1, status="finished", result=result, imported=1)
            link = str(row.get("link_chesscom","")).strip() if "link_chesscom" in row else ""
            exec_sql("UPDATE matches SET chesscom_url=?, detected_at=?, locked=1 WHERE id=?", (link, str(row.get("fecha")), mid))
            apply_elo(mid, w_id, b_id, score_from_result(result))
            imported += 1
    return imported

# =========================
# UI
# =========================

init_db()

if "user" not in st.session_state:
    st.session_state.user = None

st.title("♟️ Torneos de Ajedrez — V4")

if not st.session_state.user:
    tab_login, tab_reg = st.tabs(["Ingresar", "Registrarme"])
    with tab_login:
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
        nu = st.text_input("Usuario para la app", key="reg_user")
        nd = st.text_input("Nombre visible", key="reg_name")
        nc = st.text_input("Usuario de Chess.com", key="reg_chess")
        np = st.text_input("Contraseña", type="password", key="reg_pass")
        if st.button("Registrarme"):
            try:
                if not nu or not np or not nc:
                    st.warning("Completá usuario, contraseña y usuario Chess.com.")
                else:
                    create_user(nu, np, nc, nd)
                    st.success("Usuario creado o vinculado con historial previo. Ahora ingresá.")
            except Exception as e:
                st.error(f"No pude crear/vincular el usuario: {e}")
    st.stop()

user = get_user(st.session_state.user["id"])
st.sidebar.success(f"{user['display_name']} | {user['role']} | ELO {user['elo']}")
if user["avatar_url"]:
    st.sidebar.image(user["avatar_url"], width=100)
if st.sidebar.button("Cerrar sesión"):
    st.session_state.user = None
    st.rerun()

menus = ["Torneos", "Crear torneo", "Mi perfil", "Ranking general"]
if is_admin(user):
    menus += ["Importar historial", "Admin usuarios"]
menu = st.sidebar.radio("Menú", menus)

if menu == "Crear torneo":
    st.header("Crear torneo")
    if not is_admin(user):
        st.warning("Solo admin o superadmin puede crear torneos.")
    else:
        name = st.text_input("Nombre del torneo")
        desc = st.text_area("Descripción")
        tournament_type = st.selectbox("Tipo de torneo", ["swiss", "free_fixture"], format_func=lambda x: {"swiss":"Suizo por rondas", "free_fixture":"Fixture libre"}[x])
        c1, c2, c3 = st.columns(3)
        rules = "chess" if c1.selectbox("Modalidad", ["Ajedrez normal", "Chess960"]) == "Ajedrez normal" else "chess960"
        tc = c2.selectbox("Clase", ["blitz", "rapid", "bullet", "daily"])
        tcontrol = c3.text_input("Ritmo exacto", value="300", help="300=5+0, 600=10+0, 180+2=3+2")
        rated = {"Cualquiera":"any", "Solo rated":"rated", "Solo casual":"casual"}[st.selectbox("Rated/Casual", ["Cualquiera", "Solo rated", "Solo casual"])]
        strict_colors = st.checkbox("Respetar colores exactos del fixture", value=True)

        swiss_rounds = 1
        free_games = 5
        pairing_round1 = "random"
        pairing_free = "random"
        windows = []

        if tournament_type == "swiss":
            swiss_rounds = st.number_input("Cantidad de rondas suizas", min_value=1, max_value=20, value=5)
            pairing_round1 = st.selectbox("Ronda 1", ["manual", "random", "elo"], format_func=lambda x: {"manual":"Manual", "random":"Aleatoria", "elo":"Por ELO"}[x])
            st.subheader("Fechas por ronda")
            for rn in range(1, int(swiss_rounds)+1):
                with st.expander(f"Ronda {rn}", expanded=(rn==1)):
                    c1, c2, c3, c4 = st.columns(4)
                    sd = c1.date_input(f"Inicio fecha R{rn}", value=dt.date.today(), key=f"sd_{rn}")
                    stime = c2.time_input(f"Inicio hora R{rn}", value=dt.time(0,0), key=f"st_{rn}")
                    ed = c3.date_input(f"Fin fecha R{rn}", value=dt.date.today()+dt.timedelta(days=7), key=f"ed_{rn}")
                    etime = c4.time_input(f"Fin hora R{rn}", value=dt.time(23,59), key=f"et_{rn}")
                    windows.append((rn, dt.datetime.combine(sd, stime).isoformat(timespec="seconds"), dt.datetime.combine(ed, etime).isoformat(timespec="seconds")))
        else:
            free_games = st.number_input("Cantidad de rivales/partidas por jugador", min_value=1, max_value=30, value=5)
            pairing_free = st.selectbox("Cruces del fixture libre", ["random", "elo", "manual"], format_func=lambda x: {"random":"Aleatorio", "elo":"Por ELO cercano", "manual":"Manual luego"}[x])
            st.subheader("Rango válido para detectar partidas")
            c1, c2, c3, c4 = st.columns(4)
            sd = c1.date_input("Inicio fecha", value=dt.date.today())
            stime = c2.time_input("Inicio hora", value=dt.time(0,0))
            ed = c3.date_input("Fin fecha", value=dt.date.today()+dt.timedelta(days=14))
            etime = c4.time_input("Fin hora", value=dt.time(23,59))
            windows.append((1, dt.datetime.combine(sd, stime).isoformat(timespec="seconds"), dt.datetime.combine(ed, etime).isoformat(timespec="seconds")))

        playoff_enabled = st.checkbox("Activar playoffs/copas", value=True)
        cups = []
        if playoff_enabled:
            cup_count = st.number_input("Cantidad de copas", min_value=1, max_value=10, value=3)
            next_rank = 1
            default_names = ["Copa Oro", "Copa Plata", "Copa Bronce", "Copa Cobre"]
            for i in range(int(cup_count)):
                c1, c2, c3 = st.columns(3)
                cup_name = c1.text_input(f"Nombre copa {i+1}", value=default_names[i] if i < len(default_names) else f"Copa {i+1}")
                size = c2.number_input(f"Clasificados copa {i+1}", min_value=2, max_value=64, value=8, step=2)
                c3.write(f"Ranks: {next_rank} al {next_rank+int(size)-1}")
                cups.append((cup_name, next_rank, next_rank+int(size)-1))
                next_rank += int(size)

        if st.button("Crear torneo"):
            if not name:
                st.warning("Poné un nombre.")
            else:
                create_tournament({
                    "name": name, "desc": desc, "type": tournament_type, "rules": rules, "time_class": tc,
                    "time_control": tcontrol, "rated_filter": rated, "swiss_rounds": int(swiss_rounds),
                    "free_games": int(free_games), "pairing_round1": pairing_round1, "pairing_free": pairing_free,
                    "strict_colors": strict_colors, "playoff": playoff_enabled, "created_by": user["id"]
                }, cups, windows)
                st.success("Torneo creado.")
                st.rerun()

elif menu == "Torneos":
    st.header("Torneos")
    tournaments = q("SELECT * FROM tournaments ORDER BY id DESC")
    for t in tournaments:
        with st.expander(f"{t['name']} — {t['status']} — {t['tournament_type']} — {t['time_control']}", expanded=True):
            st.write(t["description"] or "")
            regs = q("""
                SELECT u.display_name, u.chesscom_user, u.elo FROM registrations r
                JOIN users u ON u.id=r.user_id WHERE r.tournament_id=? ORDER BY u.elo DESC
            """, (t["id"],))
            already = q("SELECT * FROM registrations WHERE tournament_id=? AND user_id=?", (t["id"], user["id"]), one=True)
            c1,c2,c3,c4 = st.columns(4)
            c1.metric("Inscriptos", len(regs)); c2.metric("Tipo", "Fixture libre" if t["tournament_type"] == "free_fixture" else "Suizo")
            c3.metric("Colores estrictos", "Sí" if t["strict_colors"] else "No"); c4.metric("Ritmo", t["time_control"])

            if t["status"] == "open":
                if already:
                    st.success("Ya estás inscripto.")
                elif st.button(f"Inscribirme en {t['name']}", key=f"reg_{t['id']}"):
                    exec_sql("INSERT INTO registrations(tournament_id,user_id) VALUES(?,?)", (t["id"], user["id"]))
                    st.rerun()

            st.write("**Inscriptos:**")
            st.dataframe([dict(r) for r in regs], use_container_width=True)

            if is_admin(user):
                a,b,c = st.columns(3)
                if t["tournament_type"] == "free_fixture":
                    if a.button("Generar fixture libre", key=f"free_{t['id']}"):
                        try:
                            generate_free_fixture(t["id"]); st.success("Fixture generado."); st.rerun()
                        except Exception as e: st.error(e)
                else:
                    if a.button("Generar ronda 1 auto", key=f"gen_{t['id']}"):
                        try:
                            generate_round_one_auto(t["id"]); st.success("Ronda 1 generada."); st.rerun()
                        except Exception as e: st.error(e)
                if b.button("Buscar resultados Chess.com", key=f"scan_{t['id']}"):
                    with st.spinner("Buscando partidas válidas..."):
                        found, errors = scan_tournament(t["id"])
                    st.success(f"Detectadas: {len(found)}") if found else st.info("No se detectaron partidas nuevas.")
                    for e in errors: st.warning(e)
                    st.rerun()
                if c.button("Cancelar torneo", key=f"del_{t['id']}"):
                    exec_sql("UPDATE tournaments SET status='cancelled' WHERE id=?", (t["id"],))
                    st.warning("Torneo cancelado."); st.rerun()

            st.write("**Rangos válidos:**")
            wins = q("SELECT round_number, start_datetime, end_datetime FROM round_windows WHERE tournament_id=? ORDER BY round_number", (t["id"],))
            st.dataframe([dict(w) for w in wins], use_container_width=True)

            st.write("**Cruces:**")
            matches = q("""
                SELECT m.*, IFNULL(r.number,1) round_number,
                       wu.display_name white_name, wu.chesscom_user white_chess,
                       bu.display_name black_name, bu.chesscom_user black_chess
                FROM matches m LEFT JOIN rounds r ON r.id=m.round_id
                LEFT JOIN users wu ON wu.id=m.white_user_id
                LEFT JOIN users bu ON bu.id=m.black_user_id
                WHERE m.tournament_id=? ORDER BY round_number, m.id
            """, (t["id"],))
            st.dataframe([{
                "Fecha/Ronda": m["round_number"], "Blancas": m["white_name"], "Chess blancas": m["white_chess"],
                "Negras": m["black_name"] or "BYE", "Chess negras": m["black_chess"] or "",
                "Estado": m["status"], "Bloqueada": "Sí" if m["locked"] else "No", "Importada": "Sí" if m["imported"] else "No",
                "Resultado": m["result"] or "", "Link": m["chesscom_url"] or ""
            } for m in matches], use_container_width=True)

            st.write("**Tabla:**")
            visible = [{k:v for k,v in r.items() if k != "User ID"} for r in standings(t["id"])]
            st.dataframe(visible, use_container_width=True)

elif menu == "Mi perfil":
    st.header("Mi perfil")
    stats = player_stats(user["id"])
    c1, c2 = st.columns([1,3])
    if user["avatar_url"]:
        c1.image(user["avatar_url"], width=180)
    c2.metric("ELO interno", user["elo"])
    c2.write(f"**Nombre:** {user['display_name']}")
    c2.write(f"**Chess.com:** {user['chesscom_user']}")
    c2.write(f"**Rol:** {user['role']}")
    if st.button("Sincronizar perfil con Chess.com"):
        ok, msg = sync_chess_profile(user["id"], user["chesscom_user"])
        st.success(msg) if ok else st.error(msg); st.rerun()

    st.subheader("Estadísticas")
    c1,c2,c3,c4,c5,c6,c7 = st.columns(7)
    c1.metric("Ranking", stats["Ranking"]); c2.metric("Torneos", stats["Torneos"]); c3.metric("PJ", stats["PJ"])
    c4.metric("G", stats["G"]); c5.metric("E", stats["E"]); c6.metric("P", stats["P"]); c7.metric("Rend.", f"{stats['Rendimiento %']}%")

    matches = q("""
        SELECT m.*, t.name tournament_name, IFNULL(r.number,1) round_number,
               wu.display_name white_name, bu.display_name black_name
        FROM matches m JOIN tournaments t ON t.id=m.tournament_id
        LEFT JOIN rounds r ON r.id=m.round_id
        LEFT JOIN users wu ON wu.id=m.white_user_id
        LEFT JOIN users bu ON bu.id=m.black_user_id
        WHERE m.status='finished' AND (m.white_user_id=? OR m.black_user_id=?)
        ORDER BY COALESCE(m.detected_at,m.created_at) DESC
    """, (user["id"], user["id"]))
    st.subheader("Historial de partidas")
    st.dataframe([{
        "Torneo": m["tournament_name"], "Fecha/Ronda": m["round_number"], "Blancas": m["white_name"],
        "Negras": m["black_name"] or "BYE", "Resultado": m["result"], "Importada": "Sí" if m["imported"] else "No",
        "Link": m["chesscom_url"] or ""
    } for m in matches], use_container_width=True)

    elo = q("SELECT old_elo,new_elo,delta,created_at FROM elo_history WHERE user_id=? ORDER BY id DESC", (user["id"],))
    st.subheader("Historial ELO")
    st.dataframe([dict(e) for e in elo], use_container_width=True)

elif menu == "Ranking general":
    st.header("Ranking general")
    users = q("SELECT id,display_name,chesscom_user,elo,role,username FROM users ORDER BY elo DESC")
    st.dataframe([{
        "Nombre": u["display_name"], "Chess.com": u["chesscom_user"], "ELO": u["elo"], "Rol": u["role"],
        "Perfil reclamado": "No" if str(u["username"]).startswith("hist_") else "Sí"
    } for u in users], use_container_width=True)

elif menu == "Importar historial":
    st.header("Importar torneos anteriores")
    st.write("Subí un CSV con columnas: torneo, ronda, fecha, blancas_chesscom, negras_chesscom, resultado, link_chesscom")
    ejemplo = pd.DataFrame([
        {"torneo":"Torneo Mayo 2026","ronda":1,"fecha":"2026-05-10 20:00","blancas_chesscom":"matiasbulacio","negras_chesscom":"juan123","resultado":"1-0","link_chesscom":""}
    ])
    st.download_button("Descargar plantilla CSV", ejemplo.to_csv(index=False).encode("utf-8"), "plantilla_historial.csv", "text/csv")
    file = st.file_uploader("CSV histórico", type=["csv"])
    if file:
        df = pd.read_csv(file)
        st.dataframe(df, use_container_width=True)
        if st.button("Importar historial"):
            try:
                n = import_history_csv(df, user["id"])
                st.success(f"Partidas importadas: {n}. Jugadores históricos creados/vinculados automáticamente.")
            except Exception as e:
                st.error(e)

elif menu == "Admin usuarios":
    st.header("Administrar usuarios")
    users = q("SELECT * FROM users ORDER BY role DESC, elo DESC, display_name")
    st.dataframe([{
        "ID": u["id"], "Nombre": u["display_name"], "Usuario": u["username"], "Chess.com": u["chesscom_user"],
        "Rol": u["role"], "ELO": u["elo"], "Reclamado": "No" if str(u["username"]).startswith("hist_") else "Sí"
    } for u in users], use_container_width=True)

    labels = {f"{u['display_name']} ({u['username']})": u["id"] for u in users}
    st.subheader("Cambiar rol")
    selected = st.selectbox("Usuario", list(labels.keys()))
    target = get_user(labels[selected])
    allowed_roles = ["player", "admin", "superadmin"] if is_superadmin(user) else ["player", "admin"]
    new_role = st.selectbox("Nuevo rol", allowed_roles, index=allowed_roles.index(target["role"]) if target["role"] in allowed_roles else 0)
    if st.button("Actualizar rol"):
        if target["id"] == user["id"] and user["role"] == "superadmin" and new_role != "superadmin":
            st.error("No te podés quitar superadmin a vos mismo.")
        elif target["role"] == "superadmin" and not is_superadmin(user):
            st.error("Solo un superadmin puede modificar otro superadmin.")
        else:
            exec_sql("UPDATE users SET role=? WHERE id=?", (new_role, target["id"]))
            st.success("Rol actualizado."); st.rerun()

    st.subheader("Cambiar ELO")
    elo_user = st.selectbox("Usuario ELO", list(labels.keys()), key="elo_user")
    elo_target = get_user(labels[elo_user])
    new_elo = st.number_input("Nuevo ELO", value=int(elo_target["elo"]), step=10)
    if st.button("Actualizar ELO"):
        exec_sql("UPDATE users SET elo=? WHERE id=?", (int(new_elo), elo_target["id"]))
        st.success("ELO actualizado."); st.rerun()