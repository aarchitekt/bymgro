from __future__ import annotations

import datetime
import hashlib
import json
import os
import secrets
import sqlite3
import string
from contextlib import contextmanager
from pathlib import Path

DB_PATH = os.environ.get("BYMGRO_DB_PATH", str(Path(__file__).resolve().parent.parent / "bymgro.db"))
SEED_PATH = Path(__file__).resolve().parent / "seed_data.json"
# Update 1.6: real login. AAMEND_USER_ID is a fixed (not random) id so the
# migration that seeds this account is idempotent across restarts/deploys --
# see _seed_aamend_account() below for why this exists at all.
AAMEND_USER_ID = "user_aamend"
SEED_AAMEND_HISTORY_PATH = Path(__file__).resolve().parent / "seed_aamend_history.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    code TEXT UNIQUE NOT NULL,
    display_name TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS profile (
    user_id TEXT PRIMARY KEY REFERENCES users(id),
    name TEXT,
    height_cm REAL,
    weight_kg REAL,
    age INTEGER,
    sex TEXT,
    goal TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS bodyweight_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    date TEXT NOT NULL,
    kg REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS plan_exercises (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    day TEXT NOT NULL,
    position INTEGER NOT NULL,
    name TEXT NOT NULL,
    muscle TEXT,
    kind TEXT NOT NULL,
    target_sets INTEGER NOT NULL DEFAULT 3,
    unit TEXT NOT NULL DEFAULT 'kg',
    last_weight REAL,
    last_reps REAL,
    last_reps_by_set TEXT
);

CREATE TABLE IF NOT EXISTS workout_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    date TEXT NOT NULL,
    day_type TEXT NOT NULL,
    duration_min REAL,
    bodyweight_kg REAL,
    created_at TEXT NOT NULL,
    finished INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS session_sets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES workout_sessions(id) ON DELETE CASCADE,
    exercise_name TEXT NOT NULL,
    set_index INTEGER NOT NULL,
    weight REAL,
    reps REAL,
    value_text TEXT,
    logged INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS nutrition_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    date TEXT NOT NULL,
    calories REAL,
    protein_g REAL,
    UNIQUE(user_id, date)
);

CREATE TABLE IF NOT EXISTS supplements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS supplement_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    supplement_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    taken INTEGER NOT NULL DEFAULT 1,
    UNIQUE(user_id, supplement_id, date)
);

CREATE TABLE IF NOT EXISTS habit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    date TEXT NOT NULL,
    alcohol INTEGER NOT NULL DEFAULT 0,
    smoke INTEGER NOT NULL DEFAULT 0,
    drugs INTEGER NOT NULL DEFAULT 0,
    UNIQUE(user_id, date)
);

CREATE TABLE IF NOT EXISTS friendships (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    friend_user_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(user_id, friend_user_id)
);

CREATE TABLE IF NOT EXISTS user_achievements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    achievement_id TEXT NOT NULL,
    unlocked_at TEXT NOT NULL,
    UNIQUE(user_id, achievement_id)
);

-- Update 1.6: real username/password login, added on top of the existing
-- anonymous-UUID identity model rather than replacing it -- a login just
-- resolves username+password to a user_id, which then flows through the
-- exact same X-User-Id header every other endpoint already uses.
CREATE TABLE IF NOT EXISTS auth_credentials (
    username TEXT PRIMARY KEY,
    salt TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    user_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Update 1.7.1: tiny generic key/value marker table for one-off, run-once
-- data-correction migrations (see _fix_aamend_history_v2()) -- cheaper than
-- adding a dedicated boolean column for every future one-time fix.
CREATE TABLE IF NOT EXISTS seed_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn: sqlite3.Connection):
    """Bring an older bymgro.db forward to the current schema (idempotent)."""
    profile_cols = {r["name"] for r in conn.execute("PRAGMA table_info(profile)").fetchall()}
    if profile_cols and "user_id" not in profile_cols:
        _archive_legacy_single_user_schema(conn)

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(session_sets)").fetchall()}
    if "logged" not in cols:
        conn.execute("ALTER TABLE session_sets ADD COLUMN logged INTEGER NOT NULL DEFAULT 1")

    # Update 1.8: remembers reps per set position (set 1 is usually higher
    # reps than set 3 in a pyramid, e.g. 12/10/8) instead of one shared
    # last_reps value that the most recently logged set always overwrote.
    plan_cols = {r["name"] for r in conn.execute("PRAGMA table_info(plan_exercises)").fetchall()}
    if "last_reps_by_set" not in plan_cols:
        conn.execute("ALTER TABLE plan_exercises ADD COLUMN last_reps_by_set TEXT")

    _fix_session_sets_fk(conn)
    _seed_aamend_account(conn)
    _fix_aamend_history_v2(conn)
    _fix_aamend_history_v3(conn)


def _fix_session_sets_fk(conn: sqlite3.Connection):
    """Caught via a live smoke test while building Update 1.2, NOT by the
    user hitting it -- but it would have hit them on their very next logged
    set, so this must self-heal on startup for anyone who already went
    through the Update 1.1 migration (their DB has this bug sitting in it
    right now).

    SQLite auto-rewrites FOREIGN KEY clauses in *other* tables whenever the
    table they reference is renamed. _archive_legacy_single_user_schema()
    does `ALTER TABLE workout_sessions RENAME TO workout_sessions_legacy_pre_1_1`
    -- and session_sets' `REFERENCES workout_sessions(id)` got silently
    repointed at that archived table as a side effect. Every session
    created *after* the migration lives in the new workout_sessions table,
    so its FK never matched, and log_set() started raising
    sqlite3.IntegrityError the moment PRAGMA foreign_keys=ON was enforced.
    Existing session_sets rows/data are untouched -- this only rebuilds the
    table to point the constraint at the live workout_sessions table."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='session_sets'"
    ).fetchone()
    if not row or not row["sql"] or "workout_sessions_legacy_pre_1_1" not in row["sql"]:
        return
    conn.execute("ALTER TABLE session_sets RENAME TO session_sets_fkfix_tmp")
    conn.execute("""
        CREATE TABLE session_sets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL REFERENCES workout_sessions(id) ON DELETE CASCADE,
            exercise_name TEXT NOT NULL,
            set_index INTEGER NOT NULL,
            weight REAL,
            reps REAL,
            value_text TEXT,
            logged INTEGER NOT NULL DEFAULT 1
        )
    """)
    conn.execute(
        """INSERT INTO session_sets (id, session_id, exercise_name, set_index, weight, reps, value_text, logged)
           SELECT id, session_id, exercise_name, set_index, weight, reps, value_text, logged FROM session_sets_fkfix_tmp"""
    )
    conn.execute("DROP TABLE session_sets_fkfix_tmp")


def _archive_legacy_single_user_schema(conn: sqlite3.Connection):
    """Update 1.1 moved from a single-profile schema to a multi-user one
    (added a `users` table + `user_id` columns everywhere). An older
    bymgro.db from before that update has incompatible tables under the
    same names -- `CREATE TABLE IF NOT EXISTS` in init_db() silently skipped
    them because they already existed. Rename the old ones aside (nothing is
    deleted) so fresh multi-user tables can be created in their place. The
    first real user to open the app afterwards still inherits
    backend/seed_data.json's history exactly as on a brand new install, so
    nothing is functionally lost -- the archived *_legacy_pre_1_1 tables are
    just there if anyone ever needs to look back at pre-1.1 local test data."""
    for t in ("profile", "plan_exercises", "workout_sessions", "bodyweight_log"):
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)
        ).fetchone()
        if exists:
            conn.execute(f"ALTER TABLE {t} RENAME TO {t}_legacy_pre_1_1")
    conn.executescript(SCHEMA)


def _hash_password(password: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{password}".encode("utf-8")).hexdigest()


def create_credential(user_id: str, username: str, password: str):
    username = username.strip().lower()
    salt = secrets.token_hex(8)
    pw_hash = _hash_password(password, salt)
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO auth_credentials (username, salt, password_hash, user_id, created_at) VALUES (?,?,?,?,datetime('now'))",
            (username, salt, pw_hash, user_id),
        )


def credential_exists(username: str) -> bool:
    username = username.strip().lower()
    with get_conn() as conn:
        return conn.execute("SELECT 1 FROM auth_credentials WHERE username=?", (username,)).fetchone() is not None


def verify_login(username: str, password: str) -> str | None:
    username = username.strip().lower()
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM auth_credentials WHERE username=?", (username,)).fetchone()
        if not row or _hash_password(password, row["salt"]) != row["password_hash"]:
            return None
        return row["user_id"]


def _seed_aamend_account(conn: sqlite3.Connection):
    """Update 1.6: real username/password login was added because the old
    anonymous-localStorage-only identity had no recovery path -- every time
    storage got cleared/reset on the real device, a fresh throwaway UUID got
    created and the real training history stayed stuck under the old
    unreachable one (confirmed happening: 4 distinct anonymous users existed
    in prod, all created within the same session). This seeds one durable
    account (username 'aamend') bound to a *fixed* user_id (AAMEND_USER_ID,
    not randomly generated) so this function is idempotent across restarts
    -- guarded on the credential row not existing yet, so it only ever runs
    its INSERTs once, on whichever deploy first creates the 'auth_credentials'
    table. Also imports the user's real logged history from their own Excel
    tracker (seed_aamend_history.json, parsed from "NO SKINNY FAT PLS.xlsx")
    so the account isn't empty on first login -- only runs that import if
    this user has zero workout_sessions yet, so it never duplicates data on
    a later restart."""
    if conn.execute("SELECT 1 FROM auth_credentials WHERE username='aamend'").fetchone():
        return

    user = conn.execute("SELECT * FROM users WHERE id=?", (AAMEND_USER_ID,)).fetchone()
    if not user:
        code = gen_code()
        while conn.execute("SELECT 1 FROM users WHERE code=?", (code,)).fetchone():
            code = gen_code()
        conn.execute(
            "INSERT INTO users (id, code, display_name, created_at) VALUES (?,?,?,datetime('now'))",
            (AAMEND_USER_ID, code, "Aaron"),
        )
        conn.execute("INSERT INTO profile (user_id, updated_at) VALUES (?, datetime('now'))", (AAMEND_USER_ID,))
        _seed_plan_default(conn, AAMEND_USER_ID)

    salt = secrets.token_hex(8)
    pw_hash = _hash_password("bymgro", salt)
    conn.execute(
        "INSERT INTO auth_credentials (username, salt, password_hash, user_id, created_at) VALUES (?,?,?,?,datetime('now'))",
        ("aamend", salt, pw_hash, AAMEND_USER_ID),
    )

    has_sessions = conn.execute(
        "SELECT 1 FROM workout_sessions WHERE user_id=? LIMIT 1", (AAMEND_USER_ID,)
    ).fetchone()
    if not has_sessions:
        _import_aamend_history(conn)


def _import_aamend_history(conn: sqlite3.Connection):
    """Shared by _seed_aamend_account() (fresh install) and
    _fix_aamend_history_v2() (correcting an already-seeded prod DB) --
    inserts every session in seed_aamend_history.json for AAMEND_USER_ID."""
    if not SEED_AAMEND_HISTORY_PATH.exists():
        return
    try:
        history = json.loads(SEED_AAMEND_HISTORY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        history = []
    for sess in history:
        cur = conn.execute(
            """INSERT INTO workout_sessions (user_id, date, day_type, duration_min, bodyweight_kg, created_at, finished)
               VALUES (?,?,?,?,?,datetime('now'),1)""",
            (AAMEND_USER_ID, sess["date"], sess["day_type"], sess.get("duration_min"), sess.get("bodyweight_kg")),
        )
        sid = cur.lastrowid
        for s in sess.get("sets", []):
            conn.execute(
                """INSERT INTO session_sets (session_id, exercise_name, set_index, weight, reps, value_text, logged)
                   VALUES (?,?,?,?,?,?,1)""",
                (sid, s["exercise_name"], s["set_index"], s.get("weight"), s.get("reps"), s.get("value_text")),
            )
        if sess.get("bodyweight_kg") is not None:
            conn.execute(
                "INSERT INTO bodyweight_log (user_id, date, kg) VALUES (?,?,?)",
                (AAMEND_USER_ID, sess["date"], sess["bodyweight_kg"]),
            )


# Update 1.7.1: the very first Excel import (Update 1.6) had two real bugs
# in how it read "NO SKINNY FAT PLS.xlsx" -- (1) a stray junk date typed
# into an ordinary weight/reps cell elsewhere in the sheet got misread as
# an extra block header, which mis-labelled a batch of real Pull sets as
# "push"; (2) every date got split into a separate push *and* pull session
# whenever the shared Rudern warmup (which structurally lives in the sheet's
# push section) had a value, even on days that were purely Pull -- so most
# imported days showed up as a real session plus a bogus near-empty
# "push: just Rudern" twin. Both are fixed in the current
# seed_aamend_history.json (single session per real training day, Rudern/
# Plank/Leg-Raises no longer vote on day-type). This list is the exact
# (date, day_type) signature of the OLD, wrong 23-row seed -- used to
# delete precisely those rows (and only those) before re-importing the
# corrected 14, so anything the user has logged for real in the meantime
# (any date/day_type combo not in this exact list) is left untouched.
_AAMEND_HISTORY_V1_BAD_ROWS = [
    ("2026-04-30", "push"), ("2026-05-02", "pull"), ("2026-05-02", "push"),
    ("2026-05-04", "pull"), ("2026-05-04", "push"), ("2026-05-08", "pull"),
    ("2026-05-08", "push"), ("2026-05-11", "pull"), ("2026-05-11", "push"),
    ("2026-05-13", "pull"), ("2026-05-13", "push"), ("2026-06-02", "push"),
    ("2026-06-04", "pull"), ("2026-06-04", "push"), ("2026-06-11", "pull"),
    ("2026-07-11", "push"), ("2026-07-16", "push"), ("2026-07-18", "pull"),
    ("2026-07-18", "push"), ("2026-08-01", "pull"), ("2026-08-03", "push"),
    ("2026-08-06", "pull"), ("2026-08-06", "push"),
]


def _fix_aamend_history_v2(conn: sqlite3.Connection):
    if not conn.execute("SELECT 1 FROM auth_credentials WHERE username='aamend'").fetchone():
        return  # _seed_aamend_account() hasn't even run yet -- nothing to fix
    if conn.execute("SELECT 1 FROM seed_meta WHERE key='aamend_history_v2'").fetchone():
        return  # already fixed
    for date, day_type in _AAMEND_HISTORY_V1_BAD_ROWS:
        rows = conn.execute(
            "SELECT id FROM workout_sessions WHERE user_id=? AND date=? AND day_type=?",
            (AAMEND_USER_ID, date, day_type),
        ).fetchall()
        for r in rows:
            conn.execute("DELETE FROM session_sets WHERE session_id=?", (r["id"],))
            conn.execute("DELETE FROM workout_sessions WHERE id=?", (r["id"],))
    bad_dates = {d for d, _ in _AAMEND_HISTORY_V1_BAD_ROWS}
    for d in bad_dates:
        conn.execute("DELETE FROM bodyweight_log WHERE user_id=? AND date=?", (AAMEND_USER_ID, d))
    _import_aamend_history(conn)
    conn.execute(
        "INSERT INTO seed_meta (key, value) VALUES ('aamend_history_v2', datetime('now'))"
    )


# Update 1.8: re-examined "NO SKINNY FAT PLS.xlsx" again after the user
# pointed out it holds visibly more real training days than the 14 the v2
# import produced. Root cause: several column headers in the sheet are not
# clean dates at all. Some got overwritten with a stray weight/duration
# number (10.5, 16.5, 27.6, 30.6, 2.7), and one has a typo'd year (2029
# instead of 2026) or an impossible future day (8/30, past "today" in the
# sheet's own timeline, really meant 7/30). The v2 parser only ever
# recognized a column as a real session when its header cell parsed
# cleanly as a date, so all seven of these were silently skipped even
# though the exercise rows underneath them hold full, real logged sets.
# Each one's true date is inferred from its position in the sheet: every
# other column group in the workbook runs left-to-right in strict
# chronological order, so a corrupted header's date is pinned down by
# the two clean dates immediately surrounding it (this is the same
# "infer from context" approach the user explicitly asked for). One
# already-imported session also gets a date correction here: the column
# between 5/4 and 5/8 carried a literal header value of 8/6/2026, which
# would be the only column in the entire sheet out of chronological
# order if taken at face value (almost certainly a "5" mistyped as
# "8"), corrected to 5/6/2026.
_AAMEND_HISTORY_V2_ROWS = [
    ("2026-04-30", "push"), ("2026-05-02", "pull"), ("2026-05-04", "push"),
    ("2026-05-08", "push"), ("2026-05-11", "push"), ("2026-05-13", "pull"),
    ("2026-06-02", "push"), ("2026-06-04", "pull"), ("2026-06-11", "pull"),
    ("2026-07-16", "push"), ("2026-07-18", "push"), ("2026-08-01", "pull"),
    ("2026-08-03", "push"), ("2026-08-06", "pull"),
]


def _fix_aamend_history_v3(conn: sqlite3.Connection):
    if not conn.execute("SELECT 1 FROM auth_credentials WHERE username='aamend'").fetchone():
        return  # _seed_aamend_account() hasn't even run yet (nothing to fix)
    if conn.execute("SELECT 1 FROM seed_meta WHERE key='aamend_history_v3'").fetchone():
        return  # already fixed

    def _delete_session(row_id):
        conn.execute("DELETE FROM session_sets WHERE session_id=?", (row_id,))
        conn.execute("DELETE FROM workout_sessions WHERE id=?", (row_id,))

    # Pass 1: remove the specific old/wrong (date, day_type) rows carried
    # forward from the v2-era 14-session import (this is what an upgraded
    # prod DB, which already ran v2 in the past, actually has sitting in it).
    for date, day_type in _AAMEND_HISTORY_V2_ROWS:
        for r in conn.execute(
            "SELECT id FROM workout_sessions WHERE user_id=? AND date=? AND day_type=?",
            (AAMEND_USER_ID, date, day_type),
        ).fetchall():
            _delete_session(r["id"])
    for d in {d for d, _ in _AAMEND_HISTORY_V2_ROWS}:
        conn.execute("DELETE FROM bodyweight_log WHERE user_id=? AND date=?", (AAMEND_USER_ID, d))

    # Pass 2: also remove anything already sitting under any date the
    # *current* seed_aamend_history.json covers, regardless of day_type.
    # This project has no persistent Railway volume yet (see CLAUDE.md), so
    # every deploy actually starts from a brand-new empty database:
    # _seed_aamend_account() already imports the current (corrected,
    # 21-session) file fresh on that first run, before this function ever
    # gets a chance to run. Without this pass, pass 1's old 14-row list
    # (which no longer matches what's already correctly seeded) would leave
    # everything untouched, and the unconditional _import_aamend_history()
    # call below would insert a second, duplicate copy of every session.
    if SEED_AAMEND_HISTORY_PATH.exists():
        try:
            history = json.loads(SEED_AAMEND_HISTORY_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            history = []
        for d in {s["date"] for s in history}:
            for r in conn.execute(
                "SELECT id FROM workout_sessions WHERE user_id=? AND date=?",
                (AAMEND_USER_ID, d),
            ).fetchall():
                _delete_session(r["id"])
            conn.execute("DELETE FROM bodyweight_log WHERE user_id=? AND date=?", (AAMEND_USER_ID, d))

    _import_aamend_history(conn)
    conn.execute(
        "INSERT INTO seed_meta (key, value) VALUES ('aamend_history_v3', datetime('now'))"
    )


def gen_code() -> str:
    alphabet = string.ascii_uppercase + string.digits
    alphabet = alphabet.replace("O", "").replace("0", "").replace("I", "").replace("1", "")
    return "".join(secrets.choice(alphabet) for _ in range(6))


CANONICAL_PLAN = {
    "push": [
        ("Rudern", None, "warmup", 1, "min"),
        ("Push-Up", "Brust", "reps", 3, "reps"),
        ("Benchpress", "Brust", "weight", 3, "kg"),
        ("Incline-Bench press", "Brust", "weight", 3, "kg"),
        ("Overhead Press", "Schulter", "weight", 3, "kg"),
        ("Laterial Rise", "Schulter", "weight", 3, "kg"),
        ("Dips Supported", "Trizeps", "weight", 3, "kg"),
        ("Arm Curl", "Bizeps", "weight", 3, "kg"),
    ],
    "pull": [
        ("Pull Up (Supported)", "Rücken", "weight", 3, "kg"),
        ("Lat Pull Down", "Rücken", "weight", 3, "kg"),
        ("Chest-Supported Row", "Rücken", "weight", 3, "kg"),
        ("Kabelziehen", "Trizeps", "weight", 3, "kg"),
        ("Bizeps Curl", "Bizeps", "weight", 3, "kg"),
        ("Rucksack", "Bauch", "weight", 3, "kg"),
        ("Plank", "Bauch", "time", 1, "sec"),
        ("Leg Raises or situps", "Bauch", "reps", 3, "reps"),
    ],
}


def _seed_plan_default(conn: sqlite3.Connection, user_id: str):
    for day, exercises in CANONICAL_PLAN.items():
        for i, (name, muscle, kind, sets, unit) in enumerate(exercises):
            conn.execute(
                """INSERT INTO plan_exercises
                   (user_id, day, position, name, muscle, kind, target_sets, unit, last_weight, last_reps)
                   VALUES (?,?,?,?,?,?,?,?,NULL,NULL)""",
                (user_id, day, i, name, muscle, kind, sets, unit),
            )


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _seed_from_legacy_archive(conn: sqlite3.Connection, user_id: str) -> bool:
    """If _archive_legacy_single_user_schema() ran on this DB (upgrading it
    from a pre-1.1 install), attach that real local history -- including
    anything logged in the window between the new code landing on disk and
    the next server restart -- to the first new-schema user, instead of the
    static seed_data.json snapshot. Returns True if it did anything.

    workout_sessions ids are preserved on purpose: session_sets itself was
    never renamed/touched by the archival step (only ALTER'd to add the
    `logged` column), so its existing session_id values already point at
    the right rows as long as the new workout_sessions rows reuse the same
    ids -- no session_sets rewrite needed."""
    if not _table_exists(conn, "workout_sessions_legacy_pre_1_1"):
        return False

    seeded_plan = False
    if _table_exists(conn, "plan_exercises_legacy_pre_1_1"):
        rows = conn.execute(
            "SELECT * FROM plan_exercises_legacy_pre_1_1 ORDER BY day, position"
        ).fetchall()
        for r in rows:
            conn.execute(
                """INSERT INTO plan_exercises
                   (user_id, day, position, name, muscle, kind, target_sets, unit, last_weight, last_reps)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (user_id, r["day"], r["position"], r["name"], r["muscle"], r["kind"],
                 r["target_sets"], r["unit"], r["last_weight"], r["last_reps"]),
            )
        seeded_plan = bool(rows)
    if not seeded_plan:
        _seed_plan_default(conn, user_id)

    if _table_exists(conn, "bodyweight_log_legacy_pre_1_1"):
        for r in conn.execute("SELECT * FROM bodyweight_log_legacy_pre_1_1 ORDER BY date").fetchall():
            conn.execute("INSERT INTO bodyweight_log (user_id, date, kg) VALUES (?,?,?)", (user_id, r["date"], r["kg"]))

    if _table_exists(conn, "profile_legacy_pre_1_1"):
        old = conn.execute("SELECT * FROM profile_legacy_pre_1_1 LIMIT 1").fetchone()
        if old is not None:
            old_cols = old.keys()
            g = lambda col: old[col] if col in old_cols else None
            conn.execute(
                """INSERT INTO profile (user_id, name, height_cm, weight_kg, age, sex, goal, updated_at)
                   VALUES (?,?,?,?,?,?,?,datetime('now'))
                   ON CONFLICT(user_id) DO UPDATE SET
                     name=excluded.name, height_cm=excluded.height_cm, weight_kg=excluded.weight_kg,
                     age=excluded.age, sex=excluded.sex, goal=excluded.goal""",
                (user_id, g("name"), g("height_cm"), g("weight_kg"), g("age"), g("sex"), g("goal")),
            )

    for r in conn.execute("SELECT * FROM workout_sessions_legacy_pre_1_1 ORDER BY id").fetchall():
        conn.execute(
            """INSERT INTO workout_sessions (id, user_id, date, day_type, duration_min, bodyweight_kg, created_at, finished)
               VALUES (?,?,?,?,?,?,?,?)""",
            (r["id"], user_id, r["date"], r["day_type"], r["duration_min"], r["bodyweight_kg"],
             r["created_at"], r["finished"]),
        )
    return True


def _seed_plan_from_history(conn: sqlite3.Connection, user_id: str):
    """Only used for the very first user on a fresh server -- inherits the
    Excel-derived history so existing data isn't orphaned (same pattern as
    the sibling project's 'first account inherits demo session'). Prefers a
    live legacy-schema archive over the static seed file when both exist --
    see _seed_from_legacy_archive()."""
    if _seed_from_legacy_archive(conn, user_id):
        return
    if not SEED_PATH.exists():
        _seed_plan_default(conn, user_id)
        return
    data = json.loads(SEED_PATH.read_text())
    plan = data.get("plan", {})
    for day, exercises in plan.items():
        for i, ex in enumerate(exercises):
            name = ex["name"]
            if name == "Pull Up (Supported":
                name = "Pull Up (Supported)"
            conn.execute(
                """INSERT INTO plan_exercises
                   (user_id, day, position, name, muscle, kind, target_sets, unit, last_weight, last_reps)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (user_id, day, i, name, ex.get("muscle"), ex["kind"], ex.get("target_sets", 3),
                 ex.get("unit", "kg"), ex.get("last_weight"), ex.get("last_reps")),
            )
    for bw in data.get("bodyweight", []):
        conn.execute("INSERT INTO bodyweight_log (user_id, date, kg) VALUES (?,?,?)", (user_id, bw["date"], bw["kg"]))
        conn.execute(
            """INSERT INTO profile (user_id, weight_kg, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET weight_kg=excluded.weight_kg""",
            (user_id, bw["kg"], bw["date"]),
        )
    for sess in data.get("sessions", []):
        exs = sess.get("exercises", [])
        day_type = "push" if any(e.get("day") == "push" for e in exs) else "pull"
        cur = conn.execute(
            "INSERT INTO workout_sessions (user_id, date, day_type, created_at, finished) VALUES (?,?,?,?,1)",
            (user_id, sess["date"], day_type, sess["date"]),
        )
        session_id = cur.lastrowid
        for ex in exs:
            name = ex["name"]
            if name == "Pull Up (Supported":
                name = "Pull Up (Supported)"
            for idx, s in enumerate(ex.get("sets", [])):
                weight = s.get("weight") if isinstance(s, dict) else None
                reps = s.get("reps") if isinstance(s, dict) else None
                value_text = str(s["value"]) if isinstance(s, dict) and "value" in s else None
                conn.execute(
                    """INSERT INTO session_sets (session_id, exercise_name, set_index, weight, reps, value_text, logged)
                       VALUES (?,?,?,?,?,?,1)""",
                    (session_id, name, idx, weight, reps, value_text),
                )


def get_or_create_user(user_id: str, display_name: str | None = None) -> dict:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if row:
            return dict(row)
        is_first_user = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0
        code = gen_code()
        while conn.execute("SELECT 1 FROM users WHERE code=?", (code,)).fetchone():
            code = gen_code()
        conn.execute(
            "INSERT INTO users (id, code, display_name, created_at) VALUES (?,?,?,datetime('now'))",
            (user_id, code, display_name or "Ich"),
        )
        conn.execute("INSERT INTO profile (user_id, updated_at) VALUES (?, datetime('now'))", (user_id,))
        if is_first_user:
            _seed_plan_from_history(conn, user_id)
        else:
            _seed_plan_default(conn, user_id)
        return dict(conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())


def get_user_by_code(code: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE code=?", (code.strip().upper(),)).fetchone()
        return dict(row) if row else None


# ---------- profile ----------

def get_profile(user_id: str) -> dict:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM profile WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            conn.execute("INSERT INTO profile (user_id, updated_at) VALUES (?, datetime('now'))", (user_id,))
            row = conn.execute("SELECT * FROM profile WHERE user_id=?", (user_id,)).fetchone()
        user = conn.execute("SELECT code, display_name FROM users WHERE id=?", (user_id,)).fetchone()
    d = dict(row)
    if user:
        d["code"] = user["code"]
        d["display_name"] = user["display_name"]
    return d


def update_profile(user_id: str, fields: dict) -> dict:
    allowed = {"height_cm", "weight_kg", "age", "sex", "goal"}
    display_name = fields.pop("name", None)
    fields = {k: v for k, v in fields.items() if k in allowed}
    with get_conn() as conn:
        conn.execute("INSERT OR IGNORE INTO profile (user_id, updated_at) VALUES (?, datetime('now'))", (user_id,))
        if fields:
            set_clause = ", ".join(f"{k}=?" for k in fields)
            conn.execute(
                f"UPDATE profile SET {set_clause}, updated_at=datetime('now') WHERE user_id=?",
                list(fields.values()) + [user_id],
            )
        if display_name is not None:
            conn.execute("UPDATE users SET display_name=? WHERE id=?", (display_name, user_id))
        if "weight_kg" in fields and fields["weight_kg"] is not None:
            conn.execute("INSERT INTO bodyweight_log (user_id, date, kg) VALUES (?, date('now'), ?)",
                         (user_id, fields["weight_kg"]))
    return get_profile(user_id)


# ---------- plan ----------

def get_plan(user_id: str) -> dict:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM plan_exercises WHERE user_id=? ORDER BY day, position", (user_id,)
        ).fetchall()
    plan = {"push": [], "pull": []}
    for r in rows:
        d = dict(r)
        plan.setdefault(d["day"], []).append(d)
    return plan


def save_plan(user_id: str, plan: dict) -> dict:
    with get_conn() as conn:
        conn.execute("DELETE FROM plan_exercises WHERE user_id=?", (user_id,))
        for day, exercises in plan.items():
            for i, ex in enumerate(exercises):
                conn.execute(
                    """INSERT INTO plan_exercises
                       (user_id, day, position, name, muscle, kind, target_sets, unit, last_weight, last_reps)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (user_id, day, i, ex["name"], ex.get("muscle"), ex.get("kind", "weight"),
                     ex.get("target_sets", 3), ex.get("unit", "kg"),
                     ex.get("last_weight"), ex.get("last_reps")),
                )
    return get_plan(user_id)


def next_day_type(user_id: str) -> str:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT day_type FROM workout_sessions WHERE user_id=? AND finished=1 ORDER BY date DESC, id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    if not row:
        return "push"
    return "pull" if row["day_type"] == "push" else "push"


# ---------- workouts ----------

def create_session(user_id: str, day_type: str, date: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO workout_sessions (user_id, date, day_type, created_at, finished) VALUES (?,?,?,datetime('now'),0)",
            (user_id, date, day_type),
        )
        return cur.lastrowid


def log_set(user_id: str, session_id: int, exercise_name: str, set_index: int,
            weight=None, reps=None, value_text=None, logged: bool = True):
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM session_sets WHERE session_id=? AND exercise_name=? AND set_index=?",
            (session_id, exercise_name, set_index),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE session_sets SET weight=?, reps=?, value_text=?, logged=? WHERE id=?",
                (weight, reps, value_text, 1 if logged else 0, existing["id"]),
            )
        else:
            conn.execute(
                """INSERT INTO session_sets (session_id, exercise_name, set_index, weight, reps, value_text, logged)
                   VALUES (?,?,?,?,?,?,?)""",
                (session_id, exercise_name, set_index, weight, reps, value_text, 1 if logged else 0),
            )
        if logged and (weight is not None or reps is not None):
            update_fields, params = [], []
            if weight is not None:
                update_fields.append("last_weight=?"); params.append(weight)
            if reps is not None:
                # Update 1.8: merge into the per-set-index map instead of a
                # single shared last_reps column. Set 1 usually runs higher
                # reps than set 3 in a pyramid (e.g. 12/10/8), so each set
                # index keeps its own remembered value rather than the most
                # recently logged set overwriting all the others.
                row = conn.execute(
                    "SELECT last_reps_by_set FROM plan_exercises WHERE user_id=? AND name=?",
                    (user_id, exercise_name),
                ).fetchone()
                try:
                    by_set = json.loads(row["last_reps_by_set"]) if row and row["last_reps_by_set"] else {}
                except (TypeError, ValueError):
                    by_set = {}
                by_set[str(set_index)] = reps
                update_fields.append("last_reps=?"); params.append(reps)
                update_fields.append("last_reps_by_set=?"); params.append(json.dumps(by_set))
            if update_fields:
                params += [user_id, exercise_name]
                conn.execute(
                    f"UPDATE plan_exercises SET {', '.join(update_fields)} WHERE user_id=? AND name=?", params,
                )


def finish_session(user_id: str, session_id: int, duration_min=None, bodyweight_kg=None):
    with get_conn() as conn:
        conn.execute(
            "UPDATE workout_sessions SET finished=1, duration_min=?, bodyweight_kg=? WHERE id=? AND user_id=?",
            (duration_min, bodyweight_kg, session_id, user_id),
        )
        if bodyweight_kg is not None:
            sess = conn.execute("SELECT date FROM workout_sessions WHERE id=?", (session_id,)).fetchone()
            conn.execute("INSERT INTO bodyweight_log (user_id, date, kg) VALUES (?,?,?)", (user_id, sess["date"], bodyweight_kg))
            conn.execute(
                """INSERT INTO profile (user_id, weight_kg, updated_at) VALUES (?, ?, datetime('now'))
                   ON CONFLICT(user_id) DO UPDATE SET weight_kg=excluded.weight_kg, updated_at=excluded.updated_at""",
                (user_id, bodyweight_kg),
            )


def get_session(user_id: str, session_id: int) -> dict | None:
    with get_conn() as conn:
        sess = conn.execute("SELECT * FROM workout_sessions WHERE id=? AND user_id=?", (session_id, user_id)).fetchone()
        if not sess:
            return None
        sets = conn.execute(
            "SELECT * FROM session_sets WHERE session_id=? ORDER BY exercise_name, set_index", (session_id,)
        ).fetchall()
    result = dict(sess)
    result["sets"] = [dict(s) for s in sets]
    return result


def get_history(user_id: str, limit: int = 60) -> list[dict]:
    # Update 1.8.5: no longer filters to finished=1 only. A session that was
    # started but never explicitly "finished" (e.g. the user closed the app
    # mid-workout) used to be completely invisible in Kalender/Tabelle even
    # though real sets were logged against it -- this is what made a real
    # workout ("mein training von gestern wird iwi nicht angezeigt") vanish.
    # Every other consumer of workout_sessions that cares about "finished"
    # (gamification, streaks, progress charts) queries finished=1 directly
    # and does NOT go through get_history(), so relaxing this filter only
    # affects what's shown in the history/table views, not XP or streaks.
    with get_conn() as conn:
        sessions = conn.execute(
            "SELECT * FROM workout_sessions WHERE user_id=? ORDER BY date DESC, id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        result = []
        for sess in sessions:
            sets = conn.execute(
                "SELECT * FROM session_sets WHERE session_id=? ORDER BY exercise_name, set_index", (sess["id"],)
            ).fetchall()
            d = dict(sess)
            d["sets"] = [dict(s) for s in sets]
            result.append(d)
        return result


def get_progress(user_id: str) -> dict:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT s.date as date, ss.exercise_name as name, MAX(ss.weight) as weight, MAX(ss.reps) as reps
               FROM session_sets ss JOIN workout_sessions s ON s.id = ss.session_id
               WHERE s.user_id=? AND s.finished=1 AND ss.logged=1 AND ss.weight IS NOT NULL
               GROUP BY s.id, ss.exercise_name ORDER BY s.date ASC""",
            (user_id,),
        ).fetchall()
        reps_rows = conn.execute(
            """SELECT s.date as date, ss.exercise_name as name, MAX(ss.reps) as reps
               FROM session_sets ss JOIN workout_sessions s ON s.id = ss.session_id
               WHERE s.user_id=? AND s.finished=1 AND ss.logged=1 AND ss.weight IS NULL AND ss.reps IS NOT NULL
               GROUP BY s.id, ss.exercise_name ORDER BY s.date ASC""",
            (user_id,),
        ).fetchall()
        bw = conn.execute("SELECT date, kg FROM bodyweight_log WHERE user_id=? ORDER BY date ASC", (user_id,)).fetchall()

    by_exercise: dict = {}
    for r in rows:
        by_exercise.setdefault(r["name"], []).append({"date": r["date"], "weight": r["weight"], "reps": r["reps"]})
    reps_by_exercise: dict = {}
    for r in reps_rows:
        reps_by_exercise.setdefault(r["name"], []).append({"date": r["date"], "reps": r["reps"]})

    return {
        "exercises": by_exercise,
        "reps_exercises": reps_by_exercise,
        "bodyweight": [dict(b) for b in bw],
    }


# ---------- nutrition ----------

def get_nutrition_day(user_id: str, date: str) -> dict:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM nutrition_log WHERE user_id=? AND date=?", (user_id, date)).fetchone()
        supplements = conn.execute("SELECT * FROM supplements WHERE user_id=? ORDER BY position", (user_id,)).fetchall()
        taken = conn.execute(
            "SELECT supplement_id FROM supplement_log WHERE user_id=? AND date=? AND taken=1", (user_id, date)
        ).fetchall()
    taken_ids = {t["supplement_id"] for t in taken}
    return {
        "date": date,
        "calories": row["calories"] if row else None,
        "protein_g": row["protein_g"] if row else None,
        "supplements": [{"id": s["id"], "name": s["name"], "taken": s["id"] in taken_ids} for s in supplements],
    }


def save_nutrition_day(user_id: str, date: str, calories=None, protein_g=None) -> dict:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO nutrition_log (user_id, date, calories, protein_g) VALUES (?,?,?,?)
               ON CONFLICT(user_id, date) DO UPDATE SET calories=excluded.calories, protein_g=excluded.protein_g""",
            (user_id, date, calories, protein_g),
        )
    return get_nutrition_day(user_id, date)


def get_nutrition_history(user_id: str, limit: int = 30) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM nutrition_log WHERE user_id=? ORDER BY date DESC LIMIT ?", (user_id, limit)
        ).fetchall()
    return [dict(r) for r in rows]


def add_supplement(user_id: str, name: str) -> dict:
    with get_conn() as conn:
        pos = conn.execute("SELECT COALESCE(MAX(position),-1)+1 p FROM supplements WHERE user_id=?", (user_id,)).fetchone()["p"]
        cur = conn.execute("INSERT INTO supplements (user_id, name, position) VALUES (?,?,?)", (user_id, name, pos))
        return {"id": cur.lastrowid, "name": name, "position": pos}


def delete_supplement(user_id: str, supplement_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM supplements WHERE id=? AND user_id=?", (supplement_id, user_id))
        conn.execute("DELETE FROM supplement_log WHERE supplement_id=? AND user_id=?", (supplement_id, user_id))


def toggle_supplement(user_id: str, supplement_id: int, date: str, taken: bool):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO supplement_log (user_id, supplement_id, date, taken) VALUES (?,?,?,?)
               ON CONFLICT(user_id, supplement_id, date) DO UPDATE SET taken=excluded.taken""",
            (user_id, supplement_id, date, 1 if taken else 0),
        )


# ---------- habits ----------

def get_habit_day(user_id: str, date: str) -> dict:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM habit_log WHERE user_id=? AND date=?", (user_id, date)).fetchone()
    if not row:
        return {"date": date, "alcohol": False, "smoke": False, "drugs": False}
    return {"date": row["date"], "alcohol": bool(row["alcohol"]), "smoke": bool(row["smoke"]), "drugs": bool(row["drugs"])}


def save_habit_day(user_id: str, date: str, alcohol: bool, smoke: bool, drugs: bool) -> dict:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO habit_log (user_id, date, alcohol, smoke, drugs) VALUES (?,?,?,?,?)
               ON CONFLICT(user_id, date) DO UPDATE SET alcohol=excluded.alcohol, smoke=excluded.smoke, drugs=excluded.drugs""",
            (user_id, date, int(alcohol), int(smoke), int(drugs)),
        )
    return get_habit_day(user_id, date)


def get_habit_history(user_id: str, limit_days: int = 90) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM habit_log WHERE user_id=? ORDER BY date DESC LIMIT ?", (user_id, limit_days)
        ).fetchall()
    return [{"date": r["date"], "alcohol": bool(r["alcohol"]), "smoke": bool(r["smoke"]), "drugs": bool(r["drugs"])} for r in rows]


def _account_created_date(conn, user_id: str) -> datetime.date:
    row = conn.execute("SELECT created_at FROM users WHERE id=?", (user_id,)).fetchone()
    if not row or not row["created_at"]:
        return datetime.date.today()
    try:
        return datetime.datetime.strptime(row["created_at"][:10], "%Y-%m-%d").date()
    except ValueError:
        return datetime.date.today()


def clean_streaks(user_id: str) -> dict:
    today = datetime.date.today()
    with get_conn() as conn:
        created = _account_created_date(conn, user_id)
        result = {}
        for field in ("alcohol", "smoke", "drugs"):
            row = conn.execute(
                f"SELECT date FROM habit_log WHERE user_id=? AND {field}=1 ORDER BY date DESC LIMIT 1", (user_id,)
            ).fetchone()
            if row:
                last_bad = datetime.datetime.strptime(row["date"], "%Y-%m-%d").date()
                since = last_bad + datetime.timedelta(days=1)
            else:
                since = created
            days = max(0, (today - since).days)
            result[field] = days
        combined_row = conn.execute(
            "SELECT date FROM habit_log WHERE user_id=? AND (alcohol=1 OR smoke=1 OR drugs=1) ORDER BY date DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    if combined_row:
        last_bad = datetime.datetime.strptime(combined_row["date"], "%Y-%m-%d").date()
        since = last_bad + datetime.timedelta(days=1)
    else:
        since = created
    result["combined"] = max(0, (today - since).days)
    return result


# ---------- streak (workout consistency, "constant grow") ----------

def workout_streak(user_id: str) -> dict:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT date FROM workout_sessions WHERE user_id=? AND finished=1", (user_id,)
        ).fetchall()
    dates = {datetime.datetime.strptime(r["date"], "%Y-%m-%d").date() for r in rows}
    if not dates:
        return {"windows": 0, "days": 0, "stage": 0, "last_workout": None}

    today = datetime.date.today()
    windows = 0
    window_end = today
    while True:
        window_start = window_end - datetime.timedelta(days=1)
        if window_start in dates or window_end in dates:
            windows += 1
            window_end = window_start - datetime.timedelta(days=1)
        else:
            break
        if windows > 400:
            break
    stage = min(5, windows // 2)
    return {"windows": windows, "days": windows * 2, "stage": stage, "last_workout": max(dates).isoformat()}


# ---------- gamification ----------

ACHIEVEMENTS = [
    {"id": "first_workout", "name": "Erstes Workout", "desc": "Dein erstes Workout abgeschlossen.", "icon": "trophy"},
    {"id": "ten_workouts", "name": "Stammgast", "desc": "10 Workouts abgeschlossen.", "icon": "trophy"},
    {"id": "fifty_workouts", "name": "Routine", "desc": "50 Workouts abgeschlossen.", "icon": "trophy"},
    {"id": "fifty_sets", "name": "Volumen-Fan", "desc": "50 Sätze geloggt.", "icon": "barbell"},
    {"id": "streak_3", "name": "In Fahrt", "desc": "3 Trainings-Fenster am Stück, ohne Pause auszulassen.", "icon": "flame"},
    {"id": "streak_7", "name": "Eine Woche dran", "desc": "7 Trainings-Fenster am Stück.", "icon": "flame"},
    {"id": "streak_15", "name": "Unaufhaltsam", "desc": "15 Trainings-Fenster am Stück.", "icon": "flame"},
    {"id": "first_friend", "name": "Nicht allein", "desc": "Ersten Freund hinzugefügt.", "icon": "users"},
    {"id": "three_friends", "name": "Team-Player", "desc": "Mit 3 Freunden vernetzt.", "icon": "users"},
    {"id": "level_5", "name": "Level 5", "desc": "Level 5 erreicht.", "icon": "star"},
    {"id": "level_10", "name": "Level 10", "desc": "Level 10 erreicht.", "icon": "star"},
    {"id": "clean_7", "name": "7 Tage clean", "desc": "7 Tage ohne Alkohol, Rauchen oder Drogen.", "icon": "leaf"},
    {"id": "clean_30", "name": "30 Tage clean", "desc": "30 Tage ohne Alkohol, Rauchen oder Drogen.", "icon": "leaf"},
    {"id": "nutrition_7", "name": "Ernährung im Blick", "desc": "7 Tage Ernährung geloggt.", "icon": "apple"},
]
ACHIEVEMENTS_BY_ID = {a["id"]: a for a in ACHIEVEMENTS}

LEVEL_THRESHOLDS = [0, 60, 150, 280, 450, 660, 920, 1230, 1600, 2050, 2600, 3250, 4000, 4850, 5800, 6850]


def _level_for_xp(xp: float):
    level = 1
    for i, t in enumerate(LEVEL_THRESHOLDS):
        if xp >= t:
            level = i + 1
    idx = min(level, len(LEVEL_THRESHOLDS)) - 1
    cur_threshold = LEVEL_THRESHOLDS[idx]
    next_threshold = LEVEL_THRESHOLDS[idx + 1] if idx + 1 < len(LEVEL_THRESHOLDS) else cur_threshold + 1000
    progress = 0.0 if next_threshold == cur_threshold else (xp - cur_threshold) / (next_threshold - cur_threshold)
    return level, max(0.0, min(1.0, progress)), cur_threshold, next_threshold


def _raw_stats(conn, user_id: str) -> dict:
    sessions = conn.execute(
        "SELECT COUNT(*) c FROM workout_sessions WHERE user_id=? AND finished=1", (user_id,)
    ).fetchone()["c"]
    sets = conn.execute(
        """SELECT COUNT(*) c FROM session_sets ss JOIN workout_sessions s ON s.id=ss.session_id
           WHERE s.user_id=? AND s.finished=1 AND ss.logged=1""", (user_id,)
    ).fetchone()["c"]
    nutrition_days = conn.execute("SELECT COUNT(*) c FROM nutrition_log WHERE user_id=?", (user_id,)).fetchone()["c"]
    habit_days = conn.execute("SELECT COUNT(*) c FROM habit_log WHERE user_id=?", (user_id,)).fetchone()["c"]
    friends = conn.execute("SELECT COUNT(*) c FROM friendships WHERE user_id=?", (user_id,)).fetchone()["c"]
    achievements = conn.execute("SELECT COUNT(*) c FROM user_achievements WHERE user_id=?", (user_id,)).fetchone()["c"]
    return {"sessions": sessions, "sets": sets, "nutrition_days": nutrition_days, "habit_days": habit_days,
            "friends": friends, "achievements": achievements}


def gamification_status(user_id: str, unlock_new: bool = True) -> dict:
    streak = workout_streak(user_id)
    clean = clean_streaks(user_id)
    with get_conn() as conn:
        stats = _raw_stats(conn, user_id)
        xp = (stats["sessions"] * 15 + stats["sets"] * 1 + stats["nutrition_days"] * 3
              + stats["habit_days"] * 2 + stats["achievements"] * 20 + streak["windows"] * 8)
        level, progress, cur_t, next_t = _level_for_xp(xp)

        already = {r["achievement_id"] for r in conn.execute(
            "SELECT achievement_id FROM user_achievements WHERE user_id=?", (user_id,)
        ).fetchall()}

        check_ctx = {**stats, "level": level, "streak_windows": streak["windows"], "clean_days": clean["combined"]}
        newly_unlocked = []
        if unlock_new:
            checks = {
                "first_workout": check_ctx["sessions"] >= 1,
                "ten_workouts": check_ctx["sessions"] >= 10,
                "fifty_workouts": check_ctx["sessions"] >= 50,
                "fifty_sets": check_ctx["sets"] >= 50,
                "streak_3": check_ctx["streak_windows"] >= 3,
                "streak_7": check_ctx["streak_windows"] >= 7,
                "streak_15": check_ctx["streak_windows"] >= 15,
                "first_friend": check_ctx["friends"] >= 1,
                "three_friends": check_ctx["friends"] >= 3,
                "level_5": check_ctx["level"] >= 5,
                "level_10": check_ctx["level"] >= 10,
                "clean_7": check_ctx["clean_days"] >= 7,
                "clean_30": check_ctx["clean_days"] >= 30,
                "nutrition_7": check_ctx["nutrition_days"] >= 7,
            }
            for aid, passed in checks.items():
                if passed and aid not in already:
                    conn.execute(
                        "INSERT OR IGNORE INTO user_achievements (user_id, achievement_id, unlocked_at) VALUES (?,?,datetime('now'))",
                        (user_id, aid),
                    )
                    newly_unlocked.append(ACHIEVEMENTS_BY_ID[aid])
                    already.add(aid)

        unlocked_rows = conn.execute(
            "SELECT achievement_id, unlocked_at FROM user_achievements WHERE user_id=?", (user_id,)
        ).fetchall()

    unlocked_map = {r["achievement_id"]: r["unlocked_at"] for r in unlocked_rows}
    achievements_out = []
    for a in ACHIEVEMENTS:
        achievements_out.append({**a, "unlocked": a["id"] in unlocked_map, "unlocked_at": unlocked_map.get(a["id"])})

    return {
        "xp": xp, "level": level, "progress": progress,
        "xp_current_level": cur_t, "xp_next_level": next_t,
        "streak": streak, "clean_streaks": clean,
        "stats": stats,
        "achievements": achievements_out,
        "newly_unlocked": newly_unlocked,
    }


# ---------- social ----------

def add_friend(user_id: str, friend_code: str) -> dict:
    friend = get_user_by_code(friend_code)
    if not friend:
        raise ValueError("Kein Nutzer mit diesem Code gefunden.")
    if friend["id"] == user_id:
        raise ValueError("Du kannst dich nicht selbst hinzufügen.")
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO friendships (user_id, friend_user_id, created_at) VALUES (?,?,datetime('now'))",
            (user_id, friend["id"]),
        )
        conn.execute(
            "INSERT OR IGNORE INTO friendships (user_id, friend_user_id, created_at) VALUES (?,?,datetime('now'))",
            (friend["id"], user_id),
        )
    return friend


def remove_friend(user_id: str, friend_user_id: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM friendships WHERE user_id=? AND friend_user_id=?", (user_id, friend_user_id))
        conn.execute("DELETE FROM friendships WHERE user_id=? AND friend_user_id=?", (friend_user_id, user_id))


def list_friends(user_id: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT u.id, u.code, u.display_name FROM friendships f
               JOIN users u ON u.id = f.friend_user_id WHERE f.user_id=?""",
            (user_id,),
        ).fetchall()
    out = []
    for r in rows:
        status = gamification_status(r["id"], unlock_new=False)
        out.append({
            "id": r["id"], "code": r["code"], "display_name": r["display_name"],
            "level": status["level"], "xp": status["xp"],
            "streak_days": status["streak"]["days"],
            "last_workout": status["streak"]["last_workout"],
        })
    out.sort(key=lambda f: f["xp"], reverse=True)
    return out
