import datetime as dt
import hashlib
import io
import os
from pathlib import Path

import pandas as pd
import requests
import streamlit as st


APP_TITLE = "Torneos de Ajedrez"
DEFAULT_PASSWORD = "12345"
API_BASE = "https://api.chess.com/pub"
HEADERS = {"User-Agent": "torneos-ajedrez-v8-supabase/1.0"}
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

    c.commit()
    c.close()


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
    except Exception:
        return None
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
    except Exception:
        pass
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
        if any(k in url for k in keys):
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
        JOIN users bu ON bu.id=m.black_user_id
        WHERE m.tournament_id=? AND m.status='pending' AND m.locked=0
    """, (tournament_id,))

    found = 0
    debug = []

    for match in pending:
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

            exact, inverted = game_is_between_players_any_color(game, match["white_user_id"], match["black_user_id"])
            if not exact and not inverted:
                final_reason = "usuarios no coinciden"
                continue

            ok, reason = validate_game_without_color(game, tournament, start_dt, end_dt)
            if not ok:
                final_reason = reason
                continue

            score = score_for_fixture_white(game, match["white_user_id"], match["black_user_id"])
            if score is None:
                final_reason = "partida encontrada sin resultado interpretable"
                continue

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
                if not valid_chess_username(white_raw) or not valid_chess_username(black_raw):
                    continue

                white_id, wc = get_or_create_player(white_raw)
                black_id, bc = get_or_create_player(black_raw)
                created_players += int(wc) + int(bc)

                register_player(tournament_id, white_id)
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
            if not valid_chess_username(white_raw) or not valid_chess_username(black_raw):
                continue

            white_id, wc = get_or_create_player(white_raw)
            black_id, bc = get_or_create_player(black_raw)
            created_players += int(wc) + int(bc)

            register_player(tournament_id, white_id)
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

    if old_status != "finished":
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
                    pts += 1; wins += 1
                elif match["result"] == "0-1":
                    losses += 1
                elif match["result"] == "1/2-1/2":
                    pts += 0.5; draws += 1
            else:
                if match["result"] == "0-1":
                    pts += 1; wins += 1
                elif match["result"] == "1-0":
                    losses += 1
                elif match["result"] == "1/2-1/2":
                    pts += 0.5; draws += 1

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

    table.sort(key=lambda x: (x["Estado"] != "disqualified", x["Puntos"], x["ELO"], x["G"]), reverse=True)
    return table


# =========================================================
# UI
# =========================================================

init_db()

if "user" not in st.session_state:
    st.session_state.user = None

st.markdown("""
<style>
.block-container {max-width: 1200px;}
.login-box {max-width: 440px; margin: 0 auto;}
.login-box [data-testid="stTextInput"] {max-width: 440px;}
.login-box .stButton button {width: 180px;}
</style>
""", unsafe_allow_html=True)

st.title("♟️ Torneos de Ajedrez — V8 Supabase")

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

menu = ["Torneos", "Mi perfil", "Ranking"]
if is_staff(current_user):
    menu = ["Torneos", "Crear torneo", "Importar fixture", "Importar rondas", "Admin usuarios", "Mi perfil", "Ranking"]

choice = st.sidebar.radio("Menú", menu)

if choice == "Crear torneo":
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

    if st.button("Crear torneo"):
        if not name.strip():
            st.warning("Poné un nombre.")
        else:
            tid = create_empty_tournament(name.strip(), desc, rules, time_class, time_control, rated_filter, strict_colors, current_user["id"])
            st.success(f"Torneo creado. ID: {tid}")

elif choice == "Importar fixture":
    st.header("Importar fixture completo")
    st.write("Columnas: `torneo`, `ronda`, `fecha_inicio`, `fecha_fin`, `blancas_chesscom`, `negras_chesscom`.")

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

elif choice == "Importar rondas":
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

elif choice == "Torneos":
    st.header("Torneos")
    tournaments = q("SELECT * FROM tournaments ORDER BY id DESC")
    if not tournaments:
        st.info("Todavía no hay torneos.")

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
                    new_class = ec2.selectbox("Clase regular", ["rapid", "blitz", "bullet", "daily"], index=["rapid", "blitz", "bullet", "daily"].index(t["time_class"]) if t["time_class"] in ["rapid", "blitz", "bullet", "daily"] else 0, key=f"class_{t['id']}")
                    strict = ec3.checkbox("Colores exactos", value=bool(t["strict_colors"]), key=f"strict_{t['id']}")

                    pc1, pc2 = st.columns(2)
                    playoff_time = pc1.text_input("Ritmo playoffs", value=str(t["playoff_time_control"]), key=f"ptime_{t['id']}")
                    playoff_class = pc2.selectbox("Clase playoffs", ["rapid", "blitz", "bullet", "daily"], index=["rapid", "blitz", "bullet", "daily"].index(t["playoff_time_class"]) if t["playoff_time_class"] in ["rapid", "blitz", "bullet", "daily"] else 1, key=f"pclass_{t['id']}")

                    if st.button("Guardar configuración", key=f"save_t_{t['id']}"):
                        exec_sql("""
                            UPDATE tournaments
                            SET time_control=?, time_class=?, strict_colors=?, playoff_time_control=?, playoff_time_class=?
                            WHERE id=?
                        """, (new_time, new_class, 1 if strict else 0, playoff_time, playoff_class, t["id"]))
                        st.success("Configuración guardada.")
                        st.rerun()

                with st.expander("Editar fechas de rondas"):
                    rounds = q("SELECT * FROM rounds WHERE tournament_id=? ORDER BY number", (t["id"],))
                    for r in rounds:
                        st.write(f"**Ronda {r['number']}**")
                        start_dt = parse_db_datetime(r["start_datetime"])
                        end_dt = parse_db_datetime(r["end_datetime"])
                        cfi, cff = st.columns(2)
                        new_start = cfi.text_input("Inicio", value=start_dt.strftime("%d/%m/%Y %H:%M"), key=f"rs_{r['id']}")
                        new_end = cff.text_input("Fin", value=end_dt.strftime("%d/%m/%Y %H:%M"), key=f"re_{r['id']}")
                        if st.button("Guardar fechas", key=f"save_round_{r['id']}"):
                            parsed_start = pd.to_datetime(new_start, dayfirst=True).to_pydatetime()
                            parsed_end = pd.to_datetime(new_end, dayfirst=True).to_pydatetime()
                            exec_sql("UPDATE rounds SET start_datetime=?, end_datetime=? WHERE id=?", (parsed_start, parsed_end, r["id"]))
                            st.success("Fechas actualizadas.")
                            st.rerun()

                b1, b2 = st.columns(2)
                if b1.button("Buscar resultados Chess.com", key=f"scan_{t['id']}"):
                    found, debug = scan_tournament(t["id"])
                    st.success(f"Detectadas: {found}")
                    st.dataframe(debug, use_container_width=True)

                if b2.button("Aplicar WO vencidos", key=f"wo_{t['id']}"):
                    applied = apply_wo_expired(t["id"])
                    st.warning(f"WO aplicados: {applied}")
                    st.rerun()

                # Auto scan simple: refresh vía meta cada minuto si se activa
                with st.expander("Motor automático y resultados manuales"):
                    auto = st.checkbox("Detectar automáticamente cada 1 minuto", key=f"auto_{t['id']}")
                    if auto:
                        st.markdown('<meta http-equiv="refresh" content="60">', unsafe_allow_html=True)
                        found, debug = scan_tournament(t["id"])
                        st.caption(f"Auto-búsqueda ejecutada. Detectadas: {found}")
                        st.dataframe(debug, use_container_width=True)

                    all_matches = q("""
                        SELECT m.*, r.number,
                               wu.chesscom_user AS white_chess,
                               bu.chesscom_user AS black_chess
                        FROM matches m
                        JOIN rounds r ON r.id=m.round_id
                        JOIN users wu ON wu.id=m.white_user_id
                        JOIN users bu ON bu.id=m.black_user_id
                        WHERE m.tournament_id=?
                        ORDER BY r.number, m.id
                    """, (t["id"],))
                    if all_matches:
                        labels = {f"R{m['number']} | {m['white_chess']} vs {m['black_chess']} | {m['status']} | {m['result'] or 'sin resultado'}": m["id"] for m in all_matches}
                        sel = st.selectbox("Partida", list(labels.keys()), key=f"manual_match_{t['id']}")
                        res = st.selectbox("Resultado", ["1-0", "0-1", "1/2-1/2"], key=f"manual_res_{t['id']}")
                        cm1, cm2 = st.columns(2)
                        if cm1.button("Guardar resultado manual", key=f"save_manual_{t['id']}"):
                            try:
                                set_manual_result(labels[sel], res, current_user["id"])
                                st.success("Resultado manual guardado.")
                                st.rerun()
                            except Exception as exc:
                                st.error(exc)
                        if cm2.button("Limpiar resultado", key=f"clear_manual_{t['id']}"):
                            try:
                                clear_match_result(labels[sel], current_user["id"])
                                st.warning("Resultado limpiado.")
                                st.rerun()
                            except Exception as exc:
                                st.error(exc)

                reviews = q("""
                    SELECT m.*, r.number,
                           wu.chesscom_user AS white_chess,
                           bu.chesscom_user AS black_chess
                    FROM matches m
                    JOIN rounds r ON r.id=m.round_id
                    JOIN users wu ON wu.id=m.white_user_id
                    JOIN users bu ON bu.id=m.black_user_id
                    WHERE m.tournament_id=? AND m.status='review'
                    ORDER BY r.number, m.id
                """, (t["id"],))
                if reviews:
                    st.subheader("Revisión por colores invertidos")
                    st.warning("Estas partidas coinciden, pero se jugaron con colores invertidos.")
                    for rm in reviews:
                        st.write(f"R{rm['number']} | `{rm['white_chess']} vs {rm['black_chess']}` | Resultado si acepta: **{rm['review_result'] or rm['result']}**")
                        if rm["review_url"]:
                            st.write(rm["review_url"])
                        ca, cr = st.columns(2)
                        if ca.button("Aceptar partida", key=f"accept_{rm['id']}"):
                            try:
                                accept_color_review(rm["id"], current_user["id"])
                                st.success("Aceptada.")
                                st.rerun()
                            except Exception as exc:
                                st.error(exc)
                        if cr.button("Rechazar partida", key=f"reject_{rm['id']}"):
                            try:
                                reject_color_review(rm["id"], current_user["id"])
                                st.warning("Rechazada.")
                                st.rerun()
                            except Exception as exc:
                                st.error(exc)

            st.subheader("Rondas")
            rounds = q("SELECT number,start_datetime,end_datetime,status FROM rounds WHERE tournament_id=? ORDER BY number", (t["id"],))
            st.dataframe(rounds, use_container_width=True)

            st.subheader("Cruces")
            matches = q("""
                SELECT m.*, r.number,
                       wu.chesscom_user AS white_chess,
                       bu.chesscom_user AS black_chess
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
        keep_alias = st.checkbox("Guardar anterior como alias", value=True)

        if st.button("Guardar corrección"):
            try:
                update_user_chess(target_user["id"], new_chess_name, new_display, keep_alias)
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

        if is_admin(current_user):
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

elif choice == "Mi perfil":
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