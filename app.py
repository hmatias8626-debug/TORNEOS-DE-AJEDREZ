import datetime as dt
import hashlib
import io
import sqlite3
from pathlib import Path

import pandas as pd
import requests
import streamlit as st


APP_TITLE = "Torneos de Ajedrez"
DB_PATH = Path("torneos_ajedrez.db")
API_BASE = "https://api.chess.com/pub"
DEFAULT_PASSWORD = "12345"
HEADERS = {"User-Agent": "torneos-ajedrez-v7-estable/1.0"}


st.set_page_config(page_title=APP_TITLE, layout="wide")


# =========================================================
# DB
# =========================================================

def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def q(sql, params=(), one=False):
    conn = db()
    cur = conn.cursor()
    cur.execute(sql, params)
    rows = cur.fetchall()
    conn.close()
    if one:
        return rows[0] if rows else None
    return rows


def exec_sql(sql, params=()):
    conn = db()
    cur = conn.cursor()
    cur.execute(sql, params)
    conn.commit()
    last_id = cur.lastrowid
    conn.close()
    return last_id


def column_exists(table, column):
    conn = db()
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    cols = [r[1] for r in cur.fetchall()]
    conn.close()
    return column in cols


def ensure_column(table, column, definition):
    if not column_exists(table, column):
        conn = db()
        cur = conn.cursor()
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        conn.commit()
        conn.close()


def init_db():
    conn = db()
    cur = conn.cursor()

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
        status TEXT DEFAULT 'open',
        tournament_type TEXT DEFAULT 'fixture',
        rules TEXT DEFAULT 'chess',
        time_class TEXT DEFAULT 'blitz',
        time_control TEXT DEFAULT '600',
        rated_filter TEXT DEFAULT 'any',
        strict_colors INTEGER DEFAULT 1,
        playoff_rules TEXT DEFAULT 'chess',
        playoff_time_class TEXT DEFAULT 'blitz',
        playoff_time_control TEXT DEFAULT '300',
        playoff_rated_filter TEXT DEFAULT 'any',
        created_by INTEGER,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS rounds (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tournament_id INTEGER,
        number INTEGER,
        start_datetime TEXT,
        end_datetime TEXT,
        status TEXT DEFAULT 'active'
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
    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        action TEXT,
        detail TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)

    conn.commit()
    conn.close()

    # Safe migrations if previous DB exists
    migrations = [
        ("users", "avatar_url", "TEXT"),
        ("users", "account_status", "TEXT DEFAULT 'pending'"),
        ("users", "must_change_password", "INTEGER DEFAULT 1"),
        ("tournaments", "playoff_rules", "TEXT DEFAULT 'chess'"),
        ("tournaments", "playoff_time_class", "TEXT DEFAULT 'blitz'"),
        ("tournaments", "playoff_time_control", "TEXT DEFAULT '300'"),
        ("tournaments", "playoff_rated_filter", "TEXT DEFAULT 'any'"),
        ("registrations", "status", "TEXT DEFAULT 'active'"),
        ("registrations", "wo_count", "INTEGER DEFAULT 0"),
        ("matches", "result_type", "TEXT DEFAULT 'normal'"),
        ("matches", "locked", "INTEGER DEFAULT 0"),
        ("matches", "detected_at", "TEXT"),
    ]
    for table, col, definition in migrations:
        ensure_column(table, col, definition)


# =========================================================
# Helpers / users
# =========================================================

def norm(value):
    return (str(value) if value is not None else "").strip().lower()


def hash_password(password):
    return hashlib.sha256(str(password).encode("utf-8")).hexdigest()


def is_staff(user):
    return user and user["role"] in ("superadmin", "admin", "moderator")


def is_admin(user):
    return user and user["role"] in ("superadmin", "admin")


def is_superadmin(user):
    return user and user["role"] == "superadmin"


def get_user(user_id):
    row = q("SELECT * FROM users WHERE id=?", (user_id,), one=True)
    return dict(row) if row else None


def audit(user_id, action, detail=""):
    exec_sql(
        "INSERT INTO audit_log(user_id, action, detail) VALUES(?,?,?)",
        (user_id, action, detail),
    )


def chess_profile(username):
    try:
        r = requests.get(f"{API_BASE}/player/{norm(username)}", headers=HEADERS, timeout=20)
        if r.status_code == 200:
            return r.json()
    except Exception:
        return None
    return None


def sync_avatar(user_id, chesscom_user):
    profile = chess_profile(chesscom_user)
    if profile and profile.get("avatar"):
        exec_sql("UPDATE users SET avatar_url=? WHERE id=?", (profile.get("avatar"), user_id))


def get_or_create_player(chesscom_user, display_name=None):
    chesscom_user = norm(chesscom_user)
    if not chesscom_user:
        raise ValueError("Usuario Chess.com vacío")

    row = q("SELECT * FROM users WHERE chesscom_user=?", (chesscom_user,), one=True)
    if row:
        return row["id"], False

    username = chesscom_user
    if q("SELECT id FROM users WHERE username=?", (username,), one=True):
        username = f"{chesscom_user}_{dt.datetime.now().strftime('%H%M%S')}"

    user_id = exec_sql("""
        INSERT INTO users(username, password_hash, chesscom_user, display_name, role, elo, account_status, must_change_password)
        VALUES(?,?,?,?,?,?,?,?)
    """, (
        username,
        hash_password(DEFAULT_PASSWORD),
        chesscom_user,
        display_name or chesscom_user,
        "player",
        1200,
        "pending",
        1,
    ))
    sync_avatar(user_id, chesscom_user)
    return user_id, True


def create_or_claim_user(username, password, chesscom_user, display_name):
    username = norm(username) or norm(chesscom_user)
    chesscom_user = norm(chesscom_user)
    display_name = display_name.strip() if display_name else username

    count = q("SELECT COUNT(*) c FROM users", one=True)["c"]
    existing = q("SELECT * FROM users WHERE chesscom_user=?", (chesscom_user,), one=True)

    if existing:
        exec_sql("""
            UPDATE users
            SET username=?, password_hash=?, display_name=?, account_status='active', must_change_password=0
            WHERE id=?
        """, (username, hash_password(password), display_name, existing["id"]))
        sync_avatar(existing["id"], chesscom_user)
        return existing["id"]

    role = "superadmin" if count == 0 else "player"
    user_id = exec_sql("""
        INSERT INTO users(username, password_hash, chesscom_user, display_name, role, elo, account_status, must_change_password)
        VALUES(?,?,?,?,?,?,?,?)
    """, (
        username,
        hash_password(password),
        chesscom_user,
        display_name,
        role,
        1200,
        "active",
        0,
    ))
    sync_avatar(user_id, chesscom_user)
    return user_id


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


def update_password(user_id, new_password, must_change=0):
    exec_sql("""
        UPDATE users
        SET password_hash=?, must_change_password=?, account_status='active'
        WHERE id=?
    """, (hash_password(new_password), must_change, user_id))


# =========================================================
# CSV
# =========================================================

def read_uploaded_csv(file):
    raw = file.getvalue()
    encodings = ["utf-8", "utf-8-sig", "latin-1", "cp1252"]
    separators = [",", ";", "\t"]

    last_error = None
    for enc in encodings:
        for sep in separators:
            try:
                text = raw.decode(enc)
                df = pd.read_csv(io.StringIO(text), sep=sep)
                if len(df.columns) > 1:
                    df.columns = [str(c).strip() for c in df.columns]
                    return df
            except Exception as exc:
                last_error = exc

    raise Exception(f"No pude leer el CSV. Guardalo como CSV UTF-8 o CSV con punto y coma. Último error: {last_error}")


# =========================================================
# Chess.com
# =========================================================

def chess_archives(username):
    try:
        r = requests.get(f"{API_BASE}/player/{norm(username)}/games/archives", headers=HEADERS, timeout=20)
        if r.status_code != 200:
            return []
        return r.json().get("archives", [])
    except Exception:
        return []


def month_keys_between(start_dt, end_dt):
    keys = set()
    cur = dt.datetime(start_dt.year, start_dt.month, 1)
    while cur <= end_dt:
        keys.add(f"{cur.year}/{cur.month:02d}")
        if cur.month == 12:
            cur = dt.datetime(cur.year + 1, 1, 1)
        else:
            cur = dt.datetime(cur.year, cur.month + 1, 1)
    return keys


def chess_games_between(username, start_dt, end_dt):
    games = []
    keys = month_keys_between(start_dt, end_dt)
    for url in chess_archives(username):
        if any(key in url for key in keys):
            try:
                r = requests.get(url, headers=HEADERS, timeout=20)
                if r.status_code == 200:
                    games.extend(r.json().get("games", []))
            except Exception:
                pass
    return games


def parse_ts(timestamp):
    try:
        return dt.datetime.fromtimestamp(int(timestamp))
    except Exception:
        return None


def validate_game(game, white_user, black_user, tournament, start_dt, end_dt):
    white = norm(game.get("white", {}).get("username"))
    black = norm(game.get("black", {}).get("username"))

    if tournament["strict_colors"]:
        if white != norm(white_user) or black != norm(black_user):
            return False, "colores/usuarios no coinciden"
    else:
        if {white, black} != {norm(white_user), norm(black_user)}:
            return False, "usuarios no coinciden"

    if game.get("rules") != tournament["rules"]:
        return False, f"modalidad distinta: {game.get('rules')}"

    if game.get("time_class") != tournament["time_class"]:
        return False, f"clase distinta: {game.get('time_class')}"

    if str(game.get("time_control")) != str(tournament["time_control"]):
        return False, f"ritmo distinto: {game.get('time_control')}"

    if tournament["rated_filter"] == "rated" and not game.get("rated"):
        return False, "no es rated"

    if tournament["rated_filter"] == "casual" and game.get("rated"):
        return False, "no es casual"

    started = parse_ts(game.get("start_time") or game.get("end_time"))
    if not started:
        return False, "sin fecha detectable"

    if started < start_dt:
        return False, "anterior al rango"
    if started > end_dt:
        return False, "posterior al rango"

    return True, "ok"


def score_for_white(game):
    wr = game.get("white", {}).get("result", "")
    br = game.get("black", {}).get("result", "")
    draws = {"agreed", "repetition", "stalemate", "50move", "insufficient", "timevsinsufficient"}

    if wr == "win":
        return 1.0
    if br == "win":
        return 0.0
    if wr in draws or br in draws:
        return 0.5
    return None


def result_label(score):
    if score == 1.0:
        return "1-0"
    if score == 0.0:
        return "0-1"
    if score == 0.5:
        return "1/2-1/2"
    return ""


# =========================================================
# ELO / standings
# =========================================================

def expected_score(ra, rb):
    return 1 / (1 + 10 ** ((rb - ra) / 400))


def elo_delta(ra, rb, score, k=32):
    return round(k * (score - expected_score(ra, rb)))


def apply_elo(match_id, white_id, black_id, white_score):
    white_user = get_user(white_id)
    black_user = get_user(black_id)
    if not white_user or not black_user:
        return

    black_score = 1 - white_score if white_score in (0, 1) else 0.5
    dw = elo_delta(white_user["elo"], black_user["elo"], white_score)
    db = elo_delta(black_user["elo"], white_user["elo"], black_score)

    exec_sql("UPDATE users SET elo=? WHERE id=?", (white_user["elo"] + dw, white_id))
    exec_sql("UPDATE users SET elo=? WHERE id=?", (black_user["elo"] + db, black_id))
    exec_sql("INSERT INTO elo_history(match_id,user_id,old_elo,new_elo,delta) VALUES(?,?,?,?,?)",
             (match_id, white_id, white_user["elo"], white_user["elo"] + dw, dw))
    exec_sql("INSERT INTO elo_history(match_id,user_id,old_elo,new_elo,delta) VALUES(?,?,?,?,?)",
             (match_id, black_id, black_user["elo"], black_user["elo"] + db, db))


def standings(tournament_id):
    regs = q("""
        SELECT u.id, u.display_name, u.chesscom_user, u.elo, r.status, r.wo_count
        FROM registrations r
        JOIN users u ON u.id=r.user_id
        WHERE r.tournament_id=? AND r.status!='removed'
    """, (tournament_id,))

    table = []
    for user in regs:
        matches = q("""
            SELECT * FROM matches
            WHERE tournament_id=? AND status='finished'
            AND (white_user_id=? OR black_user_id=?)
        """, (tournament_id, user["id"], user["id"]))

        pts = wins = draws = losses = wo = 0
        for match in matches:
            if match["result_type"] == "wo":
                wo += 1
                continue

            if match["white_user_id"] == user["id"]:
                if match["result"] == "1-0":
                    pts += 1
                    wins += 1
                elif match["result"] == "0-1":
                    losses += 1
                elif match["result"] == "1/2-1/2":
                    pts += 0.5
                    draws += 1
            else:
                if match["result"] == "0-1":
                    pts += 1
                    wins += 1
                elif match["result"] == "1-0":
                    losses += 1
                elif match["result"] == "1/2-1/2":
                    pts += 0.5
                    draws += 1

        table.append({
            "Jugador": user["display_name"],
            "Chess.com": user["chesscom_user"],
            "ELO": user["elo"],
            "PJ": len(matches),
            "G": wins,
            "E": draws,
            "P": losses,
            "WO": user["wo_count"],
            "Estado": user["status"],
            "Puntos": pts,
        })

    table.sort(key=lambda row: (row["Estado"] != "disqualified", row["Puntos"], row["ELO"], row["G"]), reverse=True)
    return table


def player_stats(user_id):
    matches = q("""
        SELECT * FROM matches
        WHERE status='finished' AND (white_user_id=? OR black_user_id=?)
    """, (user_id, user_id))

    pts = wins = draws = losses = wo = 0
    for match in matches:
        if match["result_type"] == "wo":
            wo += 1
            continue
        if match["white_user_id"] == user_id:
            if match["result"] == "1-0":
                pts += 1
                wins += 1
            elif match["result"] == "0-1":
                losses += 1
            elif match["result"] == "1/2-1/2":
                pts += 0.5
                draws += 1
        else:
            if match["result"] == "0-1":
                pts += 1
                wins += 1
            elif match["result"] == "1-0":
                losses += 1
            elif match["result"] == "1/2-1/2":
                pts += 0.5
                draws += 1

    perf = round((pts / len(matches)) * 100, 1) if matches else 0
    return {"PJ": len(matches), "G": wins, "E": draws, "P": losses, "WO": wo, "Puntos": pts, "Rendimiento": perf}


# =========================================================
# Tournament actions
# =========================================================

def register_player(tournament_id, user_id):
    exec_sql("""
        INSERT OR IGNORE INTO registrations(tournament_id,user_id,status,wo_count)
        VALUES(?,?,?,?)
    """, (tournament_id, user_id, "active", 0))


def import_fixture_csv(df, created_by, rules, time_class, time_control, rated_filter, strict_colors):
    required = {"torneo", "ronda", "fecha_inicio", "fecha_fin", "blancas_chesscom", "negras_chesscom"}
    missing = required - set(df.columns)
    if missing:
        raise Exception(f"Faltan columnas: {', '.join(sorted(missing))}")

    created_matches = 0
    created_players = 0

    for tournament_name, group in df.groupby("torneo"):
        tournament_name = str(tournament_name).strip()
        if not tournament_name:
            continue

        tournament_id = exec_sql("""
            INSERT INTO tournaments(name,description,status,tournament_type,rules,time_class,time_control,rated_filter,strict_colors,created_by)
            VALUES(?,?,?,?,?,?,?,?,?,?)
        """, (
            tournament_name,
            "Fixture importado para detección automática",
            "playing",
            "fixture_importado",
            rules,
            time_class,
            str(time_control),
            rated_filter,
            1 if strict_colors else 0,
            created_by,
        ))

        for round_number, round_df in group.groupby("ronda"):
            round_number = int(round_number)
            start_value = pd.to_datetime(round_df["fecha_inicio"], errors="coerce").min()
            end_value = pd.to_datetime(round_df["fecha_fin"], errors="coerce").max()

            if pd.isna(start_value) or pd.isna(end_value):
                continue

            round_id = exec_sql("""
                INSERT INTO rounds(tournament_id,number,start_datetime,end_datetime,status)
                VALUES(?,?,?,?,?)
            """, (
                tournament_id,
                round_number,
                start_value.to_pydatetime().isoformat(timespec="seconds"),
                end_value.to_pydatetime().isoformat(timespec="seconds"),
                "active",
            ))

            for _, row in round_df.iterrows():
                white_id, white_created = get_or_create_player(row["blancas_chesscom"])
                black_id, black_created = get_or_create_player(row["negras_chesscom"])
                created_players += int(white_created) + int(black_created)

                register_player(tournament_id, white_id)
                register_player(tournament_id, black_id)

                exec_sql("""
                    INSERT INTO matches(tournament_id,round_id,white_user_id,black_user_id,status,locked)
                    VALUES(?,?,?,?,?,?)
                """, (tournament_id, round_id, white_id, black_id, "pending", 0))
                created_matches += 1

    audit(created_by, "import_fixture", f"matches={created_matches}, players={created_players}")
    return created_matches, created_players


def scan_tournament(tournament_id):
    tournament = q("SELECT * FROM tournaments WHERE id=?", (tournament_id,), one=True)
    if not tournament:
        return 0, [{"Cruce": "-", "Estado": "torneo no encontrado"}]

    pending_matches = q("""
        SELECT m.*, r.number, r.start_datetime, r.end_datetime,
               wu.chesscom_user white_chess,
               bu.chesscom_user black_chess
        FROM matches m
        JOIN rounds r ON r.id=m.round_id
        JOIN users wu ON wu.id=m.white_user_id
        JOIN users bu ON bu.id=m.black_user_id
        WHERE m.tournament_id=? AND m.status='pending' AND m.locked=0
    """, (tournament_id,))

    found = 0
    debug = []

    for match in pending_matches:
        start_dt = dt.datetime.fromisoformat(match["start_datetime"])
        end_dt = dt.datetime.fromisoformat(match["end_datetime"])
        games = chess_games_between(match["white_chess"], start_dt, end_dt)
        final_reason = "sin partidas del jugador blanco en el rango"

        for game in games:
            ok, reason = validate_game(
                game,
                match["white_chess"],
                match["black_chess"],
                tournament,
                start_dt,
                end_dt,
            )
            if not ok:
                final_reason = reason
                continue

            score = score_for_white(game)
            if score is None:
                final_reason = "partida encontrada sin resultado interpretable"
                continue

            result = result_label(score)
            exec_sql("""
                UPDATE matches
                SET status='finished', result=?, result_type='normal', chesscom_url=?, game_uuid=?, locked=1, detected_at=?
                WHERE id=?
            """, (
                result,
                game.get("url"),
                game.get("uuid"),
                dt.datetime.now().isoformat(timespec="seconds"),
                match["id"],
            ))
            apply_elo(match["id"], match["white_user_id"], match["black_user_id"], score)
            found += 1
            final_reason = "detectada"
            break

        debug.append({"Cruce": f"{match['white_chess']} vs {match['black_chess']}", "Estado": final_reason})

    return found, debug


def apply_wo_expired(tournament_id):
    now = dt.datetime.now()
    matches = q("""
        SELECT m.*, r.end_datetime
        FROM matches m
        JOIN rounds r ON r.id=m.round_id
        WHERE m.tournament_id=? AND m.status='pending' AND m.locked=0
    """, (tournament_id,))

    applied = 0
    for match in matches:
        end_dt = dt.datetime.fromisoformat(match["end_datetime"])
        if now <= end_dt:
            continue

        exec_sql("""
            UPDATE matches
            SET status='finished', result='0-0 WO', result_type='wo', locked=1, detected_at=?
            WHERE id=?
        """, (now.isoformat(timespec="seconds"), match["id"]))

        for user_id in [match["white_user_id"], match["black_user_id"]]:
            exec_sql("""
                UPDATE registrations
                SET wo_count=IFNULL(wo_count,0)+1
                WHERE tournament_id=? AND user_id=?
            """, (tournament_id, user_id))

            reg = q("SELECT wo_count FROM registrations WHERE tournament_id=? AND user_id=?", (tournament_id, user_id), one=True)
            if reg and reg["wo_count"] >= 2:
                exec_sql("""
                    UPDATE registrations
                    SET status='disqualified'
                    WHERE tournament_id=? AND user_id=?
                """, (tournament_id, user_id))

        applied += 1

    return applied


# =========================================================
# UI
# =========================================================

init_db()

if "user" not in st.session_state:
    st.session_state.user = None

st.markdown("""
<style>
.block-container {max-width: 1180px;}
.login-box {max-width: 440px; margin: 0 auto;}
.login-box [data-testid="stTextInput"] {max-width: 440px;}
.login-box .stButton button {width: 180px;}
.small-input [data-testid="stTextInput"] {max-width: 260px;}
</style>
""", unsafe_allow_html=True)

st.title("♟️ Torneos de Ajedrez — V7 estable")

if not st.session_state.user:
    st.markdown('<div class="login-box">', unsafe_allow_html=True)
    login_tab, register_tab = st.tabs(["Ingresar", "Crear cuenta"])

    with login_tab:
        st.caption("Si el admin ya cargó tu perfil, entrá con tu usuario de Chess.com y contraseña 12345.")
        username = st.text_input("Usuario Chess.com", key="login_username")
        password = st.text_input("Contraseña", type="password", key="login_password")
        if st.button("Ingresar"):
            logged_user, error = login(username, password)
            if logged_user:
                st.session_state.user = logged_user
                st.rerun()
            else:
                st.error(error)

    with register_tab:
        chess_user = st.text_input("Usuario Chess.com", key="reg_chess")
        display_name = st.text_input("Nombre visible", key="reg_name")
        new_password = st.text_input("Contraseña", type="password", key="reg_password")
        if st.button("Crear/Reclamar cuenta"):
            if not chess_user or not new_password:
                st.warning("Completá usuario Chess.com y contraseña.")
            else:
                create_or_claim_user(chess_user, new_password, chess_user, display_name or chess_user)
                st.success("Cuenta creada o reclamada. Ahora ingresá.")

    st.markdown("</div>", unsafe_allow_html=True)
    st.stop()

current_user = get_user(st.session_state.user["id"])

st.sidebar.success(f"{current_user['display_name']} | {current_user['role']} | ELO {current_user['elo']}")
if current_user.get("avatar_url"):
    st.sidebar.image(current_user["avatar_url"], width=100)
if st.sidebar.button("Cerrar sesión"):
    st.session_state.user = None
    st.rerun()

if current_user["must_change_password"]:
    st.warning("Tu contraseña es temporal. Cambiala para continuar.")
    p1 = st.text_input("Nueva contraseña", type="password")
    p2 = st.text_input("Repetir nueva contraseña", type="password")
    if st.button("Cambiar contraseña"):
        if p1 and p1 == p2:
            update_password(current_user["id"], p1, must_change=0)
            st.success("Contraseña actualizada.")
            st.rerun()
        else:
            st.error("Las contraseñas no coinciden.")
    st.stop()

menu = ["Torneos", "Mi perfil", "Ranking"]
if is_staff(current_user):
    menu = ["Torneos", "Importar fixture", "Admin usuarios", "Mi perfil", "Ranking"]

choice = st.sidebar.radio("Menú", menu)

if choice == "Importar fixture":
    st.header("Importar fixture pendiente")
    st.write("Columnas requeridas: `torneo`, `ronda`, `fecha_inicio`, `fecha_fin`, `blancas_chesscom`, `negras_chesscom`.")

    template = pd.DataFrame([{
        "torneo": "TORNEO N°11",
        "ronda": 1,
        "fecha_inicio": "2026-02-27 00:00",
        "fecha_fin": "2026-04-03 23:59",
        "blancas_chesscom": "matiasbulacio",
        "negras_chesscom": "juan123",
    }])
    st.download_button("Descargar plantilla CSV", template.to_csv(index=False).encode("utf-8"), "fixture_pendiente.csv", "text/csv")

    st.subheader("Reglas de detección")
    c1, c2, c3 = st.columns(3)
    rules = "chess" if c1.selectbox("Modalidad", ["Ajedrez normal", "Chess960"]) == "Ajedrez normal" else "chess960"
    time_class = c2.selectbox("Clase", ["blitz", "rapid", "bullet", "daily"])
    time_control = c3.text_input("Ritmo exacto", value="600", help="600=10+0, 300=5+0, 180+2=3+2")

    rated_filter = {"Cualquiera": "any", "Solo rated": "rated", "Solo casual": "casual"}[
        st.selectbox("Rated/Casual", ["Cualquiera", "Solo rated", "Solo casual"])
    ]
    strict_colors = st.checkbox("Respetar colores exactos", value=True)

    uploaded = st.file_uploader("Subir CSV", type=["csv"])
    if uploaded:
        try:
            df = read_uploaded_csv(uploaded)
            st.dataframe(df, use_container_width=True)

            if st.button("Importar fixture"):
                matches, players = import_fixture_csv(
                    df,
                    current_user["id"],
                    rules,
                    time_class,
                    time_control,
                    rated_filter,
                    strict_colors,
                )
                st.success(f"Importado. Cruces creados: {matches}. Jugadores nuevos: {players}.")
        except Exception as exc:
            st.error(exc)

elif choice == "Torneos":
    st.header("Torneos")
    tournaments = q("SELECT * FROM tournaments ORDER BY id DESC")

    if not tournaments:
        st.info("Todavía no hay torneos cargados.")

    for t in tournaments:
        with st.expander(f"{t['name']} — {t['status']} — {t['time_class']} — {t['time_control']}", expanded=True):
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Ritmo", t["time_control"])
            c2.metric("Clase", t["time_class"])
            c3.metric("Modalidad", t["rules"])
            c4.metric("Colores", "Exactos" if t["strict_colors"] else "Flexibles")

            if is_staff(current_user):
                with st.expander("Modificar torneo"):
                    ec1, ec2, ec3 = st.columns(3)
                    new_time = ec1.text_input("Ritmo regular", value=str(t["time_control"]), key=f"time_{t['id']}")
                    new_class = ec2.selectbox(
                        "Clase regular",
                        ["blitz", "rapid", "bullet", "daily"],
                        index=["blitz", "rapid", "bullet", "daily"].index(t["time_class"]) if t["time_class"] in ["blitz", "rapid", "bullet", "daily"] else 0,
                        key=f"class_{t['id']}",
                    )
                    new_strict = ec3.checkbox("Colores exactos", value=bool(t["strict_colors"]), key=f"strict_{t['id']}")

                    st.markdown("**Playoffs**")
                    pc1, pc2 = st.columns(2)
                    playoff_time = pc1.text_input("Ritmo playoffs", value=str(t["playoff_time_control"]), key=f"ptime_{t['id']}")
                    playoff_class = pc2.selectbox(
                        "Clase playoffs",
                        ["blitz", "rapid", "bullet", "daily"],
                        index=["blitz", "rapid", "bullet", "daily"].index(t["playoff_time_class"]) if t["playoff_time_class"] in ["blitz", "rapid", "bullet", "daily"] else 0,
                        key=f"pclass_{t['id']}",
                    )

                    if st.button("Guardar configuración", key=f"save_config_{t['id']}"):
                        exec_sql("""
                            UPDATE tournaments
                            SET time_control=?, time_class=?, strict_colors=?, playoff_time_control=?, playoff_time_class=?
                            WHERE id=?
                        """, (new_time, new_class, 1 if new_strict else 0, playoff_time, playoff_class, t["id"]))
                        st.success("Configuración actualizada.")
                        st.rerun()

                b1, b2 = st.columns(2)
                if b1.button("Buscar resultados Chess.com", key=f"scan_{t['id']}"):
                    found, debug = scan_tournament(t["id"])
                    st.success(f"Resultados detectados: {found}")
                    st.dataframe(debug, use_container_width=True)

                if b2.button("Aplicar WO vencidos", key=f"wo_{t['id']}"):
                    applied = apply_wo_expired(t["id"])
                    st.warning(f"WO aplicados: {applied}")
                    st.rerun()

            st.subheader("Rondas")
            rounds = q("SELECT number,start_datetime,end_datetime FROM rounds WHERE tournament_id=? ORDER BY number", (t["id"],))
            st.dataframe([dict(r) for r in rounds], use_container_width=True)

            st.subheader("Cruces")
            matches = q("""
                SELECT m.*, r.number,
                       wu.display_name white_name, wu.chesscom_user white_chess,
                       bu.display_name black_name, bu.chesscom_user black_chess
                FROM matches m
                JOIN rounds r ON r.id=m.round_id
                JOIN users wu ON wu.id=m.white_user_id
                JOIN users bu ON bu.id=m.black_user_id
                WHERE m.tournament_id=?
                ORDER BY r.number, m.id
            """, (t["id"],))
            st.dataframe([{
                "Ronda": m["number"],
                "Blancas": m["white_chess"],
                "Negras": m["black_chess"],
                "Estado": m["status"],
                "Tipo": m["result_type"],
                "Resultado": m["result"] or "",
                "Bloqueada": "Sí" if m["locked"] else "No",
                "Link": m["chesscom_url"] or "",
            } for m in matches], use_container_width=True)

            st.subheader("Tabla")
            st.dataframe(standings(t["id"]), use_container_width=True)

elif choice == "Mi perfil":
    st.header("Mi perfil")
    stats = player_stats(current_user["id"])

    left, right = st.columns([1, 3])
    if current_user.get("avatar_url"):
        left.image(current_user["avatar_url"], width=160)
    right.metric("ELO interno", current_user["elo"])
    right.write(f"**Nombre:** {current_user['display_name']}")
    right.write(f"**Chess.com:** {current_user['chesscom_user']}")
    right.write(f"**Rol:** {current_user['role']}")

    if st.button("Sincronizar avatar con Chess.com"):
        sync_avatar(current_user["id"], current_user["chesscom_user"])
        st.rerun()

    st.subheader("Cambiar contraseña")
    with st.form("change_password"):
        old = st.text_input("Contraseña actual", type="password")
        new1 = st.text_input("Nueva contraseña", type="password")
        new2 = st.text_input("Repetir nueva contraseña", type="password")
        submitted = st.form_submit_button("Cambiar")
        if submitted:
            fresh = get_user(current_user["id"])
            if fresh["password_hash"] != hash_password(old):
                st.error("Contraseña actual incorrecta.")
            elif not new1 or new1 != new2:
                st.error("Las nuevas contraseñas no coinciden.")
            else:
                update_password(current_user["id"], new1, must_change=0)
                st.success("Contraseña actualizada.")

    st.subheader("Estadísticas")
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("PJ", stats["PJ"])
    c2.metric("G", stats["G"])
    c3.metric("E", stats["E"])
    c4.metric("P", stats["P"])
    c5.metric("WO", stats["WO"])
    c6.metric("Rend.", f"{stats['Rendimiento']}%")

elif choice == "Ranking":
    st.header("Ranking general")
    users = q("SELECT display_name,chesscom_user,elo,role,account_status,must_change_password FROM users ORDER BY elo DESC")
    st.dataframe([{
        "Nombre": u["display_name"],
        "Chess.com": u["chesscom_user"],
        "ELO": u["elo"],
        "Rol": u["role"],
        "Estado": u["account_status"],
        "Clave temporal": "Sí" if u["must_change_password"] else "No",
    } for u in users], use_container_width=True)

elif choice == "Admin usuarios":
    st.header("Admin usuarios")

    users = q("SELECT * FROM users ORDER BY role DESC, display_name")
    st.dataframe([{
        "ID": u["id"],
        "Nombre": u["display_name"],
        "Usuario": u["username"],
        "Chess.com": u["chesscom_user"],
        "Rol": u["role"],
        "ELO": u["elo"],
        "Estado": u["account_status"],
        "Clave temporal": "Sí" if u["must_change_password"] else "No",
    } for u in users], use_container_width=True)

    st.subheader("Crear jugador con clave 12345")
    new_chess = st.text_input("Usuario Chess.com nuevo")
    new_name = st.text_input("Nombre visible opcional")
    if st.button("Crear jugador"):
        if new_chess:
            get_or_create_player(new_chess, new_name)
            st.success("Jugador creado o ya existente.")
            st.rerun()

    labels = {f"{u['display_name']} ({u['chesscom_user']})": u["id"] for u in users}

    if labels:
        st.subheader("Resetear contraseña")
        target = st.selectbox("Usuario", list(labels.keys()), key="reset_user")
        if st.button("Resetear a 12345"):
            update_password(labels[target], DEFAULT_PASSWORD, must_change=1)
            st.success("Contraseña reseteada a 12345.")
            st.rerun()

        if is_admin(current_user):
            st.subheader("Cambiar rol / estado")
            target2 = st.selectbox("Usuario a modificar", list(labels.keys()), key="edit_user")
            target_user = get_user(labels[target2])

            allowed_roles = ["player", "moderator", "admin"]
            if is_superadmin(current_user):
                allowed_roles.append("superadmin")

            new_role = st.selectbox(
                "Rol",
                allowed_roles,
                index=allowed_roles.index(target_user["role"]) if target_user["role"] in allowed_roles else 0,
            )
            new_status = st.selectbox(
                "Estado",
                ["pending", "active", "suspended"],
                index=["pending", "active", "suspended"].index(target_user["account_status"]) if target_user["account_status"] in ["pending", "active", "suspended"] else 1,
            )
            new_elo = st.number_input("ELO", value=int(target_user["elo"]), step=10)

            if st.button("Guardar usuario"):
                if target_user["id"] == current_user["id"] and current_user["role"] == "superadmin" and new_role != "superadmin":
                    st.error("No te podés quitar superadmin a vos mismo.")
                else:
                    exec_sql("UPDATE users SET role=?, account_status=?, elo=? WHERE id=?", (new_role, new_status, int(new_elo), target_user["id"]))
                    st.success("Usuario actualizado.")
                    st.rerun()