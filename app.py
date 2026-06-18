import datetime as dt
import hashlib
import io
import logging
import os
from pathlib import Path
import urllib.parse

import pandas as pd
import requests
import streamlit as st

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


APP_TITLE = "Torneos de Ajedrez"
DEFAULT_PASSWORD = "12345"
API_BASE = "https://api.chess.com/pub"
HEADERS = {"User-Agent": "torneos-ajedrez/1.0 (hmatias8626@gmail.com)"}
SQLITE_PATH = Path("torneos_ajedrez_local.db")


st.set_page_config(page_title=APP_TITLE, layout="wide")


# =========================================================
# DB ENGINE: Supabase/PostgreSQL + SQLite fallback local
# =========================================================

def has_pg_secrets():
    needed = ["DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD"]
    return all(k in st.secrets for k in needed)


def use_postgres():
    return has_pg_secrets()


def pg_conn():
    import psycopg2
    return psycopg2.connect(
        host=st.secrets["DB_HOST"],
        port=int(st.secrets["DB_PORT"]),
        dbname=st.secrets["DB_NAME"],
        user=st.secrets["DB_USER"],
        password=st.secrets["DB_PASSWORD"],
        sslmode="require",
    )


def sqlite_conn():
    import sqlite3
    conn = sqlite3.connect(SQLITE_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def conn():
    return pg_conn() if use_postgres() else sqlite_conn()


def adapt_sql(sql):
    if use_postgres():
        return sql.replace("?", "%s")
    return sql


def rows_to_dicts(cursor, rows):
    if use_postgres():
        cols = [d[0] for d in cursor.description] if cursor.description else []
        return [dict(zip(cols, row)) for row in rows]
    return [dict(row) for row in rows]


def q(sql, params=(), one=False):
    c = conn()
    cur = c.cursor()
    cur.execute(adapt_sql(sql), params)
    rows = cur.fetchall()
    out = rows_to_dicts(cur, rows)
    c.close()
    if one:
        return out[0] if out else None
    return out


def exec_sql(sql, params=()):
    c = conn()
    cur = c.cursor()
    cur.execute(adapt_sql(sql), params)
    last_id = None

    if use_postgres():
        try:
            if cur.description:
                row = cur.fetchone()
                if row:
                    last_id = row[0]
        except Exception:
            last_id = None
    else:
        last_id = cur.lastrowid

    c.commit()
    c.close()
    return last_id


def db_now_default():
    return "CURRENT_TIMESTAMP"


def init_db():
    c = conn()
    cur = c.cursor()

    if use_postgres():
        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE,
            password_hash TEXT,
            chesscom_user TEXT UNIQUE,
            display_name TEXT,
            role TEXT DEFAULT 'player',
            elo INTEGER DEFAULT 1200,
            avatar_url TEXT,
            account_status TEXT DEFAULT 'pending',
            must_change_password INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS chess_aliases (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            alias TEXT UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS tournaments (
            id SERIAL PRIMARY KEY,
            name TEXT,
            description TEXT,
            status TEXT DEFAULT 'open',
            tournament_type TEXT DEFAULT 'fixture',
            rules TEXT DEFAULT 'chess',
            time_class TEXT DEFAULT 'rapid',
            time_control TEXT DEFAULT '600',
            rated_filter TEXT DEFAULT 'any',
            strict_colors INTEGER DEFAULT 1,
            playoff_rules TEXT DEFAULT 'chess',
            playoff_time_class TEXT DEFAULT 'blitz',
            playoff_time_control TEXT DEFAULT '300',
            playoff_rated_filter TEXT DEFAULT 'any',
            win_points REAL DEFAULT 1,
            draw_points REAL DEFAULT 0.5,
            loss_points REAL DEFAULT 0,
            bye_points REAL DEFAULT 1,
            wo_points REAL DEFAULT 0,
            created_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS rounds (
            id SERIAL PRIMARY KEY,
            tournament_id INTEGER REFERENCES tournaments(id) ON DELETE CASCADE,
            number INTEGER,
            start_datetime TIMESTAMP,
            end_datetime TIMESTAMP,
            status TEXT DEFAULT 'active'
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS registrations (
            id SERIAL PRIMARY KEY,
            tournament_id INTEGER REFERENCES tournaments(id) ON DELETE CASCADE,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            status TEXT DEFAULT 'active',
            wo_count INTEGER DEFAULT 0,
            UNIQUE(tournament_id, user_id)
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS matches (
            id SERIAL PRIMARY KEY,
            tournament_id INTEGER REFERENCES tournaments(id) ON DELETE CASCADE,
            round_id INTEGER REFERENCES rounds(id) ON DELETE CASCADE,
            white_user_id INTEGER REFERENCES users(id),
            black_user_id INTEGER REFERENCES users(id),
            status TEXT DEFAULT 'pending',
            result TEXT,
            result_type TEXT DEFAULT 'normal',
            chesscom_url TEXT,
            game_uuid TEXT,
            locked INTEGER DEFAULT 0,
            detected_at TIMESTAMP,
            review_game_uuid TEXT,
            review_url TEXT,
            review_result TEXT,
            rejected_game_uuid TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS elo_history (
            id SERIAL PRIMARY KEY,
            match_id INTEGER,
            user_id INTEGER,
            old_elo INTEGER,
            new_elo INTEGER,
            delta INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id SERIAL PRIMARY KEY,
            user_id INTEGER,
            action TEXT,
            detail TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS scan_jobs (
            id SERIAL PRIMARY KEY,
            tournament_id INTEGER REFERENCES tournaments(id) ON DELETE CASCADE,
            round_number INTEGER,
            started_by INTEGER,
            status TEXT DEFAULT 'running',
            total_items INTEGER DEFAULT 0,
            processed_items INTEGER DEFAULT 0,
            detected_items INTEGER DEFAULT 0,
            review_items INTEGER DEFAULT 0,
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            finished_at TIMESTAMP
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS scan_job_items (
            id SERIAL PRIMARY KEY,
            job_id INTEGER REFERENCES scan_jobs(id) ON DELETE CASCADE,
            match_id INTEGER,
            cruce TEXT,
            status TEXT,
            detail TEXT,
            chesscom_url TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS review_reports (
            id SERIAL PRIMARY KEY,
            match_id INTEGER REFERENCES matches(id) ON DELETE CASCADE,
            requested_by INTEGER,
            reviewed_by INTEGER,
            status TEXT DEFAULT 'requested',
            report_text TEXT,
            decision TEXT,
            sanctioned_user_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            decided_at TIMESTAMP
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS player_warnings (
            id SERIAL PRIMARY KEY,
            tournament_id INTEGER REFERENCES tournaments(id) ON DELETE CASCADE,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            match_id INTEGER,
            warning_type TEXT,
            reason TEXT,
            created_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
    else:
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
        CREATE TABLE IF NOT EXISTS chess_aliases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            alias TEXT UNIQUE,
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
            time_class TEXT DEFAULT 'rapid',
            time_control TEXT DEFAULT '600',
            rated_filter TEXT DEFAULT 'any',
            strict_colors INTEGER DEFAULT 1,
            playoff_rules TEXT DEFAULT 'chess',
            playoff_time_class TEXT DEFAULT 'blitz',
            playoff_time_control TEXT DEFAULT '300',
            playoff_rated_filter TEXT DEFAULT 'any',
            win_points REAL DEFAULT 1,
            draw_points REAL DEFAULT 0.5,
            loss_points REAL DEFAULT 0,
            bye_points REAL DEFAULT 1,
            wo_points REAL DEFAULT 0,
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
            review_game_uuid TEXT,
            review_url TEXT,
            review_result TEXT,
            rejected_game_uuid TEXT,
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

        cur.execute("""
        CREATE TABLE IF NOT EXISTS scan_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tournament_id INTEGER,
            round_number INTEGER,
            started_by INTEGER,
            status TEXT DEFAULT 'running',
            total_items INTEGER DEFAULT 0,
            processed_items INTEGER DEFAULT 0,
            detected_items INTEGER DEFAULT 0,
            review_items INTEGER DEFAULT 0,
            started_at TEXT DEFAULT CURRENT_TIMESTAMP,
            finished_at TEXT
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS scan_job_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER,
            match_id INTEGER,
            cruce TEXT,
            status TEXT,
            detail TEXT,
            chesscom_url TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS review_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER,
            requested_by INTEGER,
            reviewed_by INTEGER,
            status TEXT DEFAULT 'requested',
            report_text TEXT,
            decision TEXT,
            sanctioned_user_id INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            decided_at TEXT
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS player_warnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tournament_id INTEGER,
            user_id INTEGER,
            match_id INTEGER,
            warning_type TEXT,
            reason TEXT,
            created_by INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)

    c.commit()
    c.close()



def column_exists_runtime(table, column):
    c = conn()
    cur = c.cursor()
    if use_postgres():
        cur.execute("""
            SELECT COUNT(*) AS c
            FROM information_schema.columns
            WHERE table_name=%s AND column_name=%s
        """, (table, column))
        exists = cur.fetchone()[0] > 0
    else:
        cur.execute(f"PRAGMA table_info({table})")
        exists = column in [row[1] for row in cur.fetchall()]
    c.close()
    return exists


def ensure_column_runtime(table, column, definition):
    if use_postgres():
        try:
            exec_sql(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {definition}")
        except Exception:
            # Si la columna ya existe o Supabase responde raro, no frenamos la app.
            pass
    else:
        if not column_exists_runtime(table, column):
            exec_sql(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

def ensure_v9_columns():
    cols = [
        ("tournaments", "win_points", "REAL DEFAULT 1"),
        ("tournaments", "draw_points", "REAL DEFAULT 0.5"),
        ("tournaments", "loss_points", "REAL DEFAULT 0"),
        ("tournaments", "bye_points", "REAL DEFAULT 1"),
        ("tournaments", "wo_points", "REAL DEFAULT 0"),
        ("tournaments", "rounds_count", "INTEGER DEFAULT 0"),
        ("tournaments", "cups_count", "INTEGER DEFAULT 3"),
        ("tournaments", "qualifiers_count", "INTEGER DEFAULT 24"),
        ("tournaments", "cup_size", "INTEGER DEFAULT 8"),
        ("users", "celular", "TEXT"),
    ]
    for table, col, definition in cols:
        ensure_column_runtime(table, col, definition)


def insert_returning(table, cols, values):
    placeholders = ", ".join(["?"] * len(values))
    coltxt = ", ".join(cols)

    if use_postgres():
        sql = f"INSERT INTO {table} ({coltxt}) VALUES ({placeholders}) RETURNING id"
        return exec_sql(sql, values)

    sql = f"INSERT INTO {table} ({coltxt}) VALUES ({placeholders})"
    return exec_sql(sql, values)


# =========================================================
# GENERAL HELPERS
# =========================================================

def norm(value):
    return (str(value) if value is not None else "").strip().lower()


def valid_chess_username(value):
    value = norm(value)
    if not value or value in {"0", "nan", "none", "null", "-"}:
        return False
    return len(value) >= 2


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
    return row


def audit(user_id, action, detail=""):
    insert_returning("audit_log", ["user_id", "action", "detail"], [user_id, action, detail])


# =========================================================
# CHESS.COM
# =========================================================

@st.cache_data(ttl=300)
def chess_profile(username):
    try:
        r = requests.get(f"{API_BASE}/player/{norm(username)}", headers=HEADERS, timeout=20)
        if r.status_code == 200:
            return r.json()
        logger.warning("chess_profile(%s): HTTP %s", username, r.status_code)
    except Exception as e:
        logger.exception("chess_profile(%s): %s", username, e)
    return None


def chess_user_exists(chesscom_user):
    return chess_profile(chesscom_user) is not None


def sync_avatar(user_id, chesscom_user):
    profile = chess_profile(chesscom_user)
    if profile and profile.get("avatar"):
        exec_sql("UPDATE users SET avatar_url=? WHERE id=?", (profile.get("avatar"), user_id))


@st.cache_data(ttl=60)
def chess_archives(username):
    try:
        r = requests.get(f"{API_BASE}/player/{norm(username)}/games/archives", headers=HEADERS, timeout=20)
        if r.status_code == 200:
            return r.json().get("archives", [])
        logger.warning("chess_archives(%s): HTTP %s", username, r.status_code)
    except Exception as e:
        logger.exception("chess_archives(%s): %s", username, e)
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


@st.cache_data(ttl=120)
def chess_games_month(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code == 200:
            return r.json().get("games", [])
        logger.warning("chess_games_month(%s): HTTP %s", url, r.status_code)
    except Exception as e:
        logger.exception("chess_games_month(%s): %s", url, e)
    return []


def chess_games_between(username, start_dt, end_dt):
    games = []
    keys = month_keys_between(start_dt, end_dt)
    for url in chess_archives(username):
        if any(k in url for k in keys):
            games.extend(chess_games_month(url))
    return games


def parse_ts(timestamp):
    try:
        return dt.datetime.fromtimestamp(int(timestamp))
    except Exception:
        return None



def is_bye_value(value):
    v = norm(value)
    return v in {"", "0", "bye", "libre", "free", "descanso", "sin rival", "none", "nan", "-"}


def tournament_points_config(tournament):
    return {
        "win": float(tournament.get("win_points", 1) if tournament.get("win_points") is not None else 1),
        "draw": float(tournament.get("draw_points", 0.5) if tournament.get("draw_points") is not None else 0.5),
        "loss": float(tournament.get("loss_points", 0) if tournament.get("loss_points") is not None else 0),
        "bye": float(tournament.get("bye_points", 1) if tournament.get("bye_points") is not None else 1),
        "wo": float(tournament.get("wo_points", 0) if tournament.get("wo_points") is not None else 0),
    }

# =========================================================
# USERS / ALIASES
# =========================================================

def add_chess_alias(user_id, alias):
    alias = norm(alias)
    if not valid_chess_username(alias):
        raise ValueError("Alias inválido.")
    insert_returning("chess_aliases", ["user_id", "alias"], [user_id, alias])


def username_belongs_to_user(chess_name, user_id):
    chess_name = norm(chess_name)
    user = get_user(user_id)
    if user and norm(user["chesscom_user"]) == chess_name:
        return True
    alias = q("SELECT id FROM chess_aliases WHERE user_id=? AND alias=?", (user_id, chess_name), one=True)
    return alias is not None


def update_user_chess(user_id, new_chess, new_display=None, add_old_as_alias=True):
    new_chess = norm(new_chess)
    if not valid_chess_username(new_chess):
        raise ValueError("Usuario Chess.com inválido.")
    if not chess_user_exists(new_chess):
        raise ValueError("Ese usuario no existe en Chess.com.")

    user = get_user(user_id)
    if not user:
        raise ValueError("Usuario no encontrado.")

    existing = q("SELECT id FROM users WHERE chesscom_user=? AND id<>?", (new_chess, user_id), one=True)
    if existing:
        raise ValueError("Ese Chess.com ya pertenece a otro perfil.")

    old_chess = norm(user["chesscom_user"])
    if add_old_as_alias and valid_chess_username(old_chess) and old_chess != new_chess:
        add_chess_alias(user_id, old_chess)

    display = new_display.strip() if new_display else user["display_name"]
    exec_sql("UPDATE users SET username=?, chesscom_user=?, display_name=? WHERE id=?", (new_chess, new_chess, display, user_id))
    sync_avatar(user_id, new_chess)


def get_or_create_player(chesscom_user, display_name=None, validate_exists=False):
    chesscom_user = norm(chesscom_user)
    if not valid_chess_username(chesscom_user):
        raise ValueError(f"Usuario Chess.com inválido: {chesscom_user}")

    if validate_exists and not chess_user_exists(chesscom_user):
        raise ValueError(f"{chesscom_user} no existe en Chess.com.")

    row = q("SELECT * FROM users WHERE chesscom_user=?", (chesscom_user,), one=True)
    if row:
        return row["id"], False

    username = chesscom_user
    if q("SELECT id FROM users WHERE username=?", (username,), one=True):
        username = f"{chesscom_user}_{dt.datetime.now().strftime('%H%M%S')}"

    user_id = insert_returning(
        "users",
        ["username", "password_hash", "chesscom_user", "display_name", "role", "elo", "account_status", "must_change_password"],
        [username, hash_password(DEFAULT_PASSWORD), chesscom_user, display_name or chesscom_user, "player", 1200, "pending", 1],
    )
    sync_avatar(user_id, chesscom_user)
    return user_id, True


def create_or_claim_user(username, password, chesscom_user, display_name):
    username = norm(username) or norm(chesscom_user)
    chesscom_user = norm(chesscom_user)

    if not chess_user_exists(chesscom_user):
        raise ValueError("Ese usuario no existe en Chess.com. Usá tu usuario real de Chess.com.")

    display_name = display_name.strip() if display_name else username
    count = q("SELECT COUNT(*) AS c FROM users", one=True)["c"]

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
    user_id = insert_returning(
        "users",
        ["username", "password_hash", "chesscom_user", "display_name", "role", "elo", "account_status", "must_change_password"],
        [username, hash_password(password), chesscom_user, display_name, role, 1200, "active", 0],
    )
    sync_avatar(user_id, chesscom_user)
    return user_id


def login(username, password):
    username = norm(username)
    user = q("SELECT * FROM users WHERE username=? OR chesscom_user=?", (username, username), one=True)
    if not user:
        return None, "Usuario no encontrado."
    if user["account_status"] == "suspended":
        return None, "Usuario suspendido."
    if user["password_hash"] != hash_password(password):
        return None, "Contraseña incorrecta."

    if user["account_status"] == "pending":
        exec_sql("UPDATE users SET account_status='active' WHERE id=?", (user["id"],))
        user = get_user(user["id"])

    return user, None


def update_password(user_id, new_password, must_change=0):
    exec_sql("""
        UPDATE users
        SET password_hash=?, must_change_password=?, account_status='active'
        WHERE id=?
    """, (hash_password(new_password), must_change, user_id))


# =========================================================
# CSV / DATE PARSING
# =========================================================

def read_uploaded_csv(file):
    raw = file.getvalue()
    encodings = ["utf-8", "utf-8-sig", "latin-1", "cp1252"]
    seps = [",", ";", "\t"]
    last_error = None

    for enc in encodings:
        for sep in seps:
            try:
                text = raw.decode(enc)
                df = pd.read_csv(io.StringIO(text), sep=sep)
                if len(df.columns) > 1:
                    df.columns = [str(c).strip() for c in df.columns]
                    return df
            except Exception as exc:
                last_error = exc

    raise Exception(f"No pude leer el CSV. Guardalo como CSV UTF-8 o separado por punto y coma. Error: {last_error}")


def parse_date_series(series):
    # dayfirst=True para Argentina: 03/04/2026 = 3 de abril
    return pd.to_datetime(series, errors="coerce", dayfirst=True)


def to_iso(value):
    if isinstance(value, str):
        return value
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime().isoformat(timespec="seconds")
    if isinstance(value, dt.datetime):
        return value.isoformat(timespec="seconds")
    return str(value)


def parse_db_datetime(value):
    if isinstance(value, dt.datetime):
        return value
    return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00").replace("+00:00", ""))


# =========================================================
# ELO / RESULTS
# =========================================================

def expected_score(ra, rb):
    return 1 / (1 + 10 ** ((rb - ra) / 400))


def elo_delta(ra, rb, score, k=32):
    return round(k * (score - expected_score(ra, rb)))


def result_label(score):
    if score == 1.0:
        return "1-0"
    if score == 0.0:
        return "0-1"
    if score == 0.5:
        return "1/2-1/2"
    return ""


def score_from_result_label(result):
    if result == "1-0":
        return 1.0
    if result == "0-1":
        return 0.0
    if result == "1/2-1/2":
        return 0.5
    return None


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


def apply_elo(match_id, white_id, black_id, white_score):
    if black_id is None:
        return  # bye match — no Elo change
    white = get_user(white_id)
    black = get_user(black_id)
    if not white or not black:
        return

    black_score = 1 - white_score if white_score in (0, 1) else 0.5
    dw = elo_delta(white["elo"], black["elo"], white_score)
    db = elo_delta(black["elo"], white["elo"], black_score)

    exec_sql("UPDATE users SET elo=? WHERE id=?", (white["elo"] + dw, white_id))
    exec_sql("UPDATE users SET elo=? WHERE id=?", (black["elo"] + db, black_id))

    insert_returning("elo_history", ["match_id", "user_id", "old_elo", "new_elo", "delta"], [match_id, white_id, white["elo"], white["elo"] + dw, dw])
    insert_returning("elo_history", ["match_id", "user_id", "old_elo", "new_elo", "delta"], [match_id, black_id, black["elo"], black["elo"] + db, db])


# =========================================================
# MATCH DETECTION
# =========================================================

def game_is_between_players_any_color(game, white_user_id, black_user_id):
    actual_white = norm(game.get("white", {}).get("username"))
    actual_black = norm(game.get("black", {}).get("username"))

    exact = username_belongs_to_user(actual_white, white_user_id) and username_belongs_to_user(actual_black, black_user_id)
    inverted = username_belongs_to_user(actual_white, black_user_id) and username_belongs_to_user(actual_black, white_user_id)
    return exact, inverted


def validate_game_without_color(game, tournament, start_dt, end_dt):
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


def score_for_fixture_white(game, fixture_white_user_id, fixture_black_user_id):
    actual_white = norm(game.get("white", {}).get("username"))
    actual_score_white = score_for_white(game)
    if actual_score_white is None:
        return None

    if username_belongs_to_user(actual_white, fixture_white_user_id):
        return actual_score_white

    if username_belongs_to_user(actual_white, fixture_black_user_id):
        if actual_score_white == 0.5:
            return 0.5
        return 1.0 - actual_score_white

    return None


def mark_color_review(match, game):
    score = score_for_fixture_white(game, match["white_user_id"], match["black_user_id"])
    if score is None:
        return False

    result = result_label(score)
    exec_sql("""
        UPDATE matches
        SET status='review', result=?, result_type='color_review',
            review_game_uuid=?, review_url=?, review_result=?,
            chesscom_url=?, game_uuid=?, detected_at=?
        WHERE id=?
    """, (
        result,
        game.get("uuid"),
        game.get("url"),
        result,
        game.get("url"),
        game.get("uuid"),
        dt.datetime.now(),
        match["id"],
    ))
    return True


def scan_tournament(tournament_id):
    tournament = q("SELECT * FROM tournaments WHERE id=?", (tournament_id,), one=True)
    if not tournament:
        return 0, [{"Cruce": "-", "Estado": "torneo no encontrado"}]

    pending = q("""
        SELECT m.*, r.number, r.start_datetime, r.end_datetime,
               wu.chesscom_user AS white_chess,
               bu.chesscom_user AS black_chess
        FROM matches m
        JOIN rounds r ON r.id=m.round_id
        JOIN users wu ON wu.id=m.white_user_id
        LEFT JOIN users bu ON bu.id=m.black_user_id
        WHERE m.tournament_id=? AND m.status='pending' AND m.locked=0
    """, (tournament_id,))

    found = 0
    debug = []

    for match in pending:
        if match.get("black_user_id") is None or is_bye_value(match.get("black_chess")):
            exec_sql(
                "UPDATE matches SET status='finished', result='BYE', result_type='bye', locked=1, black_user_id=NULL, detected_at=? WHERE id=?",
                (dt.datetime.now(), match["id"]),
            )
            found += 1
            debug.append({"Cruce": f"{match['white_chess']} vs LIBRE/BYE", "Estado": "bye automático"})
            continue

        start_dt = parse_db_datetime(match["start_datetime"])
        end_dt = parse_db_datetime(match["end_datetime"])

        white_exists = chess_user_exists(match["white_chess"])
        black_exists = chess_user_exists(match["black_chess"])

        if not white_exists or not black_exists:
            missing = []
            if not white_exists:
                missing.append(match["white_chess"])
            if not black_exists:
                missing.append(match["black_chess"])
            debug.append({"Cruce": f"{match['white_chess']} vs {match['black_chess']}", "Estado": "usuario inexistente en Chess.com: " + ", ".join(missing)})
            continue

        white_names = _user_chess_names(match["white_user_id"])
        black_names = _user_chess_names(match["black_user_id"])

        games = chess_games_between(match["white_chess"], start_dt, end_dt)
        games += chess_games_between(match["black_chess"], start_dt, end_dt)

        unique = []
        seen = set()
        for game in games:
            gid = game.get("uuid") or game.get("url")
            if gid in seen:
                continue
            seen.add(gid)
            unique.append(game)

        final_reason = "sin partidas de los jugadores en el rango"

        for game in unique:
            if match.get("rejected_game_uuid") and game.get("uuid") == match.get("rejected_game_uuid"):
                final_reason = "partida invertida rechazada previamente"
                continue

            actual_white = norm(game.get("white", {}).get("username"))
            actual_black = norm(game.get("black", {}).get("username"))
            exact    = actual_white in white_names and actual_black in black_names
            inverted = actual_white in black_names and actual_black in white_names

            if not exact and not inverted:
                continue

            ok, reason = validate_game_without_color(game, tournament, start_dt, end_dt)
            if not ok:
                final_reason = reason
                continue

            actual_score_white = score_for_white(game)
            if actual_score_white is None:
                final_reason = "partida encontrada sin resultado interpretable"
                continue

            score = actual_score_white if actual_white in white_names else (
                1.0 - actual_score_white if actual_score_white != 0.5 else 0.5
            )
            result = result_label(score)

            if exact:
                exec_sql("""
                    UPDATE matches
                    SET status='finished', result=?, result_type='normal',
                        chesscom_url=?, game_uuid=?, locked=1, detected_at=?
                    WHERE id=?
                """, (result, game.get("url"), game.get("uuid"), dt.datetime.now(), match["id"]))
                apply_elo(match["id"], match["white_user_id"], match["black_user_id"], score)
                found += 1
                final_reason = "detectada"
                break

            if inverted:
                if mark_color_review(match, game):
                    final_reason = "se jugó con colores invertidos: requiere aceptar o rechazar"
                    break

        debug.append({"Cruce": f"{match['white_chess']} vs {match['black_chess']}", "Estado": final_reason})

    return found, debug


# =========================================================
# TOURNAMENT ACTIONS
# =========================================================

def register_player(tournament_id, user_id):
    try:
        insert_returning("registrations", ["tournament_id", "user_id", "status", "wo_count"], [tournament_id, user_id, "active", 0])
    except Exception:
        pass


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

        tournament_id = insert_returning(
            "tournaments",
            ["name", "description", "status", "tournament_type", "rules", "time_class", "time_control", "rated_filter", "strict_colors", "created_by"],
            [tournament_name, "Fixture importado para detección automática", "playing", "fixture_importado", rules, time_class, str(time_control), rated_filter, 1 if strict_colors else 0, created_by],
        )

        for round_number, round_df in group.groupby("ronda"):
            round_number = int(round_number)
            start_value = parse_date_series(round_df["fecha_inicio"]).min()
            end_value = parse_date_series(round_df["fecha_fin"]).max()
            if pd.isna(start_value) or pd.isna(end_value):
                continue

            round_id = insert_returning(
                "rounds",
                ["tournament_id", "number", "start_datetime", "end_datetime", "status"],
                [tournament_id, round_number, start_value.to_pydatetime(), end_value.to_pydatetime(), "active"],
            )

            for _, row in round_df.iterrows():
                white_raw = norm(row["blancas_chesscom"])
                black_raw = norm(row["negras_chesscom"])

                if not valid_chess_username(white_raw):
                    continue

                white_id, wc = get_or_create_player(white_raw)
                created_players += int(wc)
                register_player(tournament_id, white_id)

                if is_bye_value(black_raw):
                    insert_returning(
                        "matches",
                        ["tournament_id", "round_id", "white_user_id", "black_user_id", "status", "result", "result_type", "locked"],
                        [tournament_id, round_id, white_id, None, "finished", "BYE", "bye", 1],
                    )
                    created_matches += 1
                    continue

                if not valid_chess_username(black_raw):
                    continue

                black_id, bc = get_or_create_player(black_raw)
                created_players += int(bc)
                register_player(tournament_id, black_id)

                insert_returning(
                    "matches",
                    ["tournament_id", "round_id", "white_user_id", "black_user_id", "status", "locked"],
                    [tournament_id, round_id, white_id, black_id, "pending", 0],
                )
                created_matches += 1

    audit(created_by, "import_fixture", f"matches={created_matches}, players={created_players}")
    return created_matches, created_players


def import_rounds_to_existing_tournament(df, tournament_id):
    required = {"ronda", "fecha_inicio", "fecha_fin", "blancas_chesscom", "negras_chesscom"}
    missing = required - set(df.columns)
    if missing:
        raise Exception(f"Faltan columnas: {', '.join(sorted(missing))}")

    created_matches = 0
    created_players = 0

    for round_number, round_df in df.groupby("ronda"):
        round_number = int(round_number)
        start_value = parse_date_series(round_df["fecha_inicio"]).min()
        end_value = parse_date_series(round_df["fecha_fin"]).max()
        if pd.isna(start_value) or pd.isna(end_value):
            continue

        existing_round = q("SELECT id FROM rounds WHERE tournament_id=? AND number=?", (tournament_id, round_number), one=True)
        if existing_round:
            round_id = existing_round["id"]
            exec_sql("UPDATE rounds SET start_datetime=?, end_datetime=? WHERE id=?", (start_value.to_pydatetime(), end_value.to_pydatetime(), round_id))
        else:
            round_id = insert_returning(
                "rounds",
                ["tournament_id", "number", "start_datetime", "end_datetime", "status"],
                [tournament_id, round_number, start_value.to_pydatetime(), end_value.to_pydatetime(), "active"],
            )

        for _, row in round_df.iterrows():
            white_raw = norm(row["blancas_chesscom"])
            black_raw = norm(row["negras_chesscom"])
            if not valid_chess_username(white_raw):
                continue

            white_id, wc = get_or_create_player(white_raw)
            created_players += int(wc)
            register_player(tournament_id, white_id)

            if is_bye_value(black_raw):
                exists_bye = q(
                    "SELECT id FROM matches WHERE tournament_id=? AND round_id=? AND white_user_id=? AND black_user_id IS NULL",
                    (tournament_id, round_id, white_id), one=True,
                )
                if not exists_bye:
                    insert_returning(
                        "matches",
                        ["tournament_id", "round_id", "white_user_id", "black_user_id", "status", "result", "result_type", "locked"],
                        [tournament_id, round_id, white_id, None, "finished", "BYE", "bye", 1],
                    )
                    created_matches += 1
                continue

            if not valid_chess_username(black_raw):
                continue

            black_id, bc = get_or_create_player(black_raw)
            created_players += int(bc)
            register_player(tournament_id, black_id)

            exists = q("""
                SELECT id FROM matches
                WHERE tournament_id=? AND round_id=? AND white_user_id=? AND black_user_id=?
            """, (tournament_id, round_id, white_id, black_id), one=True)
            if exists:
                continue

            insert_returning(
                "matches",
                ["tournament_id", "round_id", "white_user_id", "black_user_id", "status", "locked"],
                [tournament_id, round_id, white_id, black_id, "pending", 0],
            )
            created_matches += 1

    exec_sql("UPDATE tournaments SET status='playing' WHERE id=?", (tournament_id,))
    return created_matches, created_players


def create_empty_tournament(name, description, rules, time_class, time_control, rated_filter, strict_colors, created_by):
    return insert_returning(
        "tournaments",
        ["name", "description", "status", "tournament_type", "rules", "time_class", "time_control", "rated_filter", "strict_colors", "created_by"],
        [name, description, "open", "manual", rules, time_class, str(time_control), rated_filter, 1 if strict_colors else 0, created_by],
    )


def accept_color_review(match_id, admin_user_id):
    match = q("SELECT * FROM matches WHERE id=?", (match_id,), one=True)
    if not match:
        raise ValueError("Partida no encontrada.")
    if match["status"] != "review":
        raise ValueError("La partida no está en revisión.")

    result = match["review_result"] or match["result"]
    score = score_from_result_label(result)
    if score is None:
        raise ValueError("Resultado inválido.")

    exec_sql("""
        UPDATE matches
        SET status='finished', result=?, result_type='color_inverted_accepted', locked=1, detected_at=?
        WHERE id=?
    """, (result, dt.datetime.now(), match_id))
    apply_elo(match_id, match["white_user_id"], match["black_user_id"], score)
    audit(admin_user_id, "accept_color_review", f"match={match_id}, result={result}")


def reject_color_review(match_id, admin_user_id):
    match = q("SELECT * FROM matches WHERE id=?", (match_id,), one=True)
    if not match:
        raise ValueError("Partida no encontrada.")

    exec_sql("""
        UPDATE matches
        SET status='pending', result=NULL, result_type='normal',
            rejected_game_uuid=review_game_uuid,
            review_game_uuid=NULL, review_url=NULL, review_result=NULL,
            chesscom_url=NULL, game_uuid=NULL, locked=0
        WHERE id=?
    """, (match_id,))
    audit(admin_user_id, "reject_color_review", f"match={match_id}")


def set_manual_result(match_id, result, admin_user_id):
    match = q("SELECT * FROM matches WHERE id=?", (match_id,), one=True)
    if not match:
        raise ValueError("Partida no encontrada.")

    old_status = match["status"]
    score = score_from_result_label(result)
    if score is None:
        raise ValueError("Resultado inválido.")

    exec_sql("""
        UPDATE matches
        SET status='finished', result=?, result_type='manual', locked=1, detected_at=?
        WHERE id=?
    """, (result, dt.datetime.now(), match_id))

    if old_status != "finished" and match.get("black_user_id") is not None:
        apply_elo(match_id, match["white_user_id"], match["black_user_id"], score)

    audit(admin_user_id, "manual_result", f"match={match_id}, result={result}")


def clear_match_result(match_id, admin_user_id):
    exec_sql("""
        UPDATE matches
        SET status='pending', result=NULL, result_type='normal', chesscom_url=NULL, game_uuid=NULL,
            locked=0, detected_at=NULL
        WHERE id=?
    """, (match_id,))
    audit(admin_user_id, "clear_result", f"match={match_id}")


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
        end_dt = parse_db_datetime(match["end_datetime"])
        if now <= end_dt:
            continue

        exec_sql("""
            UPDATE matches
            SET status='finished', result='0-0 WO', result_type='wo', locked=1, detected_at=?
            WHERE id=?
        """, (now, match["id"]))

        for uid in [match["white_user_id"], match["black_user_id"]]:
            exec_sql("UPDATE registrations SET wo_count=COALESCE(wo_count,0)+1 WHERE tournament_id=? AND user_id=?", (tournament_id, uid))
            reg = q("SELECT wo_count FROM registrations WHERE tournament_id=? AND user_id=?", (tournament_id, uid), one=True)
            if reg and reg["wo_count"] >= 2:
                exec_sql("UPDATE registrations SET status='disqualified' WHERE tournament_id=? AND user_id=?", (tournament_id, uid))

        applied += 1

    return applied


# =========================================================
# STANDINGS
# =========================================================

def head_to_head_opponents(tournament_id, user_id):
    matches = q("""
        SELECT * FROM matches
        WHERE tournament_id=? AND status='finished'
        AND result_type NOT IN ('bye', 'wo')
        AND (white_user_id=? OR black_user_id=?)
    """, (tournament_id, user_id, user_id))
    opponents = []
    for m in matches:
        if m["white_user_id"] == user_id:
            opponents.append(m["black_user_id"])
        else:
            opponents.append(m["white_user_id"])
    return [op for op in opponents if op is not None]


def raw_points_for_user(tournament_id, user_id, points_cfg):
    matches = q("""
        SELECT * FROM matches
        WHERE tournament_id=? AND status='finished'
        AND (white_user_id=? OR black_user_id=?)
    """, (tournament_id, user_id, user_id))

    pts = 0.0
    for m in matches:
        if m["result_type"] == "bye" or m["result"] == "BYE":
            if m["white_user_id"] == user_id:
                pts += points_cfg["bye"]
            continue

        if m["result_type"] == "wo":
            pts += points_cfg["wo"]
            continue

        if m["white_user_id"] == user_id:
            if m["result"] == "1-0":
                pts += points_cfg["win"]
            elif m["result"] == "0-1":
                pts += points_cfg["loss"]
            elif m["result"] == "1/2-1/2":
                pts += points_cfg["draw"]
        elif m["black_user_id"] == user_id:
            if m["result"] == "0-1":
                pts += points_cfg["win"]
            elif m["result"] == "1-0":
                pts += points_cfg["loss"]
            elif m["result"] == "1/2-1/2":
                pts += points_cfg["draw"]
    return pts


def standings(tournament_id):
    from collections import defaultdict

    tournament = q("SELECT * FROM tournaments WHERE id=?", (tournament_id,), one=True)
    points_cfg = tournament_points_config(tournament or {})

    regs = q("""
        SELECT u.id, u.display_name, u.chesscom_user, u.elo, r.status, r.wo_count
        FROM registrations r
        JOIN users u ON u.id=r.user_id
        WHERE r.tournament_id=? AND r.status!='removed'
    """, (tournament_id,))

    # Una sola query para todas las partidas terminadas del torneo
    all_matches = q("""
        SELECT * FROM matches
        WHERE tournament_id=? AND status='finished'
    """, (tournament_id,))

    # Agrupar partidas por jugador
    player_matches = defaultdict(list)
    for m in all_matches:
        player_matches[m["white_user_id"]].append(m)
        if m["black_user_id"]:
            player_matches[m["black_user_id"]].append(m)

    # Calcular puntos base para todos los jugadores
    base_points = {}
    for user in regs:
        uid = user["id"]
        pts = 0.0
        for m in player_matches[uid]:
            if m["result_type"] == "bye" or m["result"] == "BYE":
                if m["white_user_id"] == uid:
                    pts += points_cfg["bye"]
                continue
            if m["result_type"] == "wo":
                pts += points_cfg["wo"]
                continue
            if m["white_user_id"] == uid:
                if m["result"] == "1-0":   pts += points_cfg["win"]
                elif m["result"] == "0-1": pts += points_cfg["loss"]
                elif m["result"] == "1/2-1/2": pts += points_cfg["draw"]
            else:
                if m["result"] == "0-1":   pts += points_cfg["win"]
                elif m["result"] == "1-0": pts += points_cfg["loss"]
                elif m["result"] == "1/2-1/2": pts += points_cfg["draw"]
        base_points[uid] = pts

    table = []
    for user in regs:
        uid = user["id"]
        wins = draws = losses = wo_count = byes = real_played = 0
        opponents = []

        for m in player_matches[uid]:
            if m["result_type"] == "bye" or m["result"] == "BYE":
                byes += 1
                continue
            if m["result_type"] == "wo":
                wo_count += 1
                continue

            real_played += 1
            opp = m["black_user_id"] if m["white_user_id"] == uid else m["white_user_id"]
            if opp is not None:
                opponents.append(opp)

            if m["white_user_id"] == uid:
                if m["result"] == "1-0":       wins += 1
                elif m["result"] == "0-1":     losses += 1
                elif m["result"] == "1/2-1/2": draws += 1
            else:
                if m["result"] == "0-1":       wins += 1
                elif m["result"] == "1-0":     losses += 1
                elif m["result"] == "1/2-1/2": draws += 1

        buchholz = sum(base_points.get(op, 0) for op in opponents)
        opp_scores = sorted([base_points.get(op, 0) for op in opponents])
        buc1 = sum(opp_scores[1:]) if len(opp_scores) > 1 else buchholz

        table.append({
            "Jugador": user["display_name"],
            "Chess.com": user["chesscom_user"],
            "ELO": user["elo"],
            "PJ reales": real_played,
            "BYE": byes,
            "G": wins,
            "E": draws,
            "P": losses,
            "WO": user["wo_count"],
            "Estado": user["status"],
            "Puntos": base_points[uid],
            "Buchholz": round(buchholz, 2),
            "Buc1": round(buc1, 2),
        })

    table.sort(key=lambda x: (x["Estado"] != "disqualified", x["Puntos"], x["Buchholz"], x["Buc1"], x["ELO"], x["G"]), reverse=True)
    return table



def match_score_parts(result, result_type):
    if result_type == "bye" or result == "BYE":
        return ("BYE", "")
    if result_type == "wo" or result == "0-0 WO":
        return ("WO", "WO")
    if result == "1-0":
        return ("1", "0")
    if result == "0-1":
        return ("0", "1")
    if result == "1/2-1/2":
        return ("½", "½")
    return ("", "")


def wa_link(celular, mensaje):
    """Devuelve URL de WhatsApp con mensaje pregenerado. Celular sin código país (ej: 3815123456)."""
    if not celular:
        return None
    n = "".join(c for c in str(celular) if c.isdigit())
    if not n:
        return None
    if not n.startswith("549"):
        n = "549" + (n[2:] if n.startswith("54") else n)
    return f"https://wa.me/{n}?text={urllib.parse.quote(mensaje)}"


def match_status_badge(status, result_type):
    if result_type == "bye":
        return "🟦 LIBRE"
    if result_type == "wo":
        return "🟥 WO"
    if status == "finished":
        return "✅ Finalizada"
    if status == "review":
        return "🟧 Revisión"
    if status == "pending":
        return "⬜ Pendiente"
    return status


def round_visual_rows(tournament_id, round_number):
    rows = q("""
        SELECT m.*, r.number, r.start_datetime, r.end_datetime,
               wu.display_name AS white_name, wu.chesscom_user AS white_chess, wu.elo AS white_elo,
               bu.display_name AS black_name, bu.chesscom_user AS black_chess, bu.elo AS black_elo
        FROM matches m
        JOIN rounds r ON r.id=m.round_id
        JOIN users wu ON wu.id=m.white_user_id
        LEFT JOIN users bu ON bu.id=m.black_user_id
        WHERE m.tournament_id=? AND r.number=?
        ORDER BY m.id
    """, (tournament_id, round_number))

    out = []
    for m in rows:
        ws, bs = match_score_parts(m.get("result"), m.get("result_type"))
        out.append({
            "♙ ELO": m["white_elo"],
            "Blancas": m["white_name"],
            "Usuario blancas": m["white_chess"],
            "Pts": ws,
            "VS": "vs",
            "Pts ": bs,
            "Usuario negras": m["black_chess"] or "LIBRE/BYE",
            "Negras": m["black_name"] or "LIBRE/BYE",
            "♟ ELO": m["black_elo"] if m["black_elo"] is not None else "",
            "Fecha": str(m["start_datetime"])[:10],
            "Estado": match_status_badge(m["status"], m["result_type"]),
            "Link": m["chesscom_url"] or "",
        })
    return out


def pending_count_for_tournament(tournament_id):
    row = q("""
        SELECT COUNT(*) AS c
        FROM matches
        WHERE tournament_id=? AND status='pending' AND locked=0 AND black_user_id IS NOT NULL
    """, (tournament_id,), one=True)
    return int(row["c"]) if row else 0


def safe_scan_tournament(tournament_id):
    pc = pending_count_for_tournament(tournament_id)
    if pc <= 0:
        return 0, [{"Cruce": "-", "Estado": "sin cruces pendientes para buscar; no se consulta Chess.com"}]
    return scan_tournament(tournament_id)


def cup_names(count):
    base = ["Oro", "Plata", "Bronce", "Cobre", "Hierro", "Promoción"]
    names = []
    for i in range(count):
        names.append(base[i] if i < len(base) else f"Copa {i+1}")
    return names


def playoff_seed_rows(tournament_id):
    table = standings(tournament_id)
    tournament = q("SELECT * FROM tournaments WHERE id=?", (tournament_id,), one=True) or {}
    cups_count = int(tournament.get("cups_count") or 3)
    cup_size = int(tournament.get("cup_size") or 8)
    names = cup_names(cups_count)

    rows = []
    pos = 1
    for ci, cname in enumerate(names):
        start = ci * cup_size
        end = start + cup_size
        for p in table[start:end]:
            rows.append({
                "Copa": cname,
                "Seed": pos,
                "Jugador": p["Jugador"],
                "Chess.com": p["Chess.com"],
                "Puntos": p["Puntos"],
                "Buchholz": p.get("Buchholz", 0),
                "ELO": p["ELO"],
            })
            pos += 1
    return rows


def render_playoff_bracket(tournament_id):
    seeds = playoff_seed_rows(tournament_id)
    if not seeds:
        st.info("Todavía no hay clasificados para mostrar.")
        return

    cups = {}
    for s in seeds:
        cups.setdefault(s["Copa"], []).append(s)

    for cup, players in cups.items():
        st.markdown(f"### 🏆 Copa {cup}")
        st.caption("Vista preliminar por ranking. La llave definitiva se podrá generar cuando cierre la fase regular.")

        half = (len(players) + 1) // 2
        left = players[:half]
        right = players[half:][::-1]

        c1, c2, c3 = st.columns([2, 1, 2])
        with c1:
            st.markdown("**Lado A**")
            for p in left:
                st.markdown(
                    f"<div class='bracket-box'>#{p['Seed']} · {p['Jugador']}<br><small>{p['Chess.com']} · {p['Puntos']} pts · ELO {p['ELO']}</small></div>",
                    unsafe_allow_html=True
                )
        with c2:
            st.markdown("<div class='cup-center'>🏆<br>FINAL</div>", unsafe_allow_html=True)
        with c3:
            st.markdown("**Lado B**")
            for p in right:
                st.markdown(
                    f"<div class='bracket-box'>#{p['Seed']} · {p['Jugador']}<br><small>{p['Chess.com']} · {p['Puntos']} pts · ELO {p['ELO']}</small></div>",
                    unsafe_allow_html=True
                )




def is_registered_in_tournament(tournament_id, user_id):
    row = q("""
        SELECT id FROM registrations
        WHERE tournament_id=? AND user_id=? AND status!='removed'
    """, (tournament_id, user_id), one=True)
    return row is not None


def open_tournaments_for_player(user_id):
    return q("""
        SELECT *
        FROM tournaments
        WHERE status IN ('open', 'playing')
        ORDER BY id DESC
    """)


def my_tournaments(user_id):
    return q("""
        SELECT t.*
        FROM registrations r
        JOIN tournaments t ON t.id=r.tournament_id
        WHERE r.user_id=? AND r.status!='removed'
        ORDER BY t.id DESC
    """, (user_id,))


def register_current_player(tournament_id, user_id):
    register_player(tournament_id, user_id)


def tournament_kpis(tournament_id):
    players = q("SELECT COUNT(*) AS c FROM registrations WHERE tournament_id=? AND status!='removed'", (tournament_id,), one=True)["c"]
    row = q(
        "SELECT COUNT(*) AS total,"
        " SUM(CASE WHEN status='finished' THEN 1 ELSE 0 END) AS finished,"
        " SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending,"
        " SUM(CASE WHEN status='review' THEN 1 ELSE 0 END) AS review"
        " FROM matches WHERE tournament_id=?",
        (tournament_id,), one=True,
    )
    return {
        "Jugadores": players,
        "Cruces": row["total"] or 0,
        "Finalizadas": row["finished"] or 0,
        "Pendientes": row["pending"] or 0,
        "Revisión": row["review"] or 0,
    }


def player_match_rows(tournament_id, user_id):
    return q("""
        SELECT m.*, r.number,
               wu.display_name AS white_name, wu.chesscom_user AS white_chess, wu.elo AS white_elo,
               bu.display_name AS black_name, bu.chesscom_user AS black_chess, bu.elo AS black_elo
        FROM matches m
        JOIN rounds r ON r.id=m.round_id
        JOIN users wu ON wu.id=m.white_user_id
        LEFT JOIN users bu ON bu.id=m.black_user_id
        WHERE m.tournament_id=? AND (m.white_user_id=? OR m.black_user_id=?)
        ORDER BY r.number, m.id
    """, (tournament_id, user_id, user_id))


def result_for_user_in_match(match, user_id):
    if match["result_type"] == "bye" or match["result"] == "BYE":
        return "bye" if match["white_user_id"] == user_id else "none"
    if match["result_type"] == "wo":
        return "wo"
    if match["result"] == "1/2-1/2":
        return "draw"
    if match["result"] == "1-0":
        return "win" if match["white_user_id"] == user_id else "loss"
    if match["result"] == "0-1":
        return "win" if match["black_user_id"] == user_id else "loss"
    return "none"


def award_cards(tournament_id):
    table = standings(tournament_id)
    if not table:
        return []

    cards = []
    leader = table[0]
    cards.append(("👑 Líder", leader["Jugador"], f"{leader['Puntos']} pts · {leader['Chess.com']}"))

    consolation = table[-1]
    cards.append(("🎁 Premio consuelo", consolation["Jugador"], f"{consolation['Puntos']} pts · a no aflojar"))

    regs = q("""
        SELECT u.id, u.display_name, u.chesscom_user
        FROM registrations r
        JOIN users u ON u.id=r.user_id
        WHERE r.tournament_id=? AND r.status!='removed'
    """, (tournament_id,))

    best_win = (0, "-", "-")
    worst_loss = (0, "-", "-")
    most_draws = (0, "-", "-")
    most_games = (0, "-", "-")

    for u in regs:
        matches = q("""
            SELECT m.*, r.number
            FROM matches m
            JOIN rounds r ON r.id=m.round_id
            WHERE m.tournament_id=? AND m.status='finished'
            AND (m.white_user_id=? OR m.black_user_id=?)
            ORDER BY r.number, m.id
        """, (tournament_id, u["id"], u["id"]))

        cur_w = max_w = 0
        cur_l = max_l = 0
        draws = games = 0

        for m in matches:
            res = result_for_user_in_match(m, u["id"])
            if res == "win":
                cur_w += 1
                cur_l = 0
                max_w = max(max_w, cur_w)
                games += 1
            elif res == "loss":
                cur_l += 1
                cur_w = 0
                max_l = max(max_l, cur_l)
                games += 1
            elif res == "draw":
                draws += 1
                games += 1
                cur_w = 0
                cur_l = 0

        if max_w > best_win[0]:
            best_win = (max_w, u["display_name"], u["chesscom_user"])
        if max_l > worst_loss[0]:
            worst_loss = (max_l, u["display_name"], u["chesscom_user"])
        if draws > most_draws[0]:
            most_draws = (draws, u["display_name"], u["chesscom_user"])
        if games > most_games[0]:
            most_games = (games, u["display_name"], u["chesscom_user"])

    if best_win[0] > 0:
        cards.append(("🔥 Mejor racha", best_win[1], f"{best_win[0]} victorias seguidas"))
    if worst_loss[0] > 0:
        cards.append(("🧊 Racha complicada", worst_loss[1], f"{worst_loss[0]} derrotas seguidas"))
    if most_draws[0] > 0:
        cards.append(("🤝 Rey del empate", most_draws[1], f"{most_draws[0]} empates"))
    if most_games[0] > 0:
        cards.append(("⚔️ Más batallador", most_games[1], f"{most_games[0]} partidas reales"))

    return cards


def render_awards(tournament_id):
    cards = award_cards(tournament_id)
    if not cards:
        st.info("Todavía no hay suficientes resultados para mostrar destacados.")
        return

    cols = st.columns(3)
    for i, (title, name, detail) in enumerate(cards):
        with cols[i % 3]:
            st.markdown(
                f"""
                <div class='award-card'>
                    <div class='award-title'>{title}</div>
                    <div class='award-name'>{name}</div>
                    <div class='award-detail'>{detail}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )


def render_player_tournament_view(tournament, user):
    st.subheader(tournament["name"])
    kpis = tournament_kpis(tournament["id"])

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Jugadores", kpis["Jugadores"])
    c2.metric("Finalizadas", kpis["Finalizadas"])
    c3.metric("Pendientes", kpis["Pendientes"])
    c4.metric("Revisión", kpis["Revisión"])

    tabs = st.tabs(["Tabla", "Mis cruces", "Rondas", "Playoffs", "Destacados"])

    with tabs[0]:
        st.dataframe(standings(tournament["id"]), use_container_width=True, hide_index=True)

    with tabs[1]:
        rows = player_match_rows(tournament["id"], user["id"])
        if not rows:
            st.info("Todavía no tenés cruces cargados.")
        else:
            out = []
            for m in rows:
                ws, bs = match_score_parts(m.get("result"), m.get("result_type"))
                out.append({
                    "Ronda": m["number"],
                    "Blancas": m["white_chess"],
                    "Pts": ws,
                    "VS": "vs",
                    "Pts ": bs,
                    "Negras": m["black_chess"] or "LIBRE/BYE",
                    "Estado": match_status_badge(m["status"], m["result_type"]),
                    "Link": m["chesscom_url"] or "",
                })
            st.dataframe(out, use_container_width=True, hide_index=True)

    with tabs[2]:
        rounds_for_view = q("SELECT number FROM rounds WHERE tournament_id=? ORDER BY number", (tournament["id"],))
        if rounds_for_view:
            selected_round = st.selectbox("Ronda", [int(r["number"]) for r in rounds_for_view], key=f"player_round_{tournament['id']}")
            st.dataframe(round_visual_rows(tournament["id"], selected_round), use_container_width=True, hide_index=True)
        else:
            st.info("Todavía no hay rondas.")

    with tabs[3]:
        render_playoff_bracket(tournament["id"])

    with tabs[4]:
        render_awards(tournament["id"])



def create_scan_job(tournament_id, round_number, started_by, total_items):
    return insert_returning(
        "scan_jobs",
        ["tournament_id", "round_number", "started_by", "status", "total_items", "processed_items", "detected_items", "review_items"],
        [tournament_id, round_number, started_by, "running", total_items, 0, 0, 0],
    )


def finish_scan_job(job_id, status="finished"):
    exec_sql("UPDATE scan_jobs SET status=?, finished_at=? WHERE id=?", (status, dt.datetime.now(), job_id))


def add_scan_item(job_id, match_id, cruce, status, detail="", chesscom_url=""):
    return insert_returning(
        "scan_job_items",
        ["job_id", "match_id", "cruce", "status", "detail", "chesscom_url", "updated_at"],
        [job_id, match_id, cruce, status, detail, chesscom_url, dt.datetime.now()],
    )


def update_scan_job_counts(job_id):
    counts = q("""
        SELECT
            COUNT(*) AS processed,
            SUM(CASE WHEN status='detectada' THEN 1 ELSE 0 END) AS detected,
            SUM(CASE WHEN status='revision' THEN 1 ELSE 0 END) AS review
        FROM scan_job_items
        WHERE job_id=?
    """, (job_id,), one=True)

    exec_sql("""
        UPDATE scan_jobs
        SET processed_items=?, detected_items=?, review_items=?
        WHERE id=?
    """, (
        int(counts.get("processed") or 0),
        int(counts.get("detected") or 0),
        int(counts.get("review") or 0),
        job_id,
    ))


def latest_scan_jobs(tournament_id, limit=5):
    return q("""
        SELECT * FROM scan_jobs
        WHERE tournament_id=?
        ORDER BY id DESC
        LIMIT ?
    """, (tournament_id, limit))


def scan_job_items(job_id):
    return q("""
        SELECT cruce,status,detail,chesscom_url,updated_at
        FROM scan_job_items
        WHERE job_id=?
        ORDER BY id
    """, (job_id,))


def pending_matches_for_scan(tournament_id, round_number=None):
    params = [tournament_id]
    round_filter = ""
    if round_number:
        round_filter = " AND r.number=? "
        params.append(round_number)

    return q(f"""
        SELECT m.*, r.number, r.start_datetime, r.end_datetime,
               wu.chesscom_user AS white_chess,
               bu.chesscom_user AS black_chess
        FROM matches m
        JOIN rounds r ON r.id=m.round_id
        JOIN users wu ON wu.id=m.white_user_id
        LEFT JOIN users bu ON bu.id=m.black_user_id
        WHERE m.tournament_id=?
          AND m.status='pending'
          AND m.locked=0
          AND m.black_user_id IS NOT NULL
          {round_filter}
        ORDER BY r.number, m.id
    """, tuple(params))


def _user_chess_names(user_id):
    """Devuelve el set de usernames de Chess.com de un usuario (principal + aliases)."""
    user = get_user(user_id)
    names = set()
    if user and user.get("chesscom_user"):
        names.add(norm(user["chesscom_user"]))
    aliases = q("SELECT alias FROM chess_aliases WHERE user_id=?", (user_id,))
    for a in aliases:
        names.add(norm(a["alias"]))
    return names


def scan_single_match_for_job(tournament, match):
    if match.get("black_user_id") is None or is_bye_value(match.get("black_chess")):
        exec_sql(
            "UPDATE matches SET status='finished', result='BYE', result_type='bye', locked=1, black_user_id=NULL, detected_at=? WHERE id=?",
            (dt.datetime.now(), match["id"]),
        )
        return "bye", "bye automático: el jugador gana sin modificar Elo", ""

    start_dt = parse_db_datetime(match["start_datetime"])
    end_dt = parse_db_datetime(match["end_datetime"])

    white_exists = chess_user_exists(match["white_chess"])
    black_exists = chess_user_exists(match["black_chess"])

    if not white_exists or not black_exists:
        missing = []
        if not white_exists:
            missing.append(match["white_chess"])
        if not black_exists:
            missing.append(match["black_chess"])
        return "error", "usuario inexistente en Chess.com: " + ", ".join(missing), ""

    # Precargar aliases una sola vez — evita N queries a Supabase por cada partida analizada
    white_names = _user_chess_names(match["white_user_id"])
    black_names = _user_chess_names(match["black_user_id"])

    games = chess_games_between(match["white_chess"], start_dt, end_dt)
    games += chess_games_between(match["black_chess"], start_dt, end_dt)

    unique = []
    seen = set()
    for game in games:
        gid = game.get("uuid") or game.get("url")
        if gid in seen:
            continue
        seen.add(gid)
        unique.append(game)

    final_reason = "sin partidas de los jugadores en el rango"
    found_players_game = False

    for game in unique:
        if match.get("rejected_game_uuid") and game.get("uuid") == match.get("rejected_game_uuid"):
            final_reason = "partida invertida rechazada previamente"
            found_players_game = True
            continue

        actual_white = norm(game.get("white", {}).get("username"))
        actual_black = norm(game.get("black", {}).get("username"))
        exact    = actual_white in white_names and actual_black in black_names
        inverted = actual_white in black_names and actual_black in white_names

        if not exact and not inverted:
            if not found_players_game:
                final_reason = "sin partidas de los jugadores en el rango"
            continue

        found_players_game = True

        ok, reason = validate_game_without_color(game, tournament, start_dt, end_dt)
        if not ok:
            final_reason = reason
            continue

        # Calcular score usando los sets precargados (sin DB)
        actual_score_white = score_for_white(game)
        if actual_score_white is None:
            final_reason = "partida encontrada sin resultado interpretable"
            continue

        score = actual_score_white if actual_white in white_names else (
            1.0 - actual_score_white if actual_score_white != 0.5 else 0.5
        )
        result = result_label(score)

        if exact:
            exec_sql("""
                UPDATE matches
                SET status='finished', result=?, result_type='normal',
                    chesscom_url=?, game_uuid=?, locked=1, detected_at=?
                WHERE id=?
            """, (result, game.get("url"), game.get("uuid"), dt.datetime.now(), match["id"]))
            apply_elo(match["id"], match["white_user_id"], match["black_user_id"], score)
            return "detectada", f"resultado cargado: {result}", game.get("url") or ""

        if inverted:
            if mark_color_review(match, game):
                return "revision", "colores invertidos: requiere aceptar o rechazar", game.get("url") or ""
            final_reason = "colores invertidos: pendiente de revisión"

    return "pendiente", final_reason, ""


def prefetch_player_games(usernames, start_dt, end_dt):
    from concurrent.futures import ThreadPoolExecutor
    valid = [norm(u) for u in usernames if valid_chess_username(u)]
    def _fetch(u):
        chess_profile(u)  # calienta cache para chess_user_exists en el loop
        chess_games_between(u, start_dt, end_dt)
    with ThreadPoolExecutor(max_workers=3) as ex:
        list(ex.map(_fetch, valid))


def run_scan_job(tournament_id, round_number, admin_user_id, progress_placeholder=None):
    tournament = q("SELECT * FROM tournaments WHERE id=?", (tournament_id,), one=True)
    if not tournament:
        raise ValueError("Torneo no encontrado.")

    matches = pending_matches_for_scan(tournament_id, round_number)
    if not matches:
        job_id = create_scan_job(tournament_id, round_number, admin_user_id, 0)
        add_scan_item(job_id, None, "-", "sin_pendientes", "No hay cruces pendientes. No se consultó Chess.com.", "")
        update_scan_job_counts(job_id)
        finish_scan_job(job_id)
        return job_id

    # Pre-cargar todos los jugadores en paralelo antes del loop
    all_users = set()
    min_start = None
    max_end = None
    for m in matches:
        if m["white_chess"]:
            all_users.add(m["white_chess"])
        if m["black_chess"]:
            all_users.add(m["black_chess"])
        s = parse_db_datetime(m["start_datetime"])
        e = parse_db_datetime(m["end_datetime"])
        if min_start is None or s < min_start:
            min_start = s
        if max_end is None or e > max_end:
            max_end = e

    if progress_placeholder is not None:
        progress_placeholder.info(f"Pre-cargando partidas de {len(all_users)} jugadores en Chess.com...")
    prefetch_player_games(all_users, min_start, max_end)

    job_id = create_scan_job(tournament_id, round_number, admin_user_id, len(matches))

    for idx, match in enumerate(matches, start=1):
        cruce = f"R{match['number']} | {match['white_chess']} vs {match['black_chess']}"
        add_scan_item(job_id, match["id"], cruce, "buscando", "Consultando Chess.com...", "")
        update_scan_job_counts(job_id)

        if progress_placeholder is not None:
            progress_placeholder.info(f"Buscando {idx}/{len(matches)}: {cruce}")

        try:
            status, detail, url = scan_single_match_for_job(tournament, match)
        except Exception as exc:
            status, detail, url = "error", str(exc), ""

        add_scan_item(job_id, match["id"], cruce, status, detail, url)
        update_scan_job_counts(job_id)

    finish_scan_job(job_id)
    return job_id


def render_motor_panel():
    st.header("Motor Chess.com")
    st.caption("Este panel es solo para admin. La vista jugador siempre muestra la última información guardada y no ve este proceso.")

    tournaments = q("SELECT id,name,status,time_class,time_control FROM tournaments ORDER BY id DESC")
    if not tournaments:
        st.info("No hay torneos.")
        return

    labels = {f"{t['name']} — {t['status']} — {t['time_class']} {t['time_control']}": t["id"] for t in tournaments}
    selected = st.selectbox("Torneo", list(labels.keys()), key="motor_tournament")
    tid = labels[selected]

    rounds = q("SELECT number,start_datetime,end_datetime FROM rounds WHERE tournament_id=? ORDER BY number", (tid,))
    round_options = ["Todo el torneo"] + [f"Ronda {int(r['number'])}" for r in rounds]
    round_selected = st.selectbox("Alcance", round_options, key="motor_round")

    round_number = None
    if round_selected.startswith("Ronda"):
        round_number = int(round_selected.split(" ")[1])

    pending = pending_matches_for_scan(tid, round_number)
    st.metric("Cruces pendientes a escanear", len(pending))

    if len(pending) == 0:
        st.success("No hay cruces pendientes. No hace falta consultar Chess.com.")

    progress_box = st.empty()

    if st.button("Ejecutar motor ahora", key="run_scan_job"):
        with st.spinner("Ejecutando motor. La vista jugador no se bloquea para otros usuarios."):
            job_id = run_scan_job(tid, round_number, current_user["id"], progress_box)
        st.success(f"Motor finalizado. Job #{job_id}")
        st.rerun()

    st.subheader("Últimas búsquedas")
    jobs = latest_scan_jobs(tid, 5)
    if not jobs:
        st.info("Todavía no hay búsquedas registradas.")
        return

    st.dataframe(jobs, use_container_width=True, hide_index=True)

    selected_job_label = st.selectbox(
        "Ver detalle de búsqueda",
        [f"Job {j['id']} — {j['status']} — {j['processed_items']}/{j['total_items']}" for j in jobs],
        key="scan_job_detail_select",
    )
    selected_job_id = int(selected_job_label.split(" ")[1])
    items = scan_job_items(selected_job_id)
    st.dataframe(items, use_container_width=True, hide_index=True)



# =========================================================
# V10.3 FAST VIEWS / REVIEWS / SANCTIONS
# =========================================================

@st.cache_data(ttl=60)
def cached_standings_for_tournament(tournament_id, revision_key=0):
    return standings(tournament_id)


def tournament_revision_key(tournament_id):
    row = q("""
        SELECT COUNT(*) AS c FROM matches
        WHERE tournament_id=? AND status='finished'
    """, (tournament_id,), one=True)
    return int(row["c"] if row else 0)


def fast_standings(tournament_id):
    return cached_standings_for_tournament(tournament_id, tournament_revision_key(tournament_id))


def request_match_review(match_id, requested_by):
    existing = q("""
        SELECT id FROM review_reports
        WHERE match_id=? AND status IN ('requested','running')
        ORDER BY id DESC
        LIMIT 1
    """, (match_id,), one=True)
    if existing:
        return existing["id"]

    return insert_returning(
        "review_reports",
        ["match_id", "requested_by", "status", "report_text"],
        [match_id, requested_by, "requested", "Revisión solicitada. Pendiente de análisis admin/Stockfish."],
    )


def review_reports_for_match(match_id):
    return q("""
        SELECT rr.*, u.display_name AS requested_by_name
        FROM review_reports rr
        LEFT JOIN users u ON u.id=rr.requested_by
        WHERE rr.match_id=?
        ORDER BY rr.id DESC
    """, (match_id,))


def requested_reviews():
    return q("""
        SELECT rr.*, m.tournament_id, m.result, m.chesscom_url,
               wu.display_name AS white_name, wu.chesscom_user AS white_chess,
               bu.display_name AS black_name, bu.chesscom_user AS black_chess,
               t.name AS tournament_name
        FROM review_reports rr
        JOIN matches m ON m.id=rr.match_id
        JOIN tournaments t ON t.id=m.tournament_id
        JOIN users wu ON wu.id=m.white_user_id
        LEFT JOIN users bu ON bu.id=m.black_user_id
        WHERE rr.status IN ('requested','running')
        ORDER BY rr.id DESC
    """)


def warning_count(tournament_id, user_id):
    row = q("""
        SELECT COUNT(*) AS c FROM player_warnings
        WHERE tournament_id=? AND user_id=?
    """, (tournament_id, user_id), one=True)
    return int(row["c"] if row else 0)


def add_warning_and_maybe_disqualify(tournament_id, user_id, match_id, warning_type, reason, admin_user_id):
    insert_returning(
        "player_warnings",
        ["tournament_id", "user_id", "match_id", "warning_type", "reason", "created_by"],
        [tournament_id, user_id, match_id, warning_type, reason, admin_user_id],
    )
    cnt = warning_count(tournament_id, user_id)
    if cnt >= 2:
        exec_sql("""
            UPDATE registrations
            SET status='disqualified'
            WHERE tournament_id=? AND user_id=?
        """, (tournament_id, user_id))
    return cnt


def apply_sanction_00(match_id, sanctioned_user_id, admin_user_id, reason):
    match = q("SELECT * FROM matches WHERE id=?", (match_id,), one=True)
    if not match:
        raise ValueError("Partida no encontrada.")

    if sanctioned_user_id not in (match["white_user_id"], match["black_user_id"]):
        raise ValueError("El jugador sancionado no pertenece a esta partida.")

    # Resultado reglamentario: 0-0. Nadie suma punto. Solo recibe advertencia el sancionado.
    exec_sql("""
        UPDATE matches
        SET status='finished', result='0-0', result_type='sanction_00',
            locked=1, detected_at=?
        WHERE id=?
    """, (dt.datetime.now(), match_id))

    cnt = add_warning_and_maybe_disqualify(
        match["tournament_id"],
        sanctioned_user_id,
        match_id,
        "sanction",
        reason,
        admin_user_id,
    )

    exec_sql("""
        UPDATE review_reports
        SET status='decided', reviewed_by=?, decision=?, sanctioned_user_id=?, decided_at=?
        WHERE match_id=? AND status IN ('requested','running')
    """, (admin_user_id, "sanction_00", sanctioned_user_id, dt.datetime.now(), match_id))

    audit(admin_user_id, "sanction_00", f"match={match_id}, sanctioned={sanctioned_user_id}, warnings={cnt}")
    return cnt


def mark_review_no_sanction(match_id, admin_user_id, note="Resultado mantenido. Sin sanción."):
    exec_sql("""
        UPDATE review_reports
        SET status='decided', reviewed_by=?, decision='no_sanction', report_text=?, decided_at=?
        WHERE match_id=? AND status IN ('requested','running')
    """, (admin_user_id, note, dt.datetime.now(), match_id))
    audit(admin_user_id, "review_no_sanction", f"match={match_id}")


def player_warnings_table(tournament_id):
    return q("""
        SELECT pw.*, u.display_name, u.chesscom_user
        FROM player_warnings pw
        JOIN users u ON u.id=pw.user_id
        WHERE pw.tournament_id=?
        ORDER BY pw.created_at DESC
    """, (tournament_id,))


def render_review_panel():
    st.header("Revisiones / Sanciones")
    st.caption("La revisión se aplica sobre una partida puntual. El resultado sanción es 0-0 y solo recibe advertencia el jugador sancionado.")

    reqs = requested_reviews()
    if not reqs:
        st.success("No hay revisiones pendientes.")
        return

    for r in reqs:
        with st.expander(f"#{r['id']} · {r['tournament_name']} · {r['white_chess']} vs {r['black_chess']}", expanded=True):
            st.write(f"Resultado actual: **{r['result']}**")
            if r.get("chesscom_url"):
                st.write(r["chesscom_url"])

            st.info("Informe automático completo con Stockfish queda para el siguiente módulo. Acá ya podés decidir reglamentariamente.")

            c1, c2 = st.columns(2)
            sanctioned_label = c1.selectbox(
                "Jugador sancionado",
                [
                    f"{r['white_name']} ({r['white_chess']})",
                    f"{r['black_name']} ({r['black_chess']})",
                ],
                key=f"sanction_player_{r['id']}",
            )
            sanctioned_id = None
            if sanctioned_label.startswith(str(r["white_name"])):
                # match needs ids, fetch match
                m = q("SELECT * FROM matches WHERE id=?", (r["match_id"],), one=True)
                sanctioned_id = m["white_user_id"]
            else:
                m = q("SELECT * FROM matches WHERE id=?", (r["match_id"],), one=True)
                sanctioned_id = m["black_user_id"]

            reason = c2.text_input("Motivo", value="Revisión admin / conducta antirreglamentaria", key=f"sanction_reason_{r['id']}")

            a, b = st.columns(2)
            if a.button("Aplicar 0-0 y advertencia", key=f"apply_sanction_{r['id']}"):
                try:
                    cnt = apply_sanction_00(r["match_id"], sanctioned_id, current_user["id"], reason)
                    if cnt >= 2:
                        st.warning(f"Sanción aplicada. El jugador llegó a {cnt} advertencias y queda descalificado.")
                    else:
                        st.success(f"Sanción aplicada. Advertencias del jugador: {cnt}/2.")
                    st.rerun()
                except Exception as exc:
                    st.error(exc)

            if b.button("Mantener resultado / sin sanción", key=f"no_sanction_{r['id']}"):
                try:
                    mark_review_no_sanction(r["match_id"], current_user["id"])
                    st.success("Resultado mantenido. Revisión cerrada sin sanción.")
                    st.rerun()
                except Exception as exc:
                    st.error(exc)


def render_fast_player_tournament_view(tournament, user):
    st.subheader(tournament["name"])
    kpis = tournament_kpis(tournament["id"])

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Jugadores", kpis["Jugadores"])
    c2.metric("Finalizadas", kpis["Finalizadas"])
    c3.metric("Pendientes", kpis["Pendientes"])
    c4.metric("Revisión", kpis["Revisión"])

    tabs = st.tabs(["Tabla", "Mis cruces", "Rondas", "Playoffs", "Destacados"])

    with tabs[0]:
        st.dataframe(fast_standings(tournament["id"]), use_container_width=True, hide_index=True)

    with tabs[1]:
        rows = player_match_rows(tournament["id"], user["id"])
        if not rows:
            st.info("Todavía no tenés cruces cargados.")
        else:
            out = []
            for m in rows:
                ws, bs = match_score_parts(m.get("result"), m.get("result_type"))
                out.append({
                    "Ronda": m["number"],
                    "Blancas": m["white_chess"],
                    "Pts": ws,
                    "VS": "vs",
                    "Pts ": bs,
                    "Negras": m["black_chess"] or "LIBRE/BYE",
                    "Estado": match_status_badge(m["status"], m["result_type"]),
                    "Link": m["chesscom_url"] or "",
                })
            st.dataframe(out, use_container_width=True, hide_index=True)

            finished_with_links = [m for m in rows if m.get("chesscom_url") and m["status"] == "finished"]
            if finished_with_links:
                st.markdown("**Solicitar revisión de una partida**")
                labels = {
                    f"R{m['number']} | {m['white_chess']} vs {m['black_chess']} | {m['result']}": m["id"]
                    for m in finished_with_links
                }
                sel = st.selectbox("Partida a reportar", list(labels.keys()), key=f"request_review_{tournament['id']}_{user['id']}")
                if st.button("Solicitar revisión", key=f"btn_request_review_{tournament['id']}_{user['id']}"):
                    rid = request_match_review(labels[sel], user["id"])
                    st.success(f"Revisión solicitada. ID #{rid}. Un admin decidirá si corresponde analizar/sancionar.")

    with tabs[2]:
        rounds_for_view = q("SELECT number FROM rounds WHERE tournament_id=? ORDER BY number", (tournament["id"],))
        if rounds_for_view:
            selected_round = st.selectbox("Ronda", [int(r["number"]) for r in rounds_for_view], key=f"fast_round_{tournament['id']}")
            st.dataframe(round_visual_rows(tournament["id"], selected_round), use_container_width=True, hide_index=True)
        else:
            st.info("Todavía no hay rondas.")

    with tabs[3]:
        render_playoff_bracket(tournament["id"])

    with tabs[4]:
        render_awards(tournament["id"])


# =========================================================
# SCHEDULER AUTOMÁTICO (cada 30 minutos)
# =========================================================

import threading as _threading

_scheduler_lock = _threading.Lock()
_scheduler_started = False


def _auto_scan_all():
    try:
        torneos = q("SELECT id FROM tournaments WHERE status='playing'")
        for t in torneos:
            try:
                logger.info("Scheduler: escaneando torneo %s", t["id"])
                scan_tournament(t["id"])
            except Exception as e:
                logger.exception("Scheduler: error en torneo %s: %s", t["id"], e)
    except Exception as e:
        logger.exception("Scheduler: error obteniendo torneos: %s", e)


def _scheduler_loop():
    import time as _time
    while True:
        _time.sleep(1800)  # 30 minutos
        try:
            _auto_scan_all()
        except Exception as e:
            logger.exception("Scheduler: error inesperado: %s", e)


def _start_scheduler():
    global _scheduler_started
    with _scheduler_lock:
        if not _scheduler_started:
            _scheduler_started = True
            t = _threading.Thread(target=_scheduler_loop, daemon=True)
            t.start()
            logger.info("Scheduler iniciado: escaneo automático cada 30 minutos")


_start_scheduler()


# =========================================================
# UI
# =========================================================

if "db_initialized" not in st.session_state:
    init_db()
    ensure_v9_columns()
    st.session_state.db_initialized = True

if "user" not in st.session_state:
    st.session_state.user = None

st.markdown("""
<style>
.block-container {max-width: 1320px;}
.login-box {max-width: 440px; margin: 0 auto;}
.login-box [data-testid="stTextInput"] {max-width: 440px;}
.login-box .stButton button {width: 180px;}
.bracket-box {
    border: 1px solid #d7dce2;
    border-radius: 12px;
    padding: 10px 12px;
    margin: 8px 0;
    background: linear-gradient(180deg, #ffffff, #f7f9fc);
    box-shadow: 0 1px 3px rgba(0,0,0,.05);
}
.cup-center {
    text-align: center;
    font-size: 28px;
    border: 1px solid #d7dce2;
    border-radius: 16px;
    padding: 24px 8px;
    margin-top: 36px;
    background: #fff8e6;
}
.award-card {
    border: 1px solid #d7dce2;
    border-radius: 16px;
    padding: 16px;
    margin: 8px 0;
    background: linear-gradient(180deg, #ffffff, #f5f7fb);
    box-shadow: 0 1px 4px rgba(0,0,0,.06);
    min-height: 120px;
}
.award-title {font-size: 15px; font-weight: 700; color: #555;}
.award-name {font-size: 22px; font-weight: 800; margin-top: 8px;}
.award-detail {font-size: 14px; color: #666; margin-top: 6px;}
</style>
""", unsafe_allow_html=True)

st.title("♟️ Torneos de Ajedrez — V10.3 TEST reglamento + vista rápida")

if use_postgres():
    st.sidebar.success("DB: Supabase/PostgreSQL")
else:
    st.sidebar.warning("DB: SQLite local fallback")

if not st.session_state.user:
    st.markdown('<div class="login-box">', unsafe_allow_html=True)
    tab_login, tab_reg = st.tabs(["Ingresar", "Crear/Reclamar cuenta"])

    with tab_login:
        st.caption("Si el admin ya creó tu perfil, ingresá con tu usuario de Chess.com y contraseña 12345.")
        username = st.text_input("Usuario Chess.com", key="login_username")
        password = st.text_input("Contraseña", type="password", key="login_password")
        if st.button("Ingresar"):
            user, error = login(username, password)
            if user:
                st.session_state.user = user
                st.rerun()
            else:
                st.error(error)

    with tab_reg:
        chess = st.text_input("Usuario Chess.com", key="reg_chess")
        display = st.text_input("Nombre visible", key="reg_display")
        password = st.text_input("Contraseña", type="password", key="reg_password")
        if st.button("Crear/Reclamar cuenta"):
            try:
                if not chess or not password:
                    st.warning("Completá usuario Chess.com y contraseña.")
                else:
                    create_or_claim_user(chess, password, chess, display or chess)
                    st.success("Cuenta creada/reclamada. Ahora ingresá.")
            except Exception as exc:
                st.error(exc)

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
            update_password(current_user["id"], p1, 0)
            st.success("Contraseña actualizada.")
            st.rerun()
        else:
            st.error("Las contraseñas no coinciden.")
    st.stop()

menu = ["Inicio", "Torneos abiertos", "Mis torneos", "Ranking", "Mi perfil"]
if is_staff(current_user):
    menu.append("⚙ Panel Admin")

choice = st.sidebar.radio("Menú", menu)

admin_choice = None
if choice == "⚙ Panel Admin":
    admin_choice = st.sidebar.radio(
        "Panel Admin",
        ["Torneos Admin", "Motor Chess.com", "Revisiones", "Crear torneo", "Importar fixture", "Importar rondas", "Admin usuarios"]
    )


if choice == "Inicio":
    st.header("Inicio")
    st.caption("Vista jugador: torneos, cruces, tablas y destacados sin mostrar procesos internos.")

    myts = my_tournaments(current_user["id"])
    if not myts:
        st.info("Todavía no estás inscripto en ningún torneo. Entrá a Torneos abiertos para inscribirte.")
    else:
        for t in myts[:2]:
            render_fast_player_tournament_view(t, current_user)

elif choice == "Torneos abiertos":
    st.header("Torneos abiertos")
    tours = open_tournaments_for_player(current_user["id"])
    if not tours:
        st.info("No hay torneos abiertos por ahora.")

    for t in tours:
        with st.expander(f"{t['name']} — {t['status']} — {t['time_class']} — {t['time_control']}", expanded=True):
            kpis = tournament_kpis(t["id"])
            c1, c2, c3 = st.columns(3)
            c1.metric("Jugadores", kpis["Jugadores"])
            c2.metric("Cruces", kpis["Cruces"])
            c3.metric("Pendientes", kpis["Pendientes"])

            if is_registered_in_tournament(t["id"], current_user["id"]):
                st.success("Ya estás inscripto.")
            else:
                if st.button("Inscribirme", key=f"join_{t['id']}"):
                    register_current_player(t["id"], current_user["id"])
                    st.success("Inscripción realizada.")
                    st.rerun()

            st.markdown("**Tabla actual**")
            st.dataframe(fast_standings(t["id"]), use_container_width=True, hide_index=True)

elif choice == "Mis torneos":
    st.header("Mis torneos")
    tours = my_tournaments(current_user["id"])
    if not tours:
        st.info("No estás inscripto en ningún torneo todavía.")
    for t in tours:
        with st.expander(f"{t['name']} — {t['status']}", expanded=True):
            render_fast_player_tournament_view(t, current_user)

if admin_choice == "Motor Chess.com":
    render_motor_panel()

elif admin_choice == "Revisiones":
    render_review_panel()

elif admin_choice == "Crear torneo":
    st.header("Crear torneo vacío/manual")
    name = st.text_input("Nombre del torneo")
    desc = st.text_area("Descripción")

    c1, c2, c3 = st.columns(3)
    rules = "chess" if c1.selectbox("Modalidad", ["Ajedrez normal", "Chess960"]) == "Ajedrez normal" else "chess960"
    time_class = c2.selectbox("Clase", ["rapid", "blitz", "bullet", "daily"])
    time_control = c3.text_input("Ritmo exacto", value="600")

    rated_filter = {"Cualquiera": "any", "Solo rated": "rated", "Solo casual": "casual"}[
        st.selectbox("Rated/Casual", ["Cualquiera", "Solo rated", "Solo casual"])
    ]
    strict_colors = st.checkbox("Respetar colores exactos", value=True)

    st.subheader("Estructura del torneo")
    et1, et2, et3, et4 = st.columns(4)
    rounds_count = et1.number_input("Cantidad de rondas", value=5, min_value=1, step=1)
    cups_count = et2.number_input("Cantidad de copas", value=3, min_value=1, step=1)
    qualifiers_count = et3.number_input("Clasificados a playoffs", value=24, min_value=2, step=1)
    cup_size = et4.number_input("Jugadores por copa", value=8, min_value=2, step=2)

    st.subheader("Sistema de puntos")
    pp1, pp2, pp3, pp4, pp5 = st.columns(5)
    win_points = pp1.number_input("Victoria", value=1.0, step=0.5, key="ct_win")
    draw_points = pp2.number_input("Empate", value=0.5, step=0.5, key="ct_draw")
    loss_points = pp3.number_input("Derrota", value=0.0, step=0.5, key="ct_loss")
    bye_points = pp4.number_input("Libre/BYE", value=1.0, step=0.5, key="ct_bye")
    wo_points = pp5.number_input("WO", value=0.0, step=0.5, key="ct_wo")

    if st.button("Crear torneo"):
        if not name.strip():
            st.warning("Poné un nombre.")
        else:
            tid = create_empty_tournament(name.strip(), desc, rules, time_class, time_control, rated_filter, strict_colors, current_user["id"])
            exec_sql("""
                UPDATE tournaments
                SET win_points=?, draw_points=?, loss_points=?, bye_points=?, wo_points=?,
                    rounds_count=?, cups_count=?, qualifiers_count=?, cup_size=?
                WHERE id=?
            """, (win_points, draw_points, loss_points, bye_points, wo_points,
                  rounds_count, cups_count, qualifiers_count, cup_size, tid))
            st.success(f"Torneo creado. ID: {tid}")

elif admin_choice == "Importar fixture":
    st.header("Importar fixture completo")
    st.write("Columnas: `torneo`, `ronda`, `fecha_inicio`, `fecha_fin`, `blancas_chesscom`, `negras_chesscom`. Para jugador libre usá `LIBRE`, `BYE`, vacío o `0` en negras_chesscom.")

    template = pd.DataFrame([{
        "torneo": "TORNEO N°11",
        "ronda": 1,
        "fecha_inicio": "27/02/2026 00:00",
        "fecha_fin": "03/04/2026 23:59",
        "blancas_chesscom": "pabloroldan",
        "negras_chesscom": "matiasbulacio",
    }])
    st.download_button("Descargar plantilla", template.to_csv(index=False).encode("utf-8"), "fixture.csv", "text/csv")

    c1, c2, c3 = st.columns(3)
    rules = "chess" if c1.selectbox("Modalidad", ["Ajedrez normal", "Chess960"], key="if_rules") == "Ajedrez normal" else "chess960"
    time_class = c2.selectbox("Clase", ["rapid", "blitz", "bullet", "daily"], key="if_class")
    time_control = c3.text_input("Ritmo exacto", value="600", key="if_time")

    rated_filter = {"Cualquiera": "any", "Solo rated": "rated", "Solo casual": "casual"}[
        st.selectbox("Rated/Casual", ["Cualquiera", "Solo rated", "Solo casual"], key="if_rated")
    ]
    strict_colors = st.checkbox("Respetar colores exactos", value=True, key="if_strict")

    uploaded = st.file_uploader("Subir CSV", type=["csv"], key="fixture_csv")
    if uploaded:
        try:
            df = read_uploaded_csv(uploaded)
            st.dataframe(df, use_container_width=True)
            if st.button("Importar fixture"):
                matches, players = import_fixture_csv(df, current_user["id"], rules, time_class, time_control, rated_filter, strict_colors)
                st.success(f"Fixture importado. Cruces: {matches}. Jugadores nuevos: {players}.")
        except Exception as exc:
            st.error(exc)

elif admin_choice == "Importar rondas":
    st.header("Importar rondas a torneo existente")
    tournaments = q("SELECT id,name,time_class,time_control FROM tournaments ORDER BY id DESC")
    if not tournaments:
        st.info("Primero creá o importá un torneo.")
    else:
        labels = {f"{t['name']} — {t['time_class']} — {t['time_control']}": t["id"] for t in tournaments}
        selected = st.selectbox("Torneo", list(labels.keys()))
        tid = labels[selected]

        template = pd.DataFrame([{
            "ronda": 1,
            "fecha_inicio": "27/02/2026 00:00",
            "fecha_fin": "03/04/2026 23:59",
            "blancas_chesscom": "pabloroldan",
            "negras_chesscom": "matiasbulacio",
        }])
        st.download_button("Descargar plantilla rondas", template.to_csv(index=False).encode("utf-8"), "rondas.csv", "text/csv")

        uploaded = st.file_uploader("Subir CSV de rondas", type=["csv"], key="rounds_csv")
        if uploaded:
            try:
                df = read_uploaded_csv(uploaded)
                st.dataframe(df, use_container_width=True)
                if st.button("Importar rondas"):
                    matches, players = import_rounds_to_existing_tournament(df, tid)
                    st.success(f"Rondas importadas. Cruces: {matches}. Jugadores nuevos: {players}.")
            except Exception as exc:
                st.error(exc)

elif admin_choice == "Torneos Admin":
    st.header("Torneos Admin / Control")
    tournaments = q("SELECT * FROM tournaments ORDER BY id DESC")
    if not tournaments:
        st.info("Todavía no hay torneos.")
    else:
        STATUS_BADGE = {"playing": "🟢 Jugando", "open": "🟡 Abierto", "finished": "⚫ Finalizado"}

        t_labels = {f"{t['name']}  —  {STATUS_BADGE.get(t['status'], t['status'])}": t for t in tournaments}
        sel_label = st.selectbox("Seleccionar torneo", list(t_labels.keys()))
        t = t_labels[sel_label]

        badge = STATUS_BADGE.get(t["status"], t["status"])
        st.markdown(f"**{t['name']}** &nbsp; {badge} &nbsp;·&nbsp; {t['time_class']} &nbsp;·&nbsp; {t['time_control']}")

        review_count = len(q("SELECT id FROM matches WHERE tournament_id=? AND status='review'", (t["id"],)))
        gestión_label = f"🎯 Gestión  ({review_count})" if review_count > 0 else "🎯 Gestión"

        pending_wa = q("""
            SELECT m.id, r.number AS round_num, r.end_datetime,
                   wu.display_name AS white_name, wu.chesscom_user AS white_chess, wu.celular AS white_cel,
                   bu.display_name AS black_name, bu.chesscom_user AS black_chess, bu.celular AS black_cel
            FROM matches m
            JOIN rounds r ON r.id=m.round_id
            JOIN users wu ON wu.id=m.white_user_id
            LEFT JOIN users bu ON bu.id=m.black_user_id
            WHERE m.tournament_id=? AND m.status='pending' AND m.locked=0
              AND m.black_user_id IS NOT NULL
            ORDER BY r.number, m.id
        """, (t["id"],))
        avisos_label = f"📲 Avisos  ({len(pending_wa)})" if pending_wa else "📲 Avisos"

        tab_res, tab_gest, tab_avisos, tab_cruces, tab_playoffs, tab_cfg = st.tabs([
            "📊 Resumen", gestión_label, avisos_label, "📋 Cruces", "🏆 Playoffs", "⚙️ Config"
        ])

        # ── TAB RESUMEN ──────────────────────────────────────────────
        with tab_res:
            kpis = tournament_kpis(t["id"])
            mc1, mc2, mc3, mc4, mc5 = st.columns(5)
            mc1.metric("Jugadores", kpis["Jugadores"])
            mc2.metric("Total cruces", kpis["Cruces"])
            mc3.metric("Finalizadas", kpis["Finalizadas"])
            mc4.metric("Pendientes", kpis["Pendientes"])
            mc5.metric("Revisión", kpis["Revisión"])

            if review_count:
                st.warning(f"⚠️ {review_count} partida(s) esperan revisión de colores — ver tab Gestión.")

            st.divider()
            rb1, rb2 = st.columns(2)
            if rb1.button("🔍 Buscar resultados Chess.com", key=f"scan_res_{t['id']}"):
                found, debug = safe_scan_tournament(t["id"])
                st.success(f"Detectadas: {found}")
                st.dataframe(debug, use_container_width=True)
            if rb2.button("⏱ Aplicar WO vencidos", key=f"wo_res_{t['id']}"):
                applied = apply_wo_expired(t["id"])
                st.warning(f"WO aplicados: {applied}")
                st.rerun()

            st.divider()
            if t["status"] != "finished":
                confirm_end = st.checkbox("Confirmar que quiero finalizar este torneo", key=f"confirm_end_{t['id']}")
                if st.button("🏁 Finalizar torneo", key=f"finish_end_{t['id']}", disabled=not confirm_end, type="primary"):
                    exec_sql("UPDATE tournaments SET status='finished' WHERE id=?", (t["id"],))
                    audit(current_user["id"], "finish_tournament", f"tournament={t['id']}")
                    st.success("Torneo finalizado.")
                    st.rerun()
            else:
                st.success("✅ Este torneo está finalizado.")
                if st.button("↩ Reabrir torneo (volver a 'playing')", key=f"reopen_end_{t['id']}"):
                    exec_sql("UPDATE tournaments SET status='playing' WHERE id=?", (t["id"],))
                    audit(current_user["id"], "reopen_tournament", f"tournament={t['id']}")
                    st.warning("Torneo reabierto.")
                    st.rerun()

        # ── TAB GESTIÓN ───────────────────────────────────────────────
        with tab_gest:
            if review_count:
                st.subheader(f"⚠️ Revisiones por colores invertidos ({review_count})")
                st.warning("Estas partidas coinciden pero se jugaron con colores invertidos.")
                reviews = q("""
                    SELECT m.*, r.number,
                           wu.chesscom_user AS white_chess,
                           bu.chesscom_user AS black_chess
                    FROM matches m
                    JOIN rounds r ON r.id=m.round_id
                    JOIN users wu ON wu.id=m.white_user_id
                    LEFT JOIN users bu ON bu.id=m.black_user_id
                    WHERE m.tournament_id=? AND m.status='review'
                    ORDER BY r.number, m.id
                """, (t["id"],))
                for rm in reviews:
                    st.write(f"R{rm['number']} | `{rm['white_chess']} vs {rm['black_chess']}` | Resultado si acepta: **{rm['review_result'] or rm['result']}**")
                    if rm["review_url"]:
                        st.write(rm["review_url"])
                    rca, rcr = st.columns(2)
                    if rca.button("✅ Aceptar partida", key=f"accept_{rm['id']}"):
                        try:
                            accept_color_review(rm["id"], current_user["id"])
                            st.success("Aceptada.")
                            st.rerun()
                        except Exception as exc:
                            st.error(exc)
                    if rcr.button("❌ Rechazar partida", key=f"reject_{rm['id']}"):
                        try:
                            reject_color_review(rm["id"], current_user["id"])
                            st.warning("Rechazada.")
                            st.rerun()
                        except Exception as exc:
                            st.error(exc)
                st.divider()

            st.subheader("Motor Chess.com")
            st.caption("Para búsquedas largas usá Panel Admin → Motor Chess.com, que guarda progreso separado.")
            gb1, gb2 = st.columns(2)
            if gb1.button("🔍 Buscar resultados Chess.com", key=f"scan_g_{t['id']}"):
                found, debug = safe_scan_tournament(t["id"])
                st.success(f"Detectadas: {found}")
                st.dataframe(debug, use_container_width=True)
            if gb2.button("⏱ Aplicar WO vencidos", key=f"wo_g_{t['id']}"):
                applied = apply_wo_expired(t["id"])
                st.warning(f"WO aplicados: {applied}")
                st.rerun()

            auto_g = st.checkbox("Detectar automáticamente cada 1 minuto", key=f"auto_g_{t['id']}")
            if auto_g:
                st.markdown('<meta http-equiv="refresh" content="60">', unsafe_allow_html=True)
                found, debug = safe_scan_tournament(t["id"])
                st.caption(f"Auto-búsqueda ejecutada. Detectadas: {found}")
                st.dataframe(debug, use_container_width=True)

            st.divider()
            st.subheader("Resultado manual")
            all_matches_g = q("""
                SELECT m.*, r.number,
                       wu.chesscom_user AS white_chess,
                       bu.chesscom_user AS black_chess
                FROM matches m
                JOIN rounds r ON r.id=m.round_id
                JOIN users wu ON wu.id=m.white_user_id
                LEFT JOIN users bu ON bu.id=m.black_user_id
                WHERE m.tournament_id=?
                ORDER BY r.number, m.id
            """, (t["id"],))
            if all_matches_g:
                match_labels = {f"R{m['number']} | {m['white_chess']} vs {m['black_chess'] or 'LIBRE/BYE'} | {m['status']} | {m['result'] or 'sin resultado'}": m["id"] for m in all_matches_g}
                sel_m = st.selectbox("Partida", list(match_labels.keys()), key=f"manual_match_g_{t['id']}")
                res_m = st.selectbox("Resultado", ["1-0", "0-1", "1/2-1/2"], key=f"manual_res_g_{t['id']}")
                gm1, gm2 = st.columns(2)
                if gm1.button("💾 Guardar resultado manual", key=f"save_manual_g_{t['id']}"):
                    try:
                        set_manual_result(match_labels[sel_m], res_m, current_user["id"])
                        st.success("Resultado manual guardado.")
                        st.rerun()
                    except Exception as exc:
                        st.error(exc)
                if gm2.button("🗑 Limpiar resultado", key=f"clear_manual_g_{t['id']}"):
                    try:
                        clear_match_result(match_labels[sel_m], current_user["id"])
                        st.warning("Resultado limpiado.")
                        st.rerun()
                    except Exception as exc:
                        st.error(exc)

            st.divider()
            st.subheader("👤 Gestionar inscripciones")

            regs = q("""
                SELECT r.id AS reg_id, r.status AS reg_status, r.wo_count,
                       u.id AS user_id, u.display_name, u.chesscom_user
                FROM registrations r
                JOIN users u ON u.id = r.user_id
                WHERE r.tournament_id=?
                ORDER BY r.status, u.display_name
            """, (t["id"],))

            if regs:
                STATUS_REG = {"active": "✅ Activo", "disqualified": "🚫 Descalificado", "removed": "❌ Retirado"}
                reg_labels = {
                    f"{r['display_name']} ({r['chesscom_user']})  —  {STATUS_REG.get(r['reg_status'], r['reg_status'])}": r
                    for r in regs
                }
                sel_reg = st.selectbox("Jugador", list(reg_labels.keys()), key=f"reg_sel_{t['id']}")
                reg = reg_labels[sel_reg]

                ri1, ri2, ri3 = st.columns(3)
                if ri1.button("✅ Activar", key=f"reg_act_{t['id']}", disabled=reg["reg_status"] == "active"):
                    exec_sql("UPDATE registrations SET status='active' WHERE id=?", (reg["reg_id"],))
                    audit(current_user["id"], "reg_activate", f"tournament={t['id']} user={reg['user_id']}")
                    st.success(f"{reg['display_name']} activado.")
                    st.rerun()

                if ri2.button("❌ Retirar", key=f"reg_rem_{t['id']}", disabled=reg["reg_status"] == "removed"):
                    exec_sql("UPDATE registrations SET status='removed' WHERE id=?", (reg["reg_id"],))
                    audit(current_user["id"], "reg_remove", f"tournament={t['id']} user={reg['user_id']}")
                    st.warning(f"{reg['display_name']} retirado.")
                    st.rerun()

                wo_pending = q("""
                    SELECT m.id FROM matches m
                    WHERE m.tournament_id=? AND m.status='pending' AND m.locked=0
                      AND (m.white_user_id=? OR m.black_user_id=?)
                """, (t["id"], reg["user_id"], reg["user_id"]))

                if wo_pending:
                    if ri3.button(f"⏱ WO {len(wo_pending)} partida(s) pendiente(s)", key=f"reg_wo_{t['id']}"):
                        import datetime as _dt2
                        for wm in wo_pending:
                            match_full = q("SELECT * FROM matches WHERE id=?", (wm["id"],), one=True)
                            if not match_full:
                                continue
                            if match_full["white_user_id"] == reg["user_id"]:
                                result, w_score = "0-1", 0.0
                            else:
                                result, w_score = "1-0", 1.0
                            exec_sql(
                                "UPDATE matches SET status='finished', result=?, result_type='wo', locked=1, detected_at=? WHERE id=?",
                                (result, _dt2.datetime.now(), wm["id"]),
                            )
                            apply_elo(wm["id"], match_full["white_user_id"], match_full["black_user_id"], w_score)
                        audit(current_user["id"], "reg_wo_all", f"tournament={t['id']} user={reg['user_id']} matches={len(wo_pending)}")
                        st.warning(f"WO aplicado a {len(wo_pending)} partida(s).")
                        st.rerun()

            st.divider()
            st.subheader("➕ Inscribir jugador")
            all_users_g = q("SELECT id, display_name, chesscom_user FROM users WHERE account_status='active' ORDER BY display_name")
            registered_ids = {r["user_id"] for r in regs} if regs else set()
            candidates = [u for u in all_users_g if u["id"] not in registered_ids]
            if candidates:
                cand_labels = {f"{u['display_name']} ({u['chesscom_user']})": u["id"] for u in candidates}
                sel_cand = st.selectbox("Usuario a inscribir", list(cand_labels.keys()), key=f"inscribir_sel_{t['id']}")
                if st.button("Inscribir", key=f"inscribir_btn_{t['id']}"):
                    try:
                        register_player(t["id"], cand_labels[sel_cand])
                        audit(current_user["id"], "reg_add", f"tournament={t['id']} user={cand_labels[sel_cand]}")
                        st.success("Jugador inscripto.")
                        st.rerun()
                    except Exception as exc:
                        st.error(exc)
            else:
                st.caption("Todos los usuarios activos ya están inscriptos.")

        # ── TAB AVISOS ────────────────────────────────────────────────
        with tab_avisos:
            if not pending_wa:
                st.success("✅ No hay partidas pendientes en este torneo.")
            else:
                st.caption(f"{len(pending_wa)} partida(s) pendiente(s) · Los botones abren WhatsApp con el mensaje listo para enviar.")

                import re as _re
                from collections import defaultdict
                by_round = defaultdict(list)
                for m in pending_wa:
                    by_round[m["round_num"]].append(m)

                # ── Mensaje para grupo ──────────────────────────────────
                _t_num = _re.search(r'\d+', t["name"])
                _t_abbr = f"T{_t_num.group()}" if _t_num else f"T{t['id']}"

                # Posición de cada partida dentro de su ronda
                _match_pos = {}
                for _rn in by_round:
                    _all = q("""
                        SELECT m.id FROM matches m
                        JOIN rounds r ON r.id=m.round_id
                        WHERE m.tournament_id=? AND r.number=?
                        ORDER BY m.id
                    """, (t["id"], _rn))
                    for _i, _rm in enumerate(_all, 1):
                        _match_pos[_rm["id"]] = _i

                _group_lines = []
                for _rn in sorted(by_round.keys()):
                    for _m in by_round[_rn]:
                        _pos = _match_pos.get(_m["id"], "?")
                        _pos_str = f"P{_pos:02d}" if isinstance(_pos, int) else f"P{_pos}"
                        _wc = "".join(c for c in str(_m["white_cel"]) if c.isdigit()) if _m["white_cel"] else ""
                        _bc = "".join(c for c in str(_m["black_cel"]) if c.isdigit()) if _m["black_cel"] else ""
                        _wa_w = f"@{_wc}" if _wc else f"({_m['white_name']})"
                        _wa_b = f"@{_bc}" if _bc else f"({_m['black_name']})"
                        _group_lines.append(f"{_t_abbr} R{_rn} {_pos_str}  {_m['white_name']} vs {_m['black_name']}")
                        _group_lines.append(f"{_wa_w} {_m['white_chess']} vs {_m['black_chess'] or '—'} {_wa_b}")
                        _group_lines.append("")

                st.text_area(
                    "📋 Copiar para grupo de WhatsApp",
                    value="\n".join(_group_lines).strip(),
                    height=250,
                    key=f"wa_group_{t['id']}",
                )
                st.divider()
                # ── Individual por jugador ──────────────────────────────

                for round_num in sorted(by_round.keys()):
                    matches_r = by_round[round_num]
                    end_dt = parse_db_datetime(matches_r[0]["end_datetime"])
                    fecha_str = end_dt.strftime("%d/%m/%Y %H:%M") if end_dt else "—"
                    st.subheader(f"Ronda {round_num}  ·  vence {fecha_str}")

                    for m in matches_r:
                        msg_w = (
                            f"Hola {m['white_name']}! ♟️ Recordatorio del {t['name']}.\n"
                            f"Tenés una partida pendiente de la Ronda {round_num} "
                            f"que vence el {fecha_str}.\n"
                            f"Tu rival es {m['black_name']} ({m['black_chess'] or '—'}).\n"
                            f"¡Coordiná y jugá antes del vencimiento!"
                        )
                        msg_b = (
                            f"Hola {m['black_name']}! ♟️ Recordatorio del {t['name']}.\n"
                            f"Tenés una partida pendiente de la Ronda {round_num} "
                            f"que vence el {fecha_str}.\n"
                            f"Tu rival es {m['white_name']} ({m['white_chess'] or '—'}).\n"
                            f"¡Coordiná y jugá antes del vencimiento!"
                        )

                        col_w, col_vs, col_b = st.columns([5, 1, 5])
                        with col_w:
                            st.markdown(f"**{m['white_name']}**  \n`{m['white_chess']}`")
                            link_w = wa_link(m["white_cel"], msg_w)
                            if link_w:
                                st.markdown(f"[📲 Avisar por WhatsApp]({link_w})", unsafe_allow_html=False)
                            else:
                                st.caption("⚠️ Sin celular registrado")
                        with col_vs:
                            st.markdown("<div style='text-align:center;padding-top:14px'>vs</div>", unsafe_allow_html=True)
                        with col_b:
                            st.markdown(f"**{m['black_name']}**  \n`{m['black_chess'] or 'LIBRE/BYE'}`")
                            link_b = wa_link(m["black_cel"], msg_b)
                            if link_b:
                                st.markdown(f"[📲 Avisar por WhatsApp]({link_b})", unsafe_allow_html=False)
                            else:
                                st.caption("⚠️ Sin celular registrado")
                        st.divider()

        # ── TAB CRUCES ────────────────────────────────────────────────
        with tab_cruces:
            rounds_for_view = q("SELECT number,start_datetime,end_datetime,status FROM rounds WHERE tournament_id=? ORDER BY number", (t["id"],))
            if rounds_for_view:
                round_opts = [int(r["number"]) for r in rounds_for_view]
                sel_round = st.selectbox("Ronda", round_opts, key=f"view_round_tab_{t['id']}")
                visual_rows = round_visual_rows(t["id"], sel_round)
                st.dataframe(visual_rows, use_container_width=True, hide_index=True)
            else:
                st.info("Este torneo todavía no tiene rondas.")

            with st.expander("Todos los cruces"):
                all_cruces = q("""
                    SELECT m.*, r.number,
                           wu.chesscom_user AS white_chess,
                           bu.chesscom_user AS black_chess
                    FROM matches m
                    JOIN rounds r ON r.id=m.round_id
                    JOIN users wu ON wu.id=m.white_user_id
                    LEFT JOIN users bu ON bu.id=m.black_user_id
                    WHERE m.tournament_id=?
                    ORDER BY r.number, m.id
                """, (t["id"],))
                st.dataframe([{
                    "Ronda": m["number"],
                    "Blancas": m["white_chess"],
                    "Negras": m["black_chess"] or "LIBRE/BYE",
                    "Estado": m["status"],
                    "Tipo": m["result_type"],
                    "Resultado": m["result"] or "",
                    "Bloqueada": "Sí" if m["locked"] else "No",
                    "Link": m["chesscom_url"] or "",
                } for m in all_cruces], use_container_width=True)

            with st.expander("Tabla de posiciones"):
                st.dataframe(standings(t["id"]), use_container_width=True)

        # ── TAB PLAYOFFS ──────────────────────────────────────────────
        with tab_playoffs:
            render_playoff_bracket(t["id"])

        # ── TAB CONFIG ────────────────────────────────────────────────
        with tab_cfg:
            if is_staff(current_user):
                st.subheader("Tiempos y modalidad")
                cc1, cc2, cc3 = st.columns(3)
                new_time = cc1.text_input("Ritmo regular", value=str(t["time_control"]), key=f"time_{t['id']}")
                new_class = cc2.selectbox("Clase regular", ["rapid", "blitz", "bullet", "daily"],
                    index=["rapid", "blitz", "bullet", "daily"].index(t["time_class"]) if t["time_class"] in ["rapid", "blitz", "bullet", "daily"] else 0,
                    key=f"class_{t['id']}")
                strict = cc3.checkbox("Colores exactos", value=bool(t["strict_colors"]), key=f"strict_{t['id']}")

                cp1, cp2 = st.columns(2)
                playoff_time = cp1.text_input("Ritmo playoffs", value=str(t["playoff_time_control"]), key=f"ptime_{t['id']}")
                playoff_class = cp2.selectbox("Clase playoffs", ["rapid", "blitz", "bullet", "daily"],
                    index=["rapid", "blitz", "bullet", "daily"].index(t["playoff_time_class"]) if t["playoff_time_class"] in ["rapid", "blitz", "bullet", "daily"] else 1,
                    key=f"pclass_{t['id']}")

                st.subheader("Estructura")
                cs1, cs2, cs3, cs4 = st.columns(4)
                edit_rounds_count = cs1.number_input("Rondas", value=int(t.get("rounds_count", 0) or 0), min_value=0, step=1, key=f"rounds_count_{t['id']}")
                edit_cups_count = cs2.number_input("Copas", value=int(t.get("cups_count", 3) or 3), min_value=1, step=1, key=f"cups_count_{t['id']}")
                edit_qualifiers_count = cs3.number_input("Clasificados", value=int(t.get("qualifiers_count", 24) or 24), min_value=2, step=1, key=f"qualifiers_count_{t['id']}")
                edit_cup_size = cs4.number_input("Por copa", value=int(t.get("cup_size", 8) or 8), min_value=2, step=2, key=f"cup_size_{t['id']}")

                st.subheader("Sistema de puntos")
                sp1, sp2, sp3, sp4, sp5 = st.columns(5)
                edit_win = sp1.number_input("Victoria", value=float(t.get("win_points", 1) or 1), step=0.5, key=f"win_{t['id']}")
                edit_draw = sp2.number_input("Empate", value=float(t.get("draw_points", 0.5) or 0.5), step=0.5, key=f"draw_{t['id']}")
                edit_loss = sp3.number_input("Derrota", value=float(t.get("loss_points", 0) or 0), step=0.5, key=f"loss_{t['id']}")
                edit_bye = sp4.number_input("BYE/libre", value=float(t.get("bye_points", 1) or 1), step=0.5, key=f"bye_{t['id']}")
                edit_wo = sp5.number_input("WO", value=float(t.get("wo_points", 0) or 0), step=0.5, key=f"wo_pts_{t['id']}")

                if st.button("💾 Guardar configuración", key=f"save_t_{t['id']}"):
                    exec_sql("""
                        UPDATE tournaments
                        SET time_control=?, time_class=?, strict_colors=?, playoff_time_control=?, playoff_time_class=?,
                            win_points=?, draw_points=?, loss_points=?, bye_points=?, wo_points=?,
                            rounds_count=?, cups_count=?, qualifiers_count=?, cup_size=?
                        WHERE id=?
                    """, (new_time, new_class, 1 if strict else 0, playoff_time, playoff_class,
                          edit_win, edit_draw, edit_loss, edit_bye, edit_wo,
                          edit_rounds_count, edit_cups_count, edit_qualifiers_count, edit_cup_size, t["id"]))
                    st.success("Configuración guardada.")
                    st.rerun()

                st.subheader("Fechas de rondas")
                rounds_cfg = q("SELECT * FROM rounds WHERE tournament_id=? ORDER BY number", (t["id"],))
                for r in rounds_cfg:
                    st.write(f"**Ronda {r['number']}**")
                    start_dt = parse_db_datetime(r["start_datetime"])
                    end_dt = parse_db_datetime(r["end_datetime"])
                    rfi, rff = st.columns(2)
                    new_start = rfi.text_input("Inicio", value=start_dt.strftime("%d/%m/%Y %H:%M"), key=f"rs_{r['id']}")
                    new_end = rff.text_input("Fin", value=end_dt.strftime("%d/%m/%Y %H:%M"), key=f"re_{r['id']}")
                    if st.button("Guardar fechas", key=f"save_round_{r['id']}"):
                        parsed_start = pd.to_datetime(new_start, dayfirst=True).to_pydatetime()
                        parsed_end = pd.to_datetime(new_end, dayfirst=True).to_pydatetime()
                        exec_sql("UPDATE rounds SET start_datetime=?, end_datetime=? WHERE id=?", (parsed_start, parsed_end, r["id"]))
                        st.success("Fechas actualizadas.")
                        st.rerun()
            else:
                st.info("Solo los administradores pueden modificar la configuración.")

elif admin_choice == "Admin usuarios":
    st.header("Admin usuarios")
    users = q("SELECT * FROM users ORDER BY role DESC, display_name")
    st.dataframe([{
        "ID": u["id"],
        "Nombre": u["display_name"],
        "Usuario": u["username"],
        "Chess.com": u["chesscom_user"],
        "Celular": u.get("celular") or "",
        "Rol": u["role"],
        "ELO": u["elo"],
        "Estado": u["account_status"],
        "Clave temporal": "Sí" if u["must_change_password"] else "No",
    } for u in users], use_container_width=True)

    labels = {f"{u['display_name']} ({u['chesscom_user']})": u["id"] for u in users}

    st.subheader("Crear jugador con clave 12345")
    new_chess = st.text_input("Usuario Chess.com nuevo")
    new_name = st.text_input("Nombre visible opcional")
    if st.button("Crear jugador"):
        try:
            get_or_create_player(new_chess, new_name or None)
            st.success("Jugador creado o ya existente.")
            st.rerun()
        except Exception as exc:
            st.error(exc)

    if labels:
        st.subheader("Corregir usuario Chess.com")
        target = st.selectbox("Perfil", list(labels.keys()), key="edit_chess_profile")
        target_user = get_user(labels[target])
        aliases = q("SELECT alias FROM chess_aliases WHERE user_id=? ORDER BY alias", (target_user["id"],))
        st.caption("Alias: " + (", ".join([a["alias"] for a in aliases]) if aliases else "sin alias"))

        new_chess_name = st.text_input("Nuevo Chess.com principal", value=target_user["chesscom_user"])
        new_display = st.text_input("Nombre visible", value=target_user["display_name"])
        new_celular = st.text_input("Celular (sin +, ej: 3815123456)", value=target_user.get("celular") or "")
        keep_alias = st.checkbox("Guardar anterior como alias", value=True)

        if st.button("Guardar corrección"):
            try:
                update_user_chess(target_user["id"], new_chess_name, new_display, keep_alias)
                exec_sql("UPDATE users SET celular=? WHERE id=?", (new_celular.strip() or None, target_user["id"]))
                st.success("Perfil corregido.")
                st.rerun()
            except Exception as exc:
                st.error(exc)

        alias = st.text_input("Agregar alias")
        if st.button("Agregar alias"):
            try:
                add_chess_alias(target_user["id"], alias)
                st.success("Alias agregado.")
                st.rerun()
            except Exception as exc:
                st.error(exc)

        st.subheader("Resetear contraseña")
        reset_target = st.selectbox("Usuario a resetear", list(labels.keys()), key="reset_user")
        if st.button("Resetear a 12345"):
            update_password(labels[reset_target], DEFAULT_PASSWORD, 1)
            st.success("Contraseña reseteada.")
            st.rerun()

    st.subheader("📲 Importar celulares desde Excel")
    st.caption("Subí el Excel con la hoja 'jugadores' (columnas: Cod, Nombre, Usuario, Alias, Celular, …)")
    cel_file = st.file_uploader("Archivo Excel (.xlsx)", type=["xlsx"], key="cel_import")
    if cel_file:
        try:
            df_jug = pd.read_excel(cel_file, sheet_name="jugadores", header=0)
            df_jug.columns = [str(c).strip() for c in df_jug.columns]
            col_nombre   = df_jug.columns[1]
            col_usuario  = df_jug.columns[2]
            col_celular  = df_jug.columns[4]
            df_jug[col_celular] = df_jug[col_celular].apply(
                lambda v: "".join(c for c in str(v) if c.isdigit()) if pd.notna(v) else ""
            )
            df_jug = df_jug[df_jug[col_celular].str.len() >= 8]

            all_users = q("SELECT id, display_name, chesscom_user, celular FROM users")
            # mapa 1: por chesscom_user
            by_chess = {(u["chesscom_user"] or "").lower(): u for u in all_users if u["chesscom_user"]}
            # mapa 2: por alias  {alias_lower: user_id}
            all_aliases = q("SELECT user_id, alias FROM chess_aliases")
            alias_to_uid = {(a["alias"] or "").lower(): a["user_id"] for a in all_aliases}
            uid_map = {u["id"]: u for u in all_users}
            # mapa 3: por display_name
            by_display = {(u["display_name"] or "").lower(): u for u in all_users if u["display_name"]}

            def find_user(username_raw, nombre_raw):
                key = username_raw.lower()
                if key and key in by_chess:
                    return by_chess[key], "usuario"
                if key and key in alias_to_uid:
                    return uid_map[alias_to_uid[key]], "alias"
                # fallback: nombre del Excel contra display_name
                nkey = nombre_raw.lower()
                if nkey and nkey in by_display:
                    return by_display[nkey], "nombre"
                return None, None

            preview = []
            updates = []
            for _, row in df_jug.iterrows():
                username_raw = str(row[col_usuario]).strip() if pd.notna(row[col_usuario]) else ""
                nombre_raw   = str(row[col_nombre]).strip()  if pd.notna(row[col_nombre])  else ""
                celular_raw  = str(row[col_celular]).strip()
                u, via = find_user(username_raw, nombre_raw)
                if u:
                    preview.append({
                        "Chess.com": u["chesscom_user"],
                        "Nombre": u["display_name"],
                        "Celular actual": u.get("celular") or "—",
                        "Celular nuevo": celular_raw,
                        "Via": via,
                        "Acción": "actualizar" if (u.get("celular") or "") != celular_raw else "sin cambio",
                    })
                    if (u.get("celular") or "") != celular_raw:
                        updates.append((celular_raw, u["id"]))
                else:
                    preview.append({
                        "Chess.com": username_raw or nombre_raw,
                        "Nombre": nombre_raw,
                        "Celular actual": "—",
                        "Celular nuevo": celular_raw,
                        "Via": "—",
                        "Acción": "⚠️ no encontrado",
                    })

            st.dataframe(preview, use_container_width=True)
            n_updates = len(updates)
            n_notfound = sum(1 for p in preview if p["Acción"] == "⚠️ no encontrado")
            st.caption(f"{n_updates} actualización(es) pendiente(s)  ·  {n_notfound} usuario(s) no encontrado(s) en la base")

            if n_updates > 0 and st.button(f"💾 Importar {n_updates} celular(es)"):
                for cel, uid in updates:
                    exec_sql("UPDATE users SET celular=? WHERE id=?", (cel, uid))
                st.success(f"✅ {n_updates} celular(es) importados correctamente.")
                st.rerun()
        except Exception as exc:
            st.error(f"Error al leer el Excel: {exc}")

    if labels and is_admin(current_user):
        st.subheader("Cambiar rol / estado / ELO")
        edit_target = st.selectbox("Usuario a modificar", list(labels.keys()), key="edit_user")
        edit_user = get_user(labels[edit_target])
        roles = ["player", "moderator", "admin"] + (["superadmin"] if is_superadmin(current_user) else [])
        role = st.selectbox("Rol", roles, index=roles.index(edit_user["role"]) if edit_user["role"] in roles else 0)
        status = st.selectbox("Estado", ["pending", "active", "suspended"], index=["pending", "active", "suspended"].index(edit_user["account_status"]) if edit_user["account_status"] in ["pending", "active", "suspended"] else 1)
        elo = st.number_input("ELO", value=int(edit_user["elo"]), step=10)

        if st.button("Guardar usuario"):
            exec_sql("UPDATE users SET role=?, account_status=?, elo=? WHERE id=?", (role, status, int(elo), edit_user["id"]))
            st.success("Usuario actualizado.")
            st.rerun()

if choice == "Mi perfil":
    st.header("Mi perfil")
    if current_user.get("avatar_url"):
        st.image(current_user["avatar_url"], width=150)
    st.metric("ELO interno", current_user["elo"])
    st.write(f"**Nombre:** {current_user['display_name']}")
    st.write(f"**Chess.com:** {current_user['chesscom_user']}")
    st.write(f"**Rol:** {current_user['role']}")

    if st.button("Sincronizar avatar"):
        sync_avatar(current_user["id"], current_user["chesscom_user"])
        st.rerun()

    st.subheader("Cambiar contraseña")
    with st.form("change_password"):
        old = st.text_input("Contraseña actual", type="password")
        p1 = st.text_input("Nueva contraseña", type="password")
        p2 = st.text_input("Repetir nueva contraseña", type="password")
        ok = st.form_submit_button("Cambiar")
        if ok:
            fresh = get_user(current_user["id"])
            if fresh["password_hash"] != hash_password(old):
                st.error("Contraseña actual incorrecta.")
            elif not p1 or p1 != p2:
                st.error("Las nuevas contraseñas no coinciden.")
            else:
                update_password(current_user["id"], p1, 0)
                st.success("Contraseña cambiada.")

elif choice == "Ranking":
    st.header("Ranking")
    users = q("SELECT display_name,chesscom_user,elo,role,account_status,must_change_password FROM users ORDER BY elo DESC")
    st.dataframe([{
        "Nombre": u["display_name"],
        "Chess.com": u["chesscom_user"],
        "ELO": u["elo"],
        "Rol": u["role"],
        "Estado": u["account_status"],
        "Clave temporal": "Sí" if u["must_change_password"] else "No",
    } for u in users], use_container_width=True)