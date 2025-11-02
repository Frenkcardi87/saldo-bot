# bot_slots_flow_DB_layer_fixed.py
import os
import pathlib
import sqlite3
import logging

log = logging.getLogger(__name__)

# --- Impostazioni di sicurezza per Railway Volume ---
os.environ.setdefault("TMPDIR", "/var/data")
os.environ.setdefault("TEMP", "/var/data")
os.environ.setdefault("TMP", "/var/data")
os.environ.setdefault("SQLITE_TMPDIR", "/var/data")

DEFAULT_DB_PATH = "/var/data/kwh_slots.db"


def open_sqlite(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(
        db_path,
        timeout=30,
        check_same_thread=False,
        isolation_level=None,
        cached_statements=0
    )
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode=OFF;")
    cur.execute("PRAGMA synchronous=OFF;")
    cur.execute("PRAGMA temp_store=MEMORY;")
    cur.execute("PRAGMA mmap_size=0;")
    cur.execute("PRAGMA locking_mode=NORMAL;")
    return conn


class DB:
    def __init__(self, db_path_env: str | None):
        p = (db_path_env or "").strip()
        self.path = p if p else DEFAULT_DB_PATH

        dirpath = os.path.dirname(self.path) or "/var/data"
        pathlib.Path(dirpath).mkdir(parents=True, exist_ok=True)

        testfile = os.path.join(dirpath, ".rw_test")
        try:
            with open(testfile, "w") as f:
                f.write("ok")
            os.remove(testfile)
            log.info("DB dir write test OK on %s", dirpath)
        except Exception:
            log.exception("DB dir write test FAILED on %s", dirpath)

        self.conn: sqlite3.Connection = open_sqlite(self.path)
        self._init_db()

    def _init_db(self):
        try:
            with self.conn as con:
                cur = con.cursor()

                # ----------------- INIZIO SCHEMA -----------------
                cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY,
                    chat_id INTEGER,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    approved INTEGER DEFAULT 1,
                    slot1_kwh REAL DEFAULT 0,
                    slot3_kwh REAL DEFAULT 0,
                    slot5_kwh REAL DEFAULT 0,
                    slot8_kwh REAL DEFAULT 0,
                    wallet_kwh REAL DEFAULT 0,
                    created_at TEXT,
                    updated_at TEXT
                );
                """)

                cur.execute("""
                CREATE TABLE IF NOT EXISTS pending (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    slot_type TEXT,
                    kwh REAL,
                    photo_path TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                );
                """)

                cur.execute("""
                CREATE TABLE IF NOT EXISTS notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    text TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                );
                """)

                cur.execute("""
                CREATE TABLE IF NOT EXISTS recharges (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    amount REAL,
                    approved INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                );
                """)

                cur.execute("""
                CREATE TABLE IF NOT EXISTS admin_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    admin_id INTEGER,
                    action TEXT,
                    target_user_id INTEGER,
                    note TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                """)
                # ------------------ FINE SCHEMA -------------------

                cur.close()
            log.info("DB init OK on %s", self.path)
        except sqlite3.OperationalError:
            log.exception('DB init failed at %s', self.path)
            raise


def _init_db_instance() -> DB:
    DB_PATH = os.environ.get("DB_PATH", DEFAULT_DB_PATH).strip()
    try:
        db = DB(DB_PATH)
        return db
    except Exception:
        log.exception("DB init failed (path=%s). Falling back to in-memory for boot.", DB_PATH)
        mem = DB(":memory:")
        return mem


DBI = _init_db_instance()
