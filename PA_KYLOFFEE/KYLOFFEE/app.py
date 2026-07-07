import os
import re
import ssl
import sqlite3
import uuid
from io import BytesIO
from functools import wraps
from pathlib import Path
from datetime import date, datetime, timedelta

from dotenv import load_dotenv
import resend
from flask import (
    Flask,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

try:
    import cloudinary
    import cloudinary.uploader
except ImportError:  # pragma: no cover - only used when dependency is missing.
    cloudinary = None

try:
    import qrcode
except ImportError:  # pragma: no cover - app still runs before dependency install.
    qrcode = None

BASE_DIR = Path(__file__).resolve().parent
DATABASE = BASE_DIR / "database.db"
load_dotenv(BASE_DIR / ".env")


# ======================
# Resend Email Configuration
# ======================
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "").strip()
RESEND_FROM_EMAIL = os.getenv("RESEND_FROM_EMAIL", "noreply@example.com").strip()

if RESEND_API_KEY:
    resend.api_key = RESEND_API_KEY

DB_HOST = os.getenv("DB_HOST", "").strip()
DB_PORT = int(os.getenv("DB_PORT", "4000")) if os.getenv("DB_PORT") else 4000
DB_USER = os.getenv("DB_USER", "").strip()
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME = os.getenv("DB_NAME", "").strip()
DB_SSL_CA = os.getenv("DB_SSL_CA", "").strip()

# Set DB_FORCE_SQLITE=1 in .env when you want the app to ignore remote DB
# and use the local database.db file instead.
DB_FORCE_SQLITE = os.getenv("DB_FORCE_SQLITE", "0").strip().lower() in {"1", "true", "yes", "on"}

# Keep this enabled by default so wrong/expired TiDB credentials do not block
# login/register during local development. Set DB_FALLBACK_SQLITE=0 to fail fast.
DB_FALLBACK_SQLITE = os.getenv("DB_FALLBACK_SQLITE", "1").strip().lower() not in {"0", "false", "no", "off"}

DEBUG_DB_CONFIG = os.getenv("DEBUG_DB_CONFIG", "0").strip().lower() in {"1", "true", "yes", "on"}
if DEBUG_DB_CONFIG:
    print("DB_HOST:", DB_HOST or "<empty - using SQLite>")
    print("DB_PORT:", DB_PORT)
    print("DB_USER:", DB_USER or "<empty>")
    print("DB_NAME:", DB_NAME or "<empty>")
    print("DB_PASSWORD exists:", bool(DB_PASSWORD))
    print("DB_FORCE_SQLITE:", DB_FORCE_SQLITE)
    print("DB_FALLBACK_SQLITE:", DB_FALLBACK_SQLITE)

try:
    import pymysql
except ImportError:
    pymysql = None

DB_REMOTE_CONFIGURED = bool(DB_HOST and DB_USER and DB_NAME and pymysql is not None)
DB_USE_MYSQL = DB_REMOTE_CONFIGURED and not DB_FORCE_SQLITE
REMOTE_DB_FAILED = False


def get_mysql_ssl_options():
    """Return PyMySQL SSL options that work for TiDB Cloud/MySQL.

    If DB_SSL_CA is filled but the file is missing, we do not crash.
    The app will try a default encrypted connection instead.
    """
    if not DB_SSL_CA:
        if "tidbcloud.com" in DB_HOST.lower() or DB_PORT == 4000:
            return {"ssl": {}}
        return {}

    ssl_ca_path = Path(DB_SSL_CA)
    if not ssl_ca_path.is_absolute():
        ssl_ca_path = BASE_DIR / ssl_ca_path

    if ssl_ca_path.exists():
        return {"ssl": {"ca": str(ssl_ca_path)}}

    print(f"WARNING: DB_SSL_CA file was not found: {ssl_ca_path}. Using default SSL instead.")
    return {"ssl": {}}


class DatabaseConnection:
    def __init__(self, conn, is_mysql=False):
        self.conn = conn
        self.is_mysql = is_mysql

    def adapt_sql(self, sql):
        if self.is_mysql:
            return sql.replace("?", "%s")
        return sql

    def cursor(self):
        return self.conn.cursor()

    def execute(self, sql, params=()):
        sql = self.adapt_sql(sql)
        if self.is_mysql:
            cursor = self.conn.cursor()
            cursor.execute(sql, params)
            return cursor
        return self.conn.execute(sql, params)

    def executescript(self, script):
        if hasattr(self.conn, "executescript"):
            return self.conn.executescript(script)
        for statement in script.split(";"):
            statement = statement.strip()
            if statement:
                self.execute(statement)

    def commit(self):
        return self.conn.commit()

    def rollback(self):
        return self.conn.rollback()

    def close(self):
        return self.conn.close()

    def __getattr__(self, name):
        return getattr(self.conn, name)

STAFF_DEFAULT_PASSWORD = os.getenv("STAFF_DEFAULT_PASSWORD", "kyloffee123")
MIN_MENU_PRICE = 500
CATEGORY_NAME_MAX_LENGTH = 100
STAFF_POSITIONS = ["Kasir"]
STAFF_STATUSES = ["Aktif", "Cuti", "Nonaktif"]
CASHIER_ROLE = "staff"
CASHIER_ROLE_ALIASES = ("staff", "kasir", "cashier")
LEGACY_CASHIER_OWNER_WINDOW_MINUTES = int(os.getenv("LEGACY_CASHIER_OWNER_WINDOW_MINUTES", "120"))

app = Flask(
    __name__,
    template_folder=str(BASE_DIR / "templates"),
    static_folder=str(BASE_DIR / "static"),
)
app.config["SECRET_KEY"] = os.environ.get(
    "SECRET_KEY",
    "dev-secret-key-change-this-before-production",
)
app.config["UPLOAD_FOLDER"] = BASE_DIR / "static" / "uploads" / "menu"
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024
MYSQL_SSL_OPTIONS = get_mysql_ssl_options() if DB_USE_MYSQL else {}

CLOUDINARY_CLOUD_NAME = os.environ.get("CLOUDINARY_CLOUD_NAME", "").strip()
CLOUDINARY_API_KEY = os.environ.get("CLOUDINARY_API_KEY", "").strip()
CLOUDINARY_API_SECRET = os.environ.get("CLOUDINARY_API_SECRET", "").strip()
CLOUDINARY_FOLDER = os.environ.get("CLOUDINARY_FOLDER", "kyloffee/menu").strip().strip("/")

if cloudinary and CLOUDINARY_CLOUD_NAME and CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET:
    cloudinary.config(
        cloud_name=CLOUDINARY_CLOUD_NAME,
        api_key=CLOUDINARY_API_KEY,
        api_secret=CLOUDINARY_API_SECRET,
        secure=True,
    )


# ======================
# Resend Email Helper
# ======================
def send_email(to_email, subject, html_content):
    if not RESEND_API_KEY:
        app.logger.warning("RESEND_API_KEY belum tersedia. Email tidak dikirim.")
        return False

    try:
        resend.Emails.send(
            {
                "from": RESEND_FROM_EMAIL,
                "to": [to_email],
                "subject": subject,
                "html": html_content,
            }
        )
        return True
    except Exception as exc:
        app.logger.error("Resend gagal mengirim email: %s", exc)
        return False


def get_db():

    """Get the database connection for the current Flask request.

    Priority:
    1. Remote MySQL/TiDB when .env is complete and DB_FORCE_SQLITE is not enabled.
    2. Local SQLite database.db when remote DB is disabled or unavailable.

    This prevents a wrong TiDB password from making login/register unusable.
    """
    global REMOTE_DB_FAILED

    if "db" not in g:
        should_try_mysql = DB_USE_MYSQL and not (REMOTE_DB_FAILED and DB_FALLBACK_SQLITE)

        if should_try_mysql:
            try:
                conn = pymysql.connect(
                    host=DB_HOST,
                    port=int(DB_PORT),
                    user=DB_USER,
                    password=DB_PASSWORD,
                    database=DB_NAME,
                    cursorclass=pymysql.cursors.DictCursor,
                    autocommit=False,
                    **MYSQL_SSL_OPTIONS,
                )
                g.db = DatabaseConnection(conn, is_mysql=True)
                return g.db
            except Exception as exc:
                REMOTE_DB_FAILED = True
                safe_message = str(exc)

                if not DB_FALLBACK_SQLITE:
                    app.logger.error("Failed to connect to MySQL/TiDB: %s", safe_message)
                    raise RuntimeError(
                        "Gagal terhubung ke TiDB/MySQL. Periksa DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME, dan SSL."
                    ) from exc

                app.logger.warning(
                    "Remote MySQL/TiDB unavailable. Falling back to local SQLite database.db. Detail: %s",
                    safe_message,
                )

        sqlite_conn = sqlite3.connect(DATABASE)
        sqlite_conn.row_factory = sqlite3.Row
        g.db = DatabaseConnection(sqlite_conn, is_mysql=False)

    return g.db


def execute_commit(query, params=()):
    db = get_db()
    try:
        cursor = db.execute(query, params)
        db.commit()
        app.logger.debug("DB commit successful: %s", query)
        return cursor
    except Exception:
        db.rollback()
        app.logger.exception("DB write failed and rollback executed.")
        raise


def execute_script_commit(script):
    db = get_db()
    try:
        db.executescript(script)
        db.commit()
        app.logger.debug("DB script commit successful.")
    except Exception:
        db.rollback()
        app.logger.exception("DB schema change failed and rollback executed.")
        raise


def fetch_scalar(cursor):
    row = cursor.fetchone()
    if row is None:
        return None
    if isinstance(row, dict):
        return next(iter(row.values()))
    return row[0]


def fetch_all_dict(cursor):
    rows = cursor.fetchall()
    if not rows:
        return []
    if isinstance(rows[0], dict):
        return rows
    columns = [col[0] for col in cursor.description]
    return [dict(zip(columns, row)) for row in rows]


def row_to_dict(row):
    if row is None:
        return {}
    if isinstance(row, dict):
        return row
    return dict(row)


def normalize_role_value(role):
    role_key = str(role or "").strip().lower()
    if role_key in CASHIER_ROLE_ALIASES:
        return CASHIER_ROLE
    return role_key


def cashier_role_filter(column="role"):
    placeholders = ", ".join(["?"] * len(CASHIER_ROLE_ALIASES))
    return f"LOWER({column}) IN ({placeholders})"


def parse_db_datetime(value):
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())

    value = str(value or "").strip()
    if not value:
        return None

    for candidate in (value, value.replace("Z", "+00:00")):
        try:
            parsed = datetime.fromisoformat(candidate)
            return parsed.replace(tzinfo=None)
        except ValueError:
            continue

    for date_format in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value[:19], date_format)
        except ValueError:
            continue

    return None


def normalize_category_name(value):
    return " ".join(str(value or "").strip().split())


@app.template_global()
def category_key(category):
    return normalize_category_name(category).lower()


def get_table_columns(table_name):
    db = get_db()
    cursor = db.cursor()

    if db.is_mysql:
        cursor.execute(f"SHOW COLUMNS FROM {table_name}")
        fetched = cursor.fetchall()
        return {row["Field"] if isinstance(row, dict) else row[0] for row in fetched}

    cursor.execute(f"PRAGMA table_info({table_name})")
    return {row[1] for row in cursor.fetchall()}


def table_exists(table_name):
    db = get_db()
    if db.is_mysql:
        cursor = db.execute("SHOW TABLES LIKE ?", (table_name,))
    else:
        cursor = db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        )
    return cursor.fetchone() is not None


def role_display_name(role):
    role = normalize_role_value(role)
    if role == "owner":
        return "Owner"
    if role == CASHIER_ROLE:
        return "Kasir"
    return role.title() or "Pengguna"


def normalize_cashier_rows():
    execute_commit(
        f"""
        UPDATE users
        SET role = ?
        WHERE {cashier_role_filter("role")} AND LOWER(role) != ?
        """,
        (CASHIER_ROLE, *CASHIER_ROLE_ALIASES, CASHIER_ROLE),
    )
    execute_commit(
        f"""
        UPDATE users
        SET staff_position = ?
        WHERE {cashier_role_filter("role")}
          AND (staff_position IS NULL OR TRIM(staff_position) = '' OR LOWER(TRIM(staff_position)) IN (?, ?))
        """,
        ("Kasir", *CASHIER_ROLE_ALIASES, "staff", "cashier"),
    )
    execute_commit(
        f"""
        UPDATE users
        SET staff_status = ?
        WHERE {cashier_role_filter("role")}
          AND (staff_status IS NULL OR TRIM(staff_status) = '')
        """,
        ("Aktif", *CASHIER_ROLE_ALIASES),
    )
    execute_commit(
        f"""
        UPDATE users
        SET joined_date = DATE(created_at)
        WHERE {cashier_role_filter("role")}
          AND (joined_date IS NULL OR TRIM(joined_date) = '')
          AND created_at IS NOT NULL
        """,
        CASHIER_ROLE_ALIASES,
    )


def attach_legacy_cashiers_to_owner():
    db = get_db()
    normalize_cashier_rows()

    owner_rows = fetch_all_dict(
        db.execute(
            "SELECT id, created_at FROM users WHERE LOWER(role) = ? ORDER BY created_at ASC, id ASC",
            ("owner",),
        )
    )
    if not owner_rows:
        return

    if len(owner_rows) != 1:
        orphan_cashiers = fetch_all_dict(
            db.execute(
                f"""
                SELECT id, full_name, email, created_at
                FROM users
                WHERE {cashier_role_filter("role")} AND owner_id IS NULL
                ORDER BY created_at ASC, id ASC
                """,
                CASHIER_ROLE_ALIASES,
            )
        )
        parsed_owners = [
            {**owner, "created_dt": parse_db_datetime(owner.get("created_at"))}
            for owner in owner_rows
        ]

        for cashier in orphan_cashiers:
            cashier_created = parse_db_datetime(cashier.get("created_at"))
            if not cashier_created:
                continue

            candidates = [
                owner
                for owner in parsed_owners
                if owner.get("created_dt") and owner["created_dt"] <= cashier_created
            ]
            if not candidates:
                continue

            owner = max(candidates, key=lambda item: item["created_dt"])
            delta = cashier_created - owner["created_dt"]
            if delta <= timedelta(minutes=LEGACY_CASHIER_OWNER_WINDOW_MINUTES):
                execute_commit(
                    f"""
                    UPDATE users
                    SET owner_id = ?
                    WHERE id = ? AND {cashier_role_filter("role")} AND owner_id IS NULL
                    """,
                    (owner["id"], cashier["id"], *CASHIER_ROLE_ALIASES),
                )
                app.logger.info(
                    "Linked legacy cashier %s (%s) to owner_id %s from creation-time proximity.",
                    cashier.get("full_name"),
                    cashier.get("email"),
                    owner["id"],
                )
        return

    execute_commit(
        f"""
        UPDATE users
        SET owner_id = ?
        WHERE {cashier_role_filter("role")} AND owner_id IS NULL
        """,
        (owner_rows[0]["id"], *CASHIER_ROLE_ALIASES),
    )


def get_default_owner_id_for_cashier():
    owner_rows = fetch_all_dict(
        get_db().execute(
            "SELECT id FROM users WHERE LOWER(role) = ? ORDER BY id ASC",
            ("owner",),
        )
    )
    if len(owner_rows) == 1:
        return owner_rows[0].get("id")
    return None


def ensure_category_columns():
    db = get_db()
    columns = get_table_columns("categories")

    if "name_key" not in columns:
        execute_commit(
            "ALTER TABLE categories ADD COLUMN name_key VARCHAR(255)"
            if db.is_mysql
            else "ALTER TABLE categories ADD COLUMN name_key TEXT"
        )
    if "description" not in columns:
        execute_commit("ALTER TABLE categories ADD COLUMN description TEXT")
    if "created_at" not in columns:
        execute_commit(
            "ALTER TABLE categories ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
            if db.is_mysql
            else "ALTER TABLE categories ADD COLUMN created_at TEXT"
        )
    if "updated_at" not in columns:
        execute_commit(
            "ALTER TABLE categories ADD COLUMN updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
            if db.is_mysql
            else "ALTER TABLE categories ADD COLUMN updated_at TEXT"
        )

    rows = fetch_all_dict(db.execute("SELECT id, name, name_key FROM categories"))
    for row in rows:
        expected_key = category_key(row.get("name"))
        if expected_key and row.get("name_key") != expected_key:
            execute_commit(
                "UPDATE categories SET name_key = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (expected_key, row["id"]),
            )

    if db.is_mysql:
        index_exists = db.execute(
            "SHOW INDEX FROM categories WHERE Key_name = ?",
            ("idx_categories_name_key",),
        ).fetchone()
        if not index_exists:
            execute_commit("CREATE UNIQUE INDEX idx_categories_name_key ON categories (name_key)")
    else:
        execute_commit("CREATE UNIQUE INDEX IF NOT EXISTS idx_categories_name_key ON categories (name_key)")


def init_category_table():
    db = get_db()

    if db.is_mysql:
        execute_script_commit(
            """
            CREATE TABLE IF NOT EXISTS categories (
                id BIGINT PRIMARY KEY AUTO_INCREMENT,
                name VARCHAR(100) NOT NULL,
                name_key VARCHAR(255) NOT NULL UNIQUE,
                description TEXT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    else:
        execute_script_commit(
            """
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                name_key TEXT NOT NULL UNIQUE,
                description TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

    ensure_category_columns()


def get_category_by_id(category_id):
    try:
        category_id = int(category_id)
    except (TypeError, ValueError):
        return None
    if category_id < 1:
        return None
    return get_db().execute("SELECT * FROM categories WHERE id = ?", (category_id,)).fetchone()


def get_category_by_name(name):
    key = category_key(name)
    if not key:
        return None
    return get_db().execute("SELECT * FROM categories WHERE name_key = ?", (key,)).fetchone()


def get_or_create_category(name, description=None):
    clean_name = normalize_category_name(name)
    key = category_key(clean_name)
    if not clean_name:
        return None

    existing = get_category_by_name(clean_name)
    if existing:
        return existing

    try:
        cursor = execute_commit(
            """
            INSERT INTO categories (name, name_key, description)
            VALUES (?, ?, ?)
            """,
            (clean_name, key, description or None),
        )
        category_id = cursor.lastrowid
    except Exception:
        return get_category_by_name(clean_name)

    return get_category_by_id(category_id)


def migrate_menu_categories():
    if not table_exists("menus"):
        return

    columns = get_table_columns("menus")
    if "category" not in columns or "category_id" not in columns:
        return

    db = get_db()
    menu_rows = fetch_all_dict(
        db.execute(
            """
            SELECT id, category, category_id
            FROM menus
            WHERE category IS NOT NULL AND TRIM(category) <> ''
            """
        )
    )

    for menu in menu_rows:
        existing_category = get_category_by_id(menu.get("category_id"))
        if existing_category:
            category_name = normalize_category_name(existing_category["name"])
            if menu.get("category") != category_name:
                execute_commit(
                    "UPDATE menus SET category = ? WHERE id = ?",
                    (category_name, menu["id"]),
                )
            continue

        category = get_or_create_category(menu.get("category"))
        if category:
            execute_commit(
                "UPDATE menus SET category_id = ?, category = ? WHERE id = ?",
                (category["id"], category["name"], menu["id"]),
            )


def validate_category_payload(data, exclude_id=None):
    name = normalize_category_name(data.get("name", ""))
    description = str(data.get("description", "") or "").strip() or None
    errors = {}

    if not name:
        errors["name"] = "Nama kategori wajib diisi."
    elif len(name) > CATEGORY_NAME_MAX_LENGTH:
        errors["name"] = f"Nama kategori maksimal {CATEGORY_NAME_MAX_LENGTH} karakter."
    else:
        duplicate = get_category_by_name(name)
        if duplicate and int(duplicate["id"]) != int(exclude_id or 0):
            errors["name"] = "Nama kategori sudah digunakan."

    return {"name": name, "description": description}, errors


def get_category_options():
    return fetch_all_dict(
        get_db().execute(
            """
            SELECT id, name, description
            FROM categories
            ORDER BY LOWER(name) ASC, id ASC
            """
        )
    )


def get_menu_category_from_value(category_id=None, category_name=None):
    category = get_category_by_id(category_id)
    if not category and category_name:
        category = get_category_by_name(category_name)
    return category


def format_category_date(value):
    value = str(value or "").strip()
    if not value:
        return "-"

    for date_format in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(value[:19], date_format)
            return format_short_date(parsed.date())
        except ValueError:
            continue
    return value[:10]


def fetch_category_management_rows(search=""):
    db = get_db()
    where_clause = ""
    params = []

    if search:
        where_clause = "WHERE c.name LIKE ? OR COALESCE(c.description, '') LIKE ?"
        keyword = f"%{search}%"
        params = [keyword, keyword]

    rows = fetch_all_dict(
        db.execute(
            f"""
            SELECT
                c.id,
                c.name,
                c.description,
                c.created_at,
                c.updated_at,
                COUNT(m.id) AS menu_count
            FROM categories c
            LEFT JOIN menus m ON m.category_id = c.id
            {where_clause}
            GROUP BY c.id, c.name, c.description, c.created_at, c.updated_at
            ORDER BY LOWER(c.name) ASC, c.id ASC
            """,
            params,
        )
    )

    for row in rows:
        row["created_label"] = format_category_date(row.get("created_at"))
        row["description"] = row.get("description") or ""
        row["menu_count"] = int(row.get("menu_count") or 0)
    return rows


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def ensure_user_columns():
    db = get_db()
    cursor = db.cursor()

    if db.is_mysql:
        cursor.execute("SHOW COLUMNS FROM users")
        fetched = cursor.fetchall()
        columns = {row["Field"] if isinstance(row, dict) else row[0] for row in fetched}
    else:
        cursor.execute("PRAGMA table_info(users)")
        columns = {row[1] for row in cursor.fetchall()}

    if "staff_phone" not in columns:
        execute_commit("ALTER TABLE users ADD COLUMN staff_phone VARCHAR(40)" if db.is_mysql else "ALTER TABLE users ADD COLUMN staff_phone TEXT")
    if "staff_position" not in columns:
        execute_commit("ALTER TABLE users ADD COLUMN staff_position VARCHAR(100) DEFAULT 'Kasir'" if db.is_mysql else "ALTER TABLE users ADD COLUMN staff_position TEXT DEFAULT 'Kasir'")
    if "joined_date" not in columns:
        execute_commit("ALTER TABLE users ADD COLUMN joined_date DATE" if db.is_mysql else "ALTER TABLE users ADD COLUMN joined_date TEXT")
    if "staff_status" not in columns:
        execute_commit("ALTER TABLE users ADD COLUMN staff_status VARCHAR(40) DEFAULT 'Aktif'" if db.is_mysql else "ALTER TABLE users ADD COLUMN staff_status TEXT DEFAULT 'Aktif'")
    if "is_active" not in columns:
        execute_commit("ALTER TABLE users ADD COLUMN is_active TINYINT NOT NULL DEFAULT 1" if db.is_mysql else "ALTER TABLE users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
    if "owner_id" not in columns:
        execute_commit("ALTER TABLE users ADD COLUMN owner_id BIGINT NULL" if db.is_mysql else "ALTER TABLE users ADD COLUMN owner_id INTEGER")

    if db.is_mysql:
        owner_index = db.execute(
            "SHOW INDEX FROM users WHERE Key_name = ?",
            ("idx_users_owner_id",),
        ).fetchone()
        if not owner_index:
            execute_commit("CREATE INDEX idx_users_owner_id ON users (owner_id)")
    else:
        execute_commit("CREATE INDEX IF NOT EXISTS idx_users_owner_id ON users (owner_id)")


def ensure_cashier_invitation_table():
    db = get_db()
    if db.is_mysql:
        execute_script_commit(
            """
            CREATE TABLE IF NOT EXISTS cashier_invitations (
                id BIGINT PRIMARY KEY AUTO_INCREMENT,
                owner_id BIGINT NOT NULL,
                invite_code VARCHAR(64) NOT NULL UNIQUE,
                status VARCHAR(40) NOT NULL DEFAULT 'Aktif',
                expires_at DATETIME,
                used_at DATETIME,
                used_by_cashier_id BIGINT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        if not db.execute(
            "SHOW INDEX FROM cashier_invitations WHERE Key_name = ?",
            ("idx_cashier_invitations_owner_id",),
        ).fetchone():
            execute_commit("CREATE INDEX idx_cashier_invitations_owner_id ON cashier_invitations (owner_id)")
    else:
        execute_script_commit(
            """
            CREATE TABLE IF NOT EXISTS cashier_invitations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER NOT NULL,
                invite_code TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'Aktif',
                expires_at TEXT,
                used_at TEXT,
                used_by_cashier_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_cashier_invitations_owner_id ON cashier_invitations (owner_id);
            """
        )


def parse_iso_timestamp(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        try:
            return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None


def normalize_invite_code(value):
    value = str(value or "").strip().upper()
    allowed = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-"
    cleaned = "".join(char for char in value if char in allowed)
    return cleaned[:64]


def attach_legacy_transaction_owner_ids():
    db = get_db()
    try:
        execute_commit(
            """
            UPDATE pos_transactions
            SET owner_id = (
                SELECT owner_id FROM users WHERE users.id = pos_transactions.staff_id
            )
            WHERE owner_id IS NULL
              AND staff_id IS NOT NULL
            """
        )
    except Exception:
        db.rollback()


def create_cashier_invitation(owner_id, expires_days=7):
    db = get_db()
    invitation = None
    for _ in range(5):
        invite_code = f"KASIR-{uuid.uuid4().hex[:10].upper()}"
        expires_at = (datetime.now() + timedelta(days=expires_days)).isoformat(timespec="seconds")
        try:
            cursor = db.execute(
                """
                INSERT INTO cashier_invitations (
                    owner_id, invite_code, status, expires_at
                ) VALUES (?, ?, ?, ?)
                """,
                (owner_id, invite_code, "Aktif", expires_at),
            )
            db.commit()
            invitation = {
                "id": cursor.lastrowid,
                "owner_id": owner_id,
                "invite_code": invite_code,
                "status": "Aktif",
                "expires_at": expires_at,
                "used_at": None,
                "used_by_cashier_id": None,
                "created_at": datetime.now().isoformat(timespec="seconds"),
            }
            break
        except Exception:
            db.rollback()
            continue
    if invitation is None:
        raise RuntimeError("Gagal membuat kode undangan kasir. Silakan coba lagi.")
    return invitation


def get_cashier_invitation(invite_code):
    invite_code = normalize_invite_code(invite_code)
    if not invite_code:
        return None
    row = get_db().execute(
        "SELECT * FROM cashier_invitations WHERE invite_code = ?",
        (invite_code,),
    ).fetchone()
    if row is None:
        return None
    invitation = dict(row)
    if invitation.get("status") == "Aktif" and invitation.get("expires_at"):
        expires_at = parse_iso_timestamp(invitation["expires_at"])
        if expires_at and expires_at < datetime.now():
            invitation["status"] = "Kedaluwarsa"
            try:
                execute_commit(
                    "UPDATE cashier_invitations SET status = ? WHERE id = ?",
                    ("Kedaluwarsa", invitation["id"]),
                )
            except Exception:
                pass
    return invitation


def get_owner_latest_invitation(owner_id):
    row = get_db().execute(
        "SELECT * FROM cashier_invitations WHERE owner_id = ? ORDER BY created_at DESC LIMIT 1",
        (owner_id,),
    ).fetchone()
    return format_cashier_invitation(dict(row)) if row else None


def get_owner_invitations(owner_id, limit=5):
    rows = fetch_all_dict(
        get_db().execute(
            "SELECT * FROM cashier_invitations WHERE owner_id = ? ORDER BY created_at DESC LIMIT ?",
            (owner_id, limit),
        )
    )
    return [format_cashier_invitation(row) for row in rows]


def format_cashier_invitation(invitation):
    if not invitation:
        return None
    expires_at = parse_iso_timestamp(invitation.get("expires_at"))
    used_at = parse_iso_timestamp(invitation.get("used_at"))
    if invitation.get("status") == "Aktif" and expires_at and expires_at < datetime.now():
        invitation["status"] = "Kedaluwarsa"
    invitation["expires_at_display"] = expires_at.strftime("%Y-%m-%d %H:%M") if expires_at else "-"
    invitation["used_at_display"] = used_at.strftime("%Y-%m-%d %H:%M") if used_at else "-"
    invitation["is_active"] = invitation.get("status") == "Aktif"
    return invitation


def ensure_pos_transactions_columns():
    db = get_db()
    if db.is_mysql:
        cursor = db.execute("SHOW COLUMNS FROM pos_transactions")
        columns = {row[0] for row in cursor.fetchall()}
        if "owner_id" not in columns:
            execute_commit("ALTER TABLE pos_transactions ADD COLUMN owner_id BIGINT NULL")
    else:
        cursor = db.execute("PRAGMA table_info(pos_transactions)")
        columns = {row[1] for row in cursor.fetchall()}
        if "owner_id" not in columns:
            execute_commit("ALTER TABLE pos_transactions ADD COLUMN owner_id INTEGER")


def init_db():
    db = get_db()

    if db.is_mysql:
        execute_script_commit(
            """
            CREATE TABLE IF NOT EXISTS users (
                id BIGINT PRIMARY KEY AUTO_INCREMENT,
                full_name VARCHAR(255) NOT NULL,
                email VARCHAR(255) UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role VARCHAR(50) NOT NULL,
                owner_id BIGINT NULL,
                staff_phone VARCHAR(40),
                staff_position VARCHAR(100) DEFAULT 'Kasir',
                joined_date DATE,
                staff_status VARCHAR(40) DEFAULT 'Aktif',
                is_active TINYINT NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        execute_script_commit(
            """
            CREATE TABLE IF NOT EXISTS app_settings (
                `key` VARCHAR(255) PRIMARY KEY,
                `value` TEXT NOT NULL
            )
            """
        )
    else:
        execute_script_commit(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                full_name TEXT NOT NULL,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL,
                owner_id INTEGER,
                staff_phone TEXT,
                staff_position TEXT DEFAULT 'Kasir',
                joined_date TEXT,
                staff_status TEXT DEFAULT 'Aktif',
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )

    ensure_user_columns()
    attach_legacy_cashiers_to_owner()
    ensure_cashier_invitation_table()


def ensure_menu_columns():
    db = get_db()
    cursor = db.cursor()

    if db.is_mysql:
        cursor.execute("SHOW COLUMNS FROM menus")
        fetched = cursor.fetchall()
        columns = {row["Field"] if isinstance(row, dict) else row[0] for row in fetched}
    else:
        cursor.execute("PRAGMA table_info(menus)")
        columns = {row[1] for row in cursor.fetchall()}

    if "stock" not in columns:
        execute_commit("ALTER TABLE menus ADD COLUMN stock INTEGER NOT NULL DEFAULT 0")
    if "description" not in columns:
        # MySQL/TiDB may reject DEFAULT on TEXT, so no default is used here.
        if db.is_mysql:
            execute_commit("ALTER TABLE menus ADD COLUMN description TEXT")
        else:
            execute_commit("ALTER TABLE menus ADD COLUMN description TEXT")
    if "image" not in columns:
        execute_commit("ALTER TABLE menus ADD COLUMN image TEXT")
    if "is_active" not in columns:
        execute_commit("ALTER TABLE menus ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
    if "category_id" not in columns:
        execute_commit(
            "ALTER TABLE menus ADD COLUMN category_id BIGINT NULL"
            if db.is_mysql
            else "ALTER TABLE menus ADD COLUMN category_id INTEGER"
        )

    if db.is_mysql:
        index_exists = db.execute(
            "SHOW INDEX FROM menus WHERE Key_name = ?",
            ("idx_menus_category_id",),
        ).fetchone()
        if not index_exists:
            execute_commit("CREATE INDEX idx_menus_category_id ON menus (category_id)")
    else:
        execute_commit("CREATE INDEX IF NOT EXISTS idx_menus_category_id ON menus (category_id)")


def init_menu_table():
    db = get_db()
    init_category_table()

    if db.is_mysql:
        execute_script_commit(
            """
            CREATE TABLE IF NOT EXISTS menus (
                id BIGINT PRIMARY KEY AUTO_INCREMENT,
                name VARCHAR(255) NOT NULL,
                category VARCHAR(100) NOT NULL,
                category_id BIGINT NULL,
                code VARCHAR(100) NOT NULL UNIQUE,
                price BIGINT NOT NULL,
                stock INT NOT NULL DEFAULT 0,
                description TEXT,
                image TEXT,
                is_active TINYINT NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        execute_script_commit(
            """
            CREATE TABLE IF NOT EXISTS app_settings (
                `key` VARCHAR(255) PRIMARY KEY,
                `value` TEXT NOT NULL
            )
            """
        )
    else:
        execute_script_commit(
            """
            CREATE TABLE IF NOT EXISTS menus (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                category_id INTEGER,
                code TEXT NOT NULL UNIQUE,
                price INTEGER NOT NULL,
                stock INTEGER NOT NULL DEFAULT 0,
                description TEXT,
                image TEXT,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )

    ensure_menu_columns()

    cursor = db.execute(
        "SELECT value FROM app_settings WHERE key = ?",
        ("menus_seed_migration_done",),
    )
    migration_done = cursor.fetchone()

    if migration_done is None:
        # Remove old hard-coded/default menu seed once, without touching users.
        execute_commit("DELETE FROM menus")
        if not db.is_mysql:
            try:
                execute_commit("DELETE FROM sqlite_sequence WHERE name = 'menus'")
            except Exception:
                pass
        execute_commit(
            "INSERT INTO app_settings (key, value) VALUES (?, ?)",
            ("menus_seed_migration_done", "1"),
        )

    migrate_menu_categories()


def init_pos_tables():
    db = get_db()

    if db.is_mysql:
        execute_script_commit(
            """
            CREATE TABLE IF NOT EXISTS pos_transactions (
                id BIGINT PRIMARY KEY AUTO_INCREMENT,
                order_code VARCHAR(60) NOT NULL UNIQUE,
                transaction_date DATE NOT NULL,
                transaction_time TIME NOT NULL,
                customer_name VARCHAR(255) DEFAULT 'Walk-in Customer',
                payment_method VARCHAR(80) DEFAULT 'Tunai',
                subtotal_amount BIGINT NOT NULL DEFAULT 0,
                discount_amount BIGINT NOT NULL DEFAULT 0,
                tax_amount BIGINT NOT NULL DEFAULT 0,
                operational_cost BIGINT NOT NULL DEFAULT 0,
                total_amount BIGINT NOT NULL DEFAULT 0,
                item_count INT NOT NULL DEFAULT 0,
                status VARCHAR(40) NOT NULL DEFAULT 'Selesai',
                owner_id BIGINT NULL,
                staff_id BIGINT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        ensure_pos_transactions_columns()
        attach_legacy_transaction_owner_ids()
        execute_script_commit(
            """
            CREATE TABLE IF NOT EXISTS pos_transaction_items (
                id BIGINT PRIMARY KEY AUTO_INCREMENT,
                transaction_id BIGINT NOT NULL,
                menu_id BIGINT NULL,
                menu_name VARCHAR(255) NOT NULL,
                quantity INT NOT NULL DEFAULT 1,
                unit_price BIGINT NOT NULL DEFAULT 0,
                subtotal BIGINT NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    else:
        execute_script_commit(
            """
            CREATE TABLE IF NOT EXISTS pos_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_code TEXT NOT NULL UNIQUE,
                transaction_date TEXT NOT NULL,
                transaction_time TEXT NOT NULL,
                customer_name TEXT DEFAULT 'Walk-in Customer',
                payment_method TEXT DEFAULT 'Tunai',
                subtotal_amount INTEGER NOT NULL DEFAULT 0,
                discount_amount INTEGER NOT NULL DEFAULT 0,
                tax_amount INTEGER NOT NULL DEFAULT 0,
                operational_cost INTEGER NOT NULL DEFAULT 0,
                total_amount INTEGER NOT NULL DEFAULT 0,
                item_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'Selesai',
                owner_id INTEGER,
                staff_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS pos_transaction_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                transaction_id INTEGER NOT NULL,
                menu_id INTEGER,
                menu_name TEXT NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 1,
                unit_price INTEGER NOT NULL DEFAULT 0,
                subtotal INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        ensure_pos_transactions_columns()
    ensure_pos_transactions_columns()


def get_owner_name():
    return (
        session.get("full_name")
        or session.get("name")
        or session.get("username")
        or "Owner"
    )


def login_required(view_func):
    @wraps(view_func)
    def wrapped_view(**kwargs):
        if not session.get("user_id"):
            flash("Silakan login terlebih dahulu.", "error")
            return redirect(url_for("login"))
        return view_func(**kwargs)

    return wrapped_view


def redirect_for_role():
    role = normalize_role_value(session.get("role"))
    if role == "owner":
        return redirect(url_for("owner_menu"))
    if role == CASHIER_ROLE:
        return redirect(url_for("pos"))
    session.clear()
    return redirect(url_for("login"))


def staff_required(view_func):
    @wraps(view_func)
    def wrapped_view(**kwargs):
        if not session.get("user_id"):
            flash("Silakan login terlebih dahulu.", "error")
            return redirect(url_for("login"))
        if normalize_role_value(session.get("role")) != CASHIER_ROLE:
            return redirect_for_role()

        user = get_db().execute(
            "SELECT role, is_active, owner_id FROM users WHERE id = ?",
            (session.get("user_id"),),
        ).fetchone()
        user_data = row_to_dict(user)
        if not user_data or normalize_role_value(user_data.get("role")) != CASHIER_ROLE:
            session.clear()
            flash("Sesi tidak valid. Silakan login ulang.", "error")
            return redirect(url_for("login"))
        if int(user_data.get("is_active", 1) or 0) != 1:
            session.clear()
            flash("Akun kasir ini sedang nonaktif. Silakan hubungi Owner.", "error")
            return redirect(url_for("login"))
        if not user_data.get("owner_id"):
            session.clear()
            flash("Akun kasir belum terhubung dengan Owner. Silakan hubungi Owner untuk dibuatkan ulang.", "error")
            return redirect(url_for("login"))

        return view_func(**kwargs)

    return wrapped_view


def owner_required(view_func):
    @wraps(view_func)
    def wrapped_view(**kwargs):
        if not session.get("user_id"):
            flash("Silakan login terlebih dahulu.", "error")
            return redirect(url_for("login"))
        if normalize_role_value(session.get("role")) != "owner":
            return redirect_for_role()

        user = get_db().execute(
            "SELECT role FROM users WHERE id = ?",
            (session.get("user_id"),),
        ).fetchone()
        user_data = row_to_dict(user)
        if not user_data or normalize_role_value(user_data.get("role")) != "owner":
            session.clear()
            flash("Sesi tidak valid. Silakan login ulang.", "error")
            return redirect(url_for("login"))
        return view_func(**kwargs)

    return wrapped_view


def get_user_by_email(email):
    return get_db().execute(
        "SELECT * FROM users WHERE email = ?",
        (email.strip().lower(),),
    ).fetchone()


def validate_auth_fields(full_name=None, email=None, password=None):
    errors = []
    if full_name is not None and not full_name.strip():
        errors.append("Nama lengkap wajib diisi.")
    if not email or not email.strip():
        errors.append("Email wajib diisi.")
    elif "@" not in email or "." not in email.split("@")[-1]:
        errors.append("Format email tidak valid.")
    if not password:
        errors.append("Password wajib diisi.")
    elif len(password) < 6:
        errors.append("Password minimal 6 karakter.")
    return errors


def get_pos_category_filters(categories):
    category_lookup = {}
    for category in categories:
        category_name = normalize_category_name(category)
        if category_name:
            category_lookup[category_key(category_name)] = category_name
    return [category for _, category in sorted(category_lookup.items(), key=lambda item: item[1].casefold())]


def format_report_datetime(value):
    return f"{format_short_date(value.date())} {value:%H:%M} WIB"


def format_currency(amount):
    return f"Rp{int(amount or 0):,}".replace(",", ".")


def format_short_date(value):
    month_names = ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"]
    return f"{value.day} {month_names[value.month - 1]} {value.year}"


def format_month_name(value):
    month_names = [
        "Januari",
        "Februari",
        "Maret",
        "April",
        "Mei",
        "Juni",
        "Juli",
        "Agustus",
        "September",
        "Oktober",
        "November",
        "Desember",
    ]
    return f"{month_names[value.month - 1]} {value.year}"


def format_report_period(start_date, end_date):
    if start_date == end_date:
        return format_short_date(start_date)
    if start_date.month == end_date.month and start_date.year == end_date.year:
        month_names = ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"]
        return f"{start_date.day} - {end_date.day} {month_names[start_date.month - 1]} {start_date.year}"
    return f"{format_short_date(start_date)} - {format_short_date(end_date)}"


def parse_report_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def resolve_report_period(args):
    today = datetime.now().date()
    default_start = today.replace(day=1)
    start_date = parse_report_date(args.get("date_from")) or default_start
    end_date = parse_report_date(args.get("date_to")) or today
    if start_date > end_date:
        start_date, end_date = end_date, start_date
    return start_date, end_date


def shift_month(source_date, month_delta):
    month_index = source_date.month - 1 + month_delta
    year = source_date.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, 1)


def get_period_totals(start_date, end_date, owner_id=None):
    db = get_db()
    owner_join = ""
    owner_filter = ""
    params = [start_date.isoformat(), end_date.isoformat()]
    if owner_id:
        owner_join = "LEFT JOIN users u ON u.id = t.staff_id"
        owner_filter = "AND (t.owner_id = ? OR (t.owner_id IS NULL AND u.owner_id = ?))"
        params.extend([owner_id, owner_id])

    row = row_to_dict(
        db.execute(
            f"""
            SELECT
                COALESCE(SUM(total_amount), 0) AS revenue,
                COUNT(*) AS transactions
            FROM pos_transactions t
            {owner_join}
            WHERE t.transaction_date BETWEEN ? AND ?
              AND LOWER(t.status) IN ('selesai', 'paid', 'completed', 'complete')
              {owner_filter}
            """,
            params,
        ).fetchone()
    )
    revenue = int(row.get("revenue") or 0)
    transactions = int(row.get("transactions") or 0)
    return {
        "revenue": revenue,
        "profit": revenue,
        "transactions": transactions,
    }


def build_trend_text(current_value, previous_value, empty_text="Belum ada transaksi"):
    current_value = int(current_value or 0)
    previous_value = int(previous_value or 0)
    if previous_value == 0:
        return empty_text if current_value == 0 else "Baru ada transaksi"
    percentage = ((current_value - previous_value) / previous_value) * 100
    sign = "+" if percentage >= 0 else "-"
    return f"{sign}{abs(percentage):.1f}% dari periode lalu"


def trend_tone(current_value, previous_value):
    current_value = int(current_value or 0)
    previous_value = int(previous_value or 0)
    if previous_value == 0 or current_value == previous_value:
        return "neutral"
    return "positive" if current_value > previous_value else "negative"


def fetch_daily_details(start_date, end_date, owner_id=None):
    db = get_db()
    owner_join = ""
    owner_filter = ""
    params = [start_date.isoformat(), end_date.isoformat()]
    if owner_id:
        owner_join = "LEFT JOIN users u ON u.id = t.staff_id"
        owner_filter = "AND (t.owner_id = ? OR (t.owner_id IS NULL AND u.owner_id = ?))"
        params.extend([owner_id, owner_id])

    rows = fetch_all_dict(
        db.execute(
            f"""
            SELECT
                t.transaction_date,
                COUNT(*) AS transactions,
                COALESCE(SUM(t.total_amount), 0) AS income
            FROM pos_transactions t
            {owner_join}
            WHERE t.transaction_date BETWEEN ? AND ?
              AND LOWER(t.status) IN ('selesai', 'paid', 'completed', 'complete')
              {owner_filter}
            GROUP BY t.transaction_date
            ORDER BY t.transaction_date DESC
            """,
            params,
        )
    )

    details = []
    for row in rows:
        income = int(row.get("income") or 0)
        detail_date = parse_report_date(str(row.get("transaction_date")))
        details.append(
            {
                "date": format_short_date(detail_date) if detail_date else row.get("transaction_date"),
                "transactions": int(row.get("transactions") or 0),
                "income": format_currency(income),
                "profit": format_currency(income),
            }
        )
    return details


def fetch_recent_transactions(start_date, end_date, limit=5, owner_id=None):
    db = get_db()
    owner_filter = ""
    params = [start_date.isoformat(), end_date.isoformat()]
    if owner_id:
        owner_filter = "AND (t.owner_id = ? OR (t.owner_id IS NULL AND u.owner_id = ?))"
        params.extend([owner_id, owner_id])
    params.append(limit)

    rows = fetch_all_dict(
        db.execute(
            f"""
            SELECT
                t.order_code,
                t.transaction_date,
                t.transaction_time,
                t.customer_name,
                t.payment_method,
                t.total_amount,
                t.item_count,
                t.status,
                u.full_name AS staff_name
            FROM pos_transactions t
            LEFT JOIN users u ON u.id = t.staff_id
            WHERE t.transaction_date BETWEEN ? AND ?
              AND LOWER(t.status) IN ('selesai', 'paid', 'completed', 'complete')
              {owner_filter}
            ORDER BY t.transaction_date DESC, t.transaction_time DESC, t.id DESC
            LIMIT ?
            """,
            params,
        )
    )

    transactions = []
    for row in rows:
        transaction_date = parse_report_date(str(row.get("transaction_date")))
        transaction_time = str(row.get("transaction_time") or "")[:5]
        transactions.append(
            {
                "id": row.get("order_code") or "-",
                "date": format_short_date(transaction_date) if transaction_date else row.get("transaction_date"),
                "time": transaction_time or "-",
                "customer": row.get("customer_name") or "Walk-in Customer",
                "method": row.get("payment_method") or "Tunai",
                "staff": row.get("staff_name") or "-",
                "total": format_currency(row.get("total_amount") or 0),
                "items": int(row.get("item_count") or 0),
                "status": str(row.get("status") or "Selesai").title(),
            }
        )
    return transactions


def fetch_hourly_sales(start_date, end_date, owner_id=None):
    db = get_db()
    owner_join = ""
    owner_filter = ""
    params = [start_date.isoformat(), end_date.isoformat()]
    if owner_id:
        owner_join = "LEFT JOIN users u ON u.id = t.staff_id"
        owner_filter = "AND (t.owner_id = ? OR (t.owner_id IS NULL AND u.owner_id = ?))"
        params.extend([owner_id, owner_id])

    rows = fetch_all_dict(
        db.execute(
            f"""
            SELECT t.transaction_time, t.total_amount
            FROM pos_transactions t
            {owner_join}
            WHERE t.transaction_date BETWEEN ? AND ?
              AND LOWER(t.status) IN ('selesai', 'paid', 'completed', 'complete')
              {owner_filter}
            """,
            params,
        )
    )

    hourly_values = {hour: 0 for hour in range(8, 23)}
    for row in rows:
        try:
            hour = int(str(row.get("transaction_time") or "")[:2])
        except ValueError:
            continue
        if hour in hourly_values:
            hourly_values[hour] += int(row.get("total_amount") or 0)

    max_value = max(hourly_values.values()) if hourly_values else 0
    chart = []
    for hour, amount in hourly_values.items():
        height = int((amount / max_value) * 100) if max_value else 0
        chart.append(
            {
                "hour": f"{hour:02d}:00",
                "amount": format_currency(amount),
                "height": height,
                "has_value": amount > 0,
                "is_peak": max_value > 0 and amount == max_value,
                "label_visible": hour % 2 == 0,
            }
        )
    return chart


def build_monthly_summary(end_date, owner_id=None):
    current_month = end_date.replace(day=1)
    rows = []
    for month_delta in (-2, -1, 0):
        month_start = shift_month(current_month, month_delta)
        month_end = shift_month(month_start, 1) - timedelta(days=1)
        totals = get_period_totals(month_start, month_end, owner_id)
        rows.append(
            {
                "month": format_month_name(month_start),
                "income": format_currency(totals["revenue"]),
                "profit": format_currency(totals["profit"]),
                "is_current": month_delta == 0,
            }
        )
    return rows


def build_financial_report(args=None):
    init_pos_tables()
    args = args or request.args
    start_date, end_date = resolve_report_period(args)
    now = datetime.now()
    owner_id = session.get("user_id") if session.get("role") == "owner" else None
    totals = get_period_totals(start_date, end_date, owner_id)
    day_count = max((end_date - start_date).days + 1, 1)
    previous_end = start_date - timedelta(days=1)
    previous_start = previous_end - timedelta(days=day_count - 1)
    previous_totals = get_period_totals(previous_start, previous_end, owner_id)

    today = now.date()
    today_totals = get_period_totals(today, today, owner_id)
    average_income = totals["revenue"] // day_count
    previous_average = previous_totals["revenue"] // day_count if day_count else 0
    period_label = format_report_period(start_date, end_date)
    daily_details = fetch_daily_details(start_date, end_date, owner_id)
    recent_transactions = fetch_recent_transactions(start_date, end_date, limit=5, owner_id=owner_id)

    return {
        "period": period_label,
        "calendar_label": period_label,
        "printed_at": format_report_datetime(now),
        "date_from": start_date.isoformat(),
        "date_to": end_date.isoformat(),
        "has_data": totals["transactions"] > 0,
        "dashboard_metrics": [
            {
                "label": "Total Pendapatan",
                "value": format_currency(totals["revenue"]),
                "trend": build_trend_text(totals["revenue"], previous_totals["revenue"]),
                "tone": trend_tone(totals["revenue"], previous_totals["revenue"]),
            },
            {
                "label": "Laba Bersih",
                "value": format_currency(totals["profit"]),
                "trend": build_trend_text(totals["profit"], previous_totals["profit"], "Belum ada transaksi"),
                "tone": trend_tone(totals["profit"], previous_totals["profit"]),
            },
            {
                "label": "Total Transaksi",
                "value": str(totals["transactions"]),
                "trend": build_trend_text(totals["transactions"], previous_totals["transactions"]),
                "tone": trend_tone(totals["transactions"], previous_totals["transactions"]),
            },
            {
                "label": "Pendapatan Hari Ini",
                "value": format_currency(today_totals["revenue"]),
                "trend": "Dari transaksi tanggal ini",
                "tone": "neutral",
            },
            {
                "label": "Rata-rata Pendapatan Harian",
                "value": format_currency(average_income),
                "trend": build_trend_text(average_income, previous_average),
                "tone": trend_tone(average_income, previous_average),
            },
        ],
        "print_summary": [
            {"label": "Total Pendapatan (Revenue)", "value": format_currency(totals["revenue"]), "tone": "normal"},
            {"label": "Laba Bersih", "value": format_currency(totals["profit"]), "tone": "success"},
            {"label": "Total Transaksi", "value": str(totals["transactions"]), "tone": "normal"},
            {"label": "Rata-rata Pendapatan Harian", "value": format_currency(average_income), "tone": "normal"},
        ],
        "net_profit": format_currency(totals["profit"]),
        "net_profit_trend": build_trend_text(totals["profit"], previous_totals["profit"], "Belum ada data periode lalu"),
        "hourly_sales": fetch_hourly_sales(start_date, end_date, owner_id),
        "daily_details": daily_details,
        "daily_totals": {
            "transactions": str(totals["transactions"]),
            "income": format_currency(totals["revenue"]),
            "profit": format_currency(totals["profit"]),
        },
        "recent_transactions": recent_transactions,
        "print_transactions": fetch_recent_transactions(start_date, end_date, limit=20, owner_id=owner_id),
        "monthly_summary": build_monthly_summary(end_date, owner_id),
        "daily_income_rows": daily_details[:6],
    }


def parse_menu_price(price_value):
    price = int(price_value)
    if price < MIN_MENU_PRICE or price % MIN_MENU_PRICE != 0:
        raise ValueError
    return price


def build_menu_code_prefix(category, name=""):
    source = str(category or name or "Menu").upper()
    cleaned = re.sub(r"[^A-Z0-9]+", " ", source).strip()
    first_word = cleaned.split()[0] if cleaned else "MENU"
    prefix = re.sub(r"[^A-Z0-9]", "", first_word)[:3]
    return (prefix or "MNU").ljust(3, "X")


def generate_menu_code(category, name=""):
    db = get_db()
    prefix = build_menu_code_prefix(category, name)
    rows = fetch_all_dict(
        db.execute(
            "SELECT code FROM menus WHERE code LIKE ?",
            (f"{prefix}-%",),
        )
    )
    highest_number = 0
    pattern = re.compile(rf"^{re.escape(prefix)}-(\d+)$")
    for row in rows:
        match = pattern.match(str(row.get("code") or "").upper())
        if match:
            highest_number = max(highest_number, int(match.group(1)))

    next_number = highest_number + 1
    while True:
        candidate = f"{prefix}-{next_number:03d}"
        exists = db.execute("SELECT id FROM menus WHERE code = ?", (candidate,)).fetchone()
        if not exists:
            return candidate
        next_number += 1


def parse_staff_date(value):
    value = str(value or "").strip()
    if not value:
        return ""
    for date_format in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(value, date_format).date().isoformat()
        except ValueError:
            continue
    return ""


def format_staff_date(value):
    value = str(value or "").strip()
    if not value:
        return "-"
    date_part = value[:10]
    parsed_date = parse_report_date(date_part)
    if parsed_date:
        return format_short_date(parsed_date)
    return value


def normalize_staff_status(status, is_active=True):
    status = str(status or "Aktif").strip().title()
    if status not in STAFF_STATUSES:
        status = "Aktif"
    if not is_active:
        return "Nonaktif"
    return status


def staff_status_tone(status):
    status_key = str(status or "").strip().lower()
    if status_key == "aktif":
        return "active"
    if status_key == "cuti":
        return "leave"
    return "inactive"


def staff_initial(name):
    name = str(name or "").strip()
    return name[:1].upper() if name else "K"


def format_staff_member(row):
    data = row_to_dict(row)
    is_active = int(data.get("is_active", 1) or 0) == 1
    status = normalize_staff_status(data.get("staff_status"), is_active)
    joined_date = data.get("joined_date") or data.get("created_at")
    return {
        "id": data.get("id"),
        "full_name": data.get("full_name") or "Kasir",
        "initial": staff_initial(data.get("full_name")),
        "email": data.get("email") or "-",
        "phone": data.get("staff_phone") or "-",
        "phone_value": data.get("staff_phone") or "",
        "position": "Kasir",
        "joined_date": format_staff_date(joined_date),
        "joined_date_value": parse_staff_date(joined_date) or datetime.now().date().isoformat(),
        "status": status,
        "status_tone": staff_status_tone(status),
        "is_active": is_active,
    }


def get_staff_form_data():
    is_active = request.form.get("is_active", "1") == "1"
    status = normalize_staff_status(request.form.get("staff_status", "Aktif"), is_active)
    if status == "Nonaktif":
        is_active = False
    return {
        "full_name": request.form.get("full_name", "").strip(),
        "email": request.form.get("email", "").strip().lower(),
        "staff_phone": request.form.get("staff_phone", "").strip(),
        "staff_position": request.form.get("staff_position", "Kasir").strip() or "Kasir",
        "joined_date": request.form.get("joined_date", "").strip(),
        "staff_status": status,
        "is_active": is_active,
    }


def validate_staff_form(form_data, require_email=True):
    errors = []
    if not form_data["full_name"]:
        errors.append("Nama lengkap wajib diisi.")
    if require_email:
        if not form_data["email"]:
            errors.append("Email wajib diisi.")
        elif "@" not in form_data["email"]:
            errors.append("Format email tidak valid.")
    if not form_data["staff_phone"]:
        errors.append("Nomor telepon wajib diisi.")
    if not form_data["staff_position"]:
        errors.append("Role kasir wajib diisi.")
    elif form_data["staff_position"] not in STAFF_POSITIONS:
        errors.append("Role kasir tidak valid.")
    joined_date = parse_staff_date(form_data["joined_date"])
    if not joined_date:
        errors.append("Tanggal bergabung wajib diisi dengan format tanggal yang valid.")
    if form_data["staff_status"] not in STAFF_STATUSES:
        errors.append("Status kasir tidak valid.")
    return errors, joined_date


def get_current_shift():
    current_hour = datetime.now().hour
    if 5 <= current_hour < 12:
        return "Pagi"
    if 12 <= current_hour < 18:
        return "Siang"
    return "Malam"


def parse_pos_amount(value, field_label):
    if value in (None, ""):
        return 0
    try:
        amount = int(str(value).strip().replace(".", "").replace(",", ""))
    except (TypeError, ValueError):
        raise ValueError(f"{field_label} harus berupa angka.")
    if amount < 0:
        raise ValueError(f"{field_label} tidak boleh negatif.")
    return amount


def normalize_pos_items(raw_items):
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("Keranjang masih kosong.")

    items = {}
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            raise ValueError("Data item POS tidak valid.")
        try:
            menu_id = int(raw_item.get("menu_id"))
            quantity = int(raw_item.get("quantity", 1))
        except (TypeError, ValueError):
            raise ValueError("Data item POS tidak valid.")
        if menu_id < 1 or quantity < 1:
            raise ValueError("Jumlah item POS tidak valid.")
        if quantity > 999:
            raise ValueError("Jumlah item terlalu besar.")
        items[menu_id] = items.get(menu_id, 0) + quantity

    return items


def generate_order_code(now=None):
    now = now or datetime.now()
    return f"POS-{now:%Y%m%d%H%M%S}-{uuid.uuid4().hex[:4].upper()}"


def generate_invoice_code(now=None):
    now = now or datetime.now()
    return f"INV{now:%Y%m%d%H%M%S}{uuid.uuid4().hex[:3].upper()}"


def normalize_order_code(value):
    value = str(value or "").strip().upper()
    allowed = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
    cleaned = "".join(char for char in value if char in allowed)
    return cleaned[:60]


def build_qris_payload(order_code, total_amount, timestamp):
    return f"ORDER={order_code}\nTOTAL={int(total_amount or 0)}\nTIME={timestamp}"


def remember_payment_details(order_code, payment_method, total_amount, received_amount=None, change_amount=None):
    received = int(received_amount if received_amount is not None else total_amount or 0)
    change = int(change_amount if change_amount is not None else max(received - int(total_amount or 0), 0))
    session["last_payment"] = {
        "order_code": order_code,
        "payment_method": payment_method,
        "total_amount": int(total_amount or 0),
        "received_amount": received,
        "change_amount": change,
    }
    session.modified = True


def get_payment_details(order_code, transaction):
    stored = session.get("last_payment") or {}
    if stored.get("order_code") == order_code:
        return {
            "method": stored.get("payment_method") or transaction.get("payment_method") or "-",
            "received_amount": int(stored.get("received_amount") or 0),
            "change_amount": int(stored.get("change_amount") or 0),
        }

    total_amount = int(transaction.get("total_amount") or 0)
    return {
        "method": transaction.get("payment_method") or "-",
        "received_amount": total_amount,
        "change_amount": 0,
    }


def fetch_transaction_detail(order_code):
    init_pos_tables()
    db = get_db()
    transaction = row_to_dict(
        db.execute(
            """
            SELECT
                t.id,
                t.order_code,
                t.transaction_date,
                t.transaction_time,
                t.customer_name,
                t.payment_method,
                t.subtotal_amount,
                t.total_amount,
                t.item_count,
                t.status,
                u.full_name AS staff_name
            FROM pos_transactions t
            LEFT JOIN users u ON u.id = t.staff_id
            WHERE t.order_code = ?
            """,
            (order_code,),
        ).fetchone()
    )
    if not transaction:
        return None

    items = fetch_all_dict(
        db.execute(
            """
            SELECT menu_name, quantity, unit_price, subtotal
            FROM pos_transaction_items
            WHERE transaction_id = ?
            ORDER BY id ASC
            """,
            (transaction["id"],),
        )
    )

    transaction_date = parse_report_date(str(transaction.get("transaction_date") or ""))
    transaction["date_display"] = format_short_date(transaction_date) if transaction_date else transaction.get("transaction_date")
    transaction["time_display"] = str(transaction.get("transaction_time") or "")[:5]
    transaction["total_display"] = format_currency(transaction.get("total_amount") or 0)
    transaction["subtotal_display"] = format_currency(transaction.get("subtotal_amount") or 0)
    transaction["items"] = [
        {
            **item,
            "unit_price_display": format_currency(item.get("unit_price") or 0),
            "subtotal_display": format_currency(item.get("subtotal") or 0),
        }
        for item in items
    ]
    return transaction


def create_pos_transaction(data):
    init_menu_table()
    init_pos_tables()

    items = normalize_pos_items(data.get("items"))
    customer_name = str(data.get("customer_name") or "").strip() or "Walk-in Customer"
    payment_method = str(data.get("payment_method") or "Tunai").strip() or "Tunai"
    if payment_method.lower() in {"tunai", "cash"}:
        payment_method = "Cash"
    elif payment_method.lower() == "qris":
        payment_method = "QRIS"
    else:
        raise ValueError("Metode pembayaran hanya boleh Cash atau QRIS.")
    discount_amount = parse_pos_amount(data.get("discount_amount"), "Diskon")
    tax_amount = 0
    operational_cost = 0

    db = get_db()
    placeholders = ", ".join(["?"] * len(items))
    menu_rows = fetch_all_dict(
        db.execute(
            f"""
            SELECT id, name, price, stock, is_active
            FROM menus
            WHERE id IN ({placeholders})
            """,
            tuple(items.keys()),
        )
    )
    menu_map = {int(row["id"]): row for row in menu_rows}

    prepared_items = []
    validation_errors = []
    subtotal_amount = 0
    item_count = 0

    for menu_id, quantity in items.items():
        menu = menu_map.get(menu_id)
        if menu is None:
            validation_errors.append(f"Menu ID {menu_id} tidak ditemukan.")
            continue

        menu_name = menu.get("name") or "Menu"
        is_active = int(menu.get("is_active", 0) or 0) == 1
        stock = int(menu.get("stock") or 0)
        unit_price = int(menu.get("price") or 0)

        if not is_active:
            validation_errors.append(f"{menu_name} sedang nonaktif.")
            continue
        if quantity > stock:
            validation_errors.append(f"Stok {menu_name} tidak cukup. Tersedia {stock}.")
            continue

        line_subtotal = unit_price * quantity
        subtotal_amount += line_subtotal
        item_count += quantity
        prepared_items.append(
            {
                "menu_id": menu_id,
                "menu_name": menu_name,
                "quantity": quantity,
                "unit_price": unit_price,
                "subtotal": line_subtotal,
                "stock": stock,
            }
        )

    if validation_errors:
        raise ValueError(" ".join(validation_errors))
    if discount_amount > subtotal_amount:
        raise ValueError("Diskon tidak boleh lebih besar dari subtotal.")

    total_amount = subtotal_amount - discount_amount
    if payment_method == "Cash":
        received_amount = parse_pos_amount(data.get("received_amount"), "Nominal diterima")
        if received_amount < total_amount:
            raise ValueError("Nominal diterima kurang dari total pembayaran.")

    now = datetime.now()
    order_code = normalize_order_code(data.get("order_code")) or generate_order_code(now)

    try:
        owner_id = session.get("owner_id")
        cursor = db.execute(
            """
            INSERT INTO pos_transactions (
                order_code, transaction_date, transaction_time, customer_name,
                payment_method, subtotal_amount, discount_amount, tax_amount,
                operational_cost, total_amount, item_count, status, owner_id, staff_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_code,
                now.date().isoformat(),
                now.strftime("%H:%M:%S"),
                customer_name,
                payment_method,
                subtotal_amount,
                discount_amount,
                tax_amount,
                operational_cost,
                total_amount,
                item_count,
                "Selesai",
                owner_id,
                session.get("user_id"),
            ),
        )
        transaction_id = cursor.lastrowid

        for item in prepared_items:
            stock_update = db.execute(
                """
                UPDATE menus
                SET stock = stock - ?
                WHERE id = ? AND stock >= ?
                """,
                (item["quantity"], item["menu_id"], item["quantity"]),
            )
            if getattr(stock_update, "rowcount", 0) != 1:
                raise ValueError(f"Stok {item['menu_name']} baru saja berubah. Silakan cek ulang keranjang.")

            db.execute(
                """
                INSERT INTO pos_transaction_items (
                    transaction_id, menu_id, menu_name, quantity, unit_price, subtotal
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    transaction_id,
                    item["menu_id"],
                    item["menu_name"],
                    item["quantity"],
                    item["unit_price"],
                    item["subtotal"],
                ),
            )
            item["stock_remaining"] = item["stock"] - item["quantity"]

        db.commit()
    except Exception:
        db.rollback()
        raise

    return {
        "order_code": order_code,
        "subtotal_amount": subtotal_amount,
        "discount_amount": discount_amount,
        "total_amount": total_amount,
        "item_count": item_count,
        "items": prepared_items,
    }


def save_menu_image(uploaded_file):
    if not uploaded_file or not uploaded_file.filename:
        return "", None

    extension = Path(uploaded_file.filename).suffix.lower()
    allowed_extensions = {".png", ".jpg", ".jpeg", ".webp"}
    if extension not in allowed_extensions:
        return "", "Format gambar tidak valid. Gunakan PNG, JPG, JPEG, atau WEBP."

    cloudinary_ready = CLOUDINARY_CLOUD_NAME and CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET
    if cloudinary_ready:
        if cloudinary is None:
            return "", "Paket Cloudinary belum terpasang. Jalankan pip install -r requirements.txt."

        filename = secure_filename(uploaded_file.filename)
        public_id = f"{Path(filename).stem}-{uuid.uuid4().hex[:12]}"
        upload_options = {
            "public_id": public_id,
            "resource_type": "image",
            "overwrite": False,
        }
        if CLOUDINARY_FOLDER:
            upload_options["folder"] = CLOUDINARY_FOLDER

        try:
            uploaded_file.stream.seek(0)
            result = cloudinary.uploader.upload(uploaded_file.stream, **upload_options)
        except Exception:
            return "", "Gagal mengunggah gambar ke Cloudinary. Periksa konfigurasi .env Anda."

        image_url = result.get("secure_url") or result.get("url")
        if not image_url:
            return "", "Cloudinary tidak mengembalikan URL gambar."
        return image_url, None

    app.config["UPLOAD_FOLDER"].mkdir(parents=True, exist_ok=True)
    filename = secure_filename(uploaded_file.filename)
    unique_name = f"{uuid.uuid4().hex}_{filename}"
    target_path = app.config["UPLOAD_FOLDER"] / unique_name
    uploaded_file.stream.seek(0)
    uploaded_file.save(target_path)
    return f"uploads/menu/{unique_name}", None


@app.template_global()
def menu_image_url(image_path):
    if not image_path:
        return ""

    image_path = str(image_path).strip()
    if image_path.startswith(("http://", "https://", "//")):
        return image_path
    return url_for("static", filename=image_path.lstrip("/"))


@app.route("/")
def opening():
    if session.get("user_id"):
        return redirect_for_role()
    return render_template("opening.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    init_db()
    if session.get("user_id"):
        return redirect_for_role()

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        errors = validate_auth_fields(email=email, password=password)
        if errors:
            for error in errors:
                flash(error, "error")
            return render_template("login.html", email=email)

        user = get_user_by_email(email)
        if user is None or not check_password_hash(user["password_hash"], password):
            flash("Email atau password salah. Cek lagi email yang terdaftar dan password saat registrasi.", "error")
            return render_template("login.html", email=email)

        user_data = row_to_dict(user)
        user_role = normalize_role_value(user_data.get("role"))
        if user_role == CASHIER_ROLE and int(user_data.get("is_active", 1) or 0) != 1:
            flash("Akun kasir ini sedang nonaktif. Silakan hubungi Owner.", "error")
            return render_template("login.html", email=email)
        if user_role == CASHIER_ROLE and not user_data.get("owner_id"):
            flash("Akun kasir belum terhubung dengan Owner. Silakan hubungi Owner untuk dibuatkan ulang.", "error")
            return render_template("login.html", email=email)

        session.clear()
        session["user_id"] = user_data["id"]
        session["full_name"] = user_data["full_name"]
        session["name"] = user_data["full_name"]
        session["username"] = user_data["full_name"]
        session["role"] = user_role
        session["owner_id"] = user_data["id"] if user_role == "owner" else user_data.get("owner_id")
        flash(f"Login sebagai {role_display_name(user_role)} berhasil.", "success")
        return redirect_for_role()

    registered_email = session.pop("registered_email", "")
    query_email = request.args.get("email", "").strip().lower()
    return render_template("login.html", email=query_email or registered_email)


def register_user(role):
    init_db()
    role = normalize_role_value(role)
    full_name = request.form.get("full_name", "").strip()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    password_confirm = request.form.get("password_confirm", "")
    staff_phone = request.form.get("staff_phone", "").strip()
    invite_code = request.form.get("invite_code", "").strip()

    errors = validate_auth_fields(full_name=full_name, email=email, password=password)
    if password != password_confirm:
        errors.append("Password dan konfirmasi password harus sama.")

    if role == CASHIER_ROLE:
        invitation = None
        if not invite_code:
            errors.append("Kode undangan owner wajib diisi untuk pendaftaran kasir.")
            owner_id = None
        else:
            invitation = get_cashier_invitation(invite_code)
            if not invitation:
                errors.append("Kode undangan owner tidak valid.")
                owner_id = None
            elif invitation.get("status") != "Aktif":
                errors.append("Kode undangan owner sudah tidak aktif atau sudah digunakan.")
                owner_id = None
            else:
                expires_at = parse_iso_timestamp(invitation.get("expires_at"))
                if expires_at and expires_at < datetime.now():
                    errors.append("Kode undangan owner sudah kedaluwarsa.")
                    owner_id = None
                else:
                    owner_id = invitation.get("owner_id")
    else:
        owner_id = None

    if errors:
        for error in errors:
            flash(error, "error")
        template_name = "register_owner.html" if role == "owner" else "register_staff.html"
        return render_template(
            template_name,
            full_name=full_name,
            email=email,
            staff_phone=staff_phone,
            invite_code=invite_code,
        )

    password_hash = generate_password_hash(password)
    db = get_db()
    try:
        if role == CASHIER_ROLE:
            cursor = db.execute(
                """
                INSERT INTO users (
                    full_name, email, password_hash, role, owner_id,
                    staff_phone, staff_position, joined_date, staff_status, is_active
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    full_name,
                    email,
                    password_hash,
                    role,
                    owner_id,
                    staff_phone,
                    "Kasir",
                    datetime.now().date().isoformat(),
                    "Aktif",
                    1,
                ),
            )
            cashier_id = cursor.lastrowid
        else:
            cursor = db.execute(
                """
                INSERT INTO users (full_name, email, password_hash, role)
                VALUES (?, ?, ?, ?)
                """,
                (full_name, email, password_hash, role),
            )
            owner_id = cursor.lastrowid
        db.commit()
    except Exception:
        db.rollback()
        flash("Email sudah terdaftar atau database sedang bermasalah. Silakan gunakan email lain atau coba lagi.", "error")
        template_name = "register_owner.html" if role == "owner" else "register_staff.html"
        return render_template(
            template_name,
            full_name=full_name,
            email=email,
            staff_phone=staff_phone,
            invite_code=invite_code,
        )

    if role == CASHIER_ROLE and invitation:
        try:
            execute_commit(
                "UPDATE cashier_invitations SET status = ?, used_at = ?, used_by_cashier_id = ? WHERE id = ?",
                (
                    "Digunakan",
                    datetime.now().isoformat(timespec="seconds"),
                    cashier_id,
                    invitation["id"],
                ),
            )
        except Exception:
            pass

    role_label = role_display_name(role)

    send_email(
        email,
        "Registrasi Kyloffee Berhasil",
        f"""
        <h2>Halo {full_name}</h2>
        <p>Akun Kyloffee kamu berhasil dibuat.</p>
        <p>Role akun: <b>{role_label}</b></p>
        <p>Silakan login menggunakan email dan password yang sudah didaftarkan.</p>
        <br>
        <p>Kyloffee Team</p>
        """
    )

    flash(f"Registrasi {role_label} berhasil, silakan login.", "success")
    session["registered_email"] = email
    return redirect(url_for("login", email=email))


@app.route("/register/owner", methods=["GET", "POST"])
def register_owner():
    init_db()
    if session.get("user_id"):
        return redirect_for_role()
    if request.method == "POST":
        return register_user("owner")
    return render_template("register_owner.html")


@app.route("/register/kasir", methods=["GET", "POST"])
def register_cashier():
    init_db()
    if session.get("user_id"):
        return redirect_for_role()
    if request.method == "POST":
        return register_user("staff")
    return render_template("register_staff.html")


@app.route("/register/staff", methods=["GET", "POST"])
def register_staff():
    return register_cashier()


@app.route("/dashboard")
@login_required
def dashboard():
    return redirect_for_role()


@app.route("/owner/dashboard")
@owner_required
def owner_dashboard():
    return redirect(url_for("owner_menu"))


@app.route("/owner/menu")
@owner_required
def owner_menu():
    init_menu_table()
    page = request.args.get("page", 1, type=int)
    per_page = 8

    if page < 1:
        page = 1

    db = get_db()
    offset = (page - 1) * per_page
    total = fetch_scalar(db.execute("SELECT COUNT(*) FROM menus"))
    menus = db.execute(
        """
        SELECT
            m.id,
            m.name,
            COALESCE(c.name, m.category) AS category,
            m.category_id,
            m.code,
            m.price,
            m.stock,
            m.image,
            m.is_active
        FROM menus m
        LEFT JOIN categories c ON c.id = m.category_id
        ORDER BY m.id DESC
        LIMIT ? OFFSET ?
        """,
        (per_page, offset),
    ).fetchall()

    total_pages = max(1, (total + per_page - 1) // per_page)
    return render_template(
        "owner_menu.html",
        owner_name=get_owner_name(),
        active_page="menu",
        menus=menus,
        page=page,
        total_pages=total_pages,
        total=total,
        per_page=per_page,
    )


@app.route("/owner/menu/add", methods=["GET", "POST"])
@owner_required
def owner_menu_add():
    init_menu_table()
    category_options = get_category_options()

    if request.method == "POST":
        form_data = {
            "name": request.form.get("name", "").strip(),
            "category_id": request.form.get("category_id", "").strip(),
            "code": "",
            "price": request.form.get("price", "").strip(),
            "stock": request.form.get("stock", "").strip(),
            "description": request.form.get("description", "").strip(),
            "is_active": request.form.get("is_active", "1") == "1",
        }
        selected_category = get_menu_category_from_value(form_data["category_id"])

        errors = []
        if not form_data["name"]:
            errors.append("Nama item wajib diisi.")
        if not category_options:
            errors.append("Belum ada kategori. Tambahkan kategori terlebih dahulu.")
        elif not form_data["category_id"]:
            errors.append("Kategori wajib dipilih.")
        elif not selected_category:
            errors.append("Kategori tidak valid.")
        if not form_data["price"]:
            errors.append("Harga satuan wajib diisi.")
        else:
            try:
                price = parse_menu_price(form_data["price"])
            except ValueError:
                errors.append("Harga satuan harus berupa angka minimal Rp 500 dan kelipatan Rp 500.")
                price = None
        if not form_data["stock"]:
            errors.append("Stok wajib diisi.")
        else:
            try:
                stock = int(form_data["stock"])
                if stock < 0:
                    raise ValueError
            except ValueError:
                errors.append("Stok harus berupa angka.")
                stock = None
        if not form_data["description"]:
            errors.append("Deskripsi wajib diisi.")

        image_path = ""
        if request.files.get("image") and request.files["image"].filename:
            image_path, image_error = save_menu_image(request.files["image"])
            if image_error:
                errors.append(image_error)

        if errors:
            return render_template(
                "owner_menu_add.html",
                owner_name=get_owner_name(),
                active_page="menu",
                category_options=category_options,
                form_data=form_data,
                errors=errors,
            )

        category_name = selected_category["name"]
        menu_code = generate_menu_code(category_name, form_data["name"])

        execute_commit(
            """
            INSERT INTO menus (name, category, category_id, code, price, stock, description, image, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                form_data["name"],
                category_name,
                selected_category["id"],
                menu_code,
                price,
                stock,
                form_data["description"],
                image_path,
                1 if form_data["is_active"] else 0,
            ),
        )
        flash("Menu berhasil ditambahkan.", "success")
        return redirect(url_for("owner_menu"))

    return render_template(
        "owner_menu_add.html",
        owner_name=get_owner_name(),
        active_page="menu",
        category_options=category_options,
        form_data={},
        errors=[],
    )


@app.route("/owner/menu/<int:menu_id>/edit", methods=["GET", "POST"])
@owner_required
def owner_menu_edit(menu_id):
    init_menu_table()
    db = get_db()
    menu = db.execute(
        """
        SELECT m.*, COALESCE(c.name, m.category) AS category_name
        FROM menus m
        LEFT JOIN categories c ON c.id = m.category_id
        WHERE m.id = ?
        """,
        (menu_id,),
    ).fetchone()

    if menu is None:
        flash("Menu tidak ditemukan.", "error")
        return redirect(url_for("owner_menu"))

    category_options = get_category_options()

    if request.method == "POST":
        form_data = {
            "name": request.form.get("name", "").strip(),
            "category_id": request.form.get("category_id", "").strip(),
            "code": menu["code"],
            "price": request.form.get("price", "").strip(),
            "stock": request.form.get("stock", "").strip(),
            "description": request.form.get("description", "").strip(),
            "is_active": request.form.get("is_active", "1") == "1",
        }
        selected_category = get_menu_category_from_value(form_data["category_id"])

        errors = []
        if not form_data["name"]:
            errors.append("Nama item wajib diisi.")
        if not category_options:
            errors.append("Belum ada kategori. Tambahkan kategori terlebih dahulu.")
        elif not form_data["category_id"]:
            errors.append("Kategori wajib dipilih.")
        elif not selected_category:
            errors.append("Kategori tidak valid.")
        if not form_data["price"]:
            errors.append("Harga satuan wajib diisi.")
        else:
            try:
                price = parse_menu_price(form_data["price"])
            except ValueError:
                errors.append("Harga satuan harus berupa angka minimal Rp 500 dan kelipatan Rp 500.")
                price = None
        if not form_data["stock"]:
            errors.append("Stok wajib diisi.")
        else:
            try:
                stock = int(form_data["stock"])
                if stock < 0:
                    raise ValueError
            except ValueError:
                errors.append("Stok harus berupa angka.")
                stock = None
        if not form_data["description"]:
            errors.append("Deskripsi wajib diisi.")

        image_path = menu["image"] or ""
        if request.files.get("image") and request.files["image"].filename:
            image_path, image_error = save_menu_image(request.files["image"])
            if image_error:
                errors.append(image_error)

        if errors:
            return render_template(
                "owner_menu_edit.html",
                owner_name=get_owner_name(),
                active_page="menu",
                category_options=category_options,
                menu=dict(menu),
                form_data=form_data,
                errors=errors,
            )

        category_name = selected_category["name"]
        execute_commit(
            """
            UPDATE menus
            SET name = ?, category = ?, category_id = ?, price = ?, stock = ?, description = ?, image = ?, is_active = ?
            WHERE id = ?
            """,
            (
                form_data["name"],
                category_name,
                selected_category["id"],
                price,
                stock,
                form_data["description"],
                image_path,
                1 if form_data["is_active"] else 0,
                menu_id,
            ),
        )
        flash("Menu berhasil diperbarui.", "success")
        return redirect(url_for("owner_menu"))

    return render_template(
        "owner_menu_edit.html",
        owner_name=get_owner_name(),
        active_page="menu",
        category_options=category_options,
        menu=dict(menu),
        form_data={},
        errors=[],
    )


@app.route("/owner/menu/<int:menu_id>/delete", methods=["POST"])
@owner_required
def owner_menu_delete(menu_id):
    init_menu_table()
    db = get_db()
    menu = db.execute("SELECT id FROM menus WHERE id = ?", (menu_id,)).fetchone()

    if menu is None:
        flash("Menu tidak ditemukan.", "error")
        return redirect(url_for("owner_menu"))

    execute_commit("DELETE FROM menus WHERE id = ?", (menu_id,))
    flash("Menu berhasil dihapus.", "success")
    return redirect(url_for("owner_menu"))


def render_owner_categories(category_form=None):
    init_menu_table()
    search = request.args.get("q", "").strip()
    return render_template(
        "owner_categories.html",
        owner_name=get_owner_name(),
        active_page="categories",
        categories=fetch_category_management_rows(search),
        search=search,
        category_form=category_form or {},
    )


@app.route("/owner/categories", methods=["GET", "POST"])
@owner_required
def owner_categories():
    init_menu_table()

    if request.method == "POST":
        payload, errors = validate_category_payload(request.form)
        if errors:
            return render_owner_categories(
                {
                    "mode": "create",
                    "name": payload["name"],
                    "description": payload["description"] or "",
                    "errors": errors,
                }
            )

        try:
            execute_commit(
                """
                INSERT INTO categories (name, name_key, description)
                VALUES (?, ?, ?)
                """,
                (payload["name"], category_key(payload["name"]), payload["description"]),
            )
        except Exception:
            return render_owner_categories(
                {
                    "mode": "create",
                    "name": payload["name"],
                    "description": payload["description"] or "",
                    "errors": {"name": "Nama kategori sudah digunakan."},
                }
            )

        flash("Kategori berhasil ditambahkan.", "success")
        return redirect(url_for("owner_categories"))

    return render_owner_categories()


@app.route("/owner/categories/<int:category_id>/edit", methods=["POST"])
@owner_required
def owner_category_edit(category_id):
    init_menu_table()
    category = get_category_by_id(category_id)

    if category is None:
        flash("Kategori tidak ditemukan.", "error")
        return redirect(url_for("owner_categories"))

    payload, errors = validate_category_payload(request.form, exclude_id=category_id)
    if errors:
        return render_owner_categories(
            {
                "mode": "edit",
                "id": category_id,
                "name": payload["name"],
                "description": payload["description"] or "",
                "errors": errors,
            }
        )

    try:
        execute_commit(
            """
            UPDATE categories
            SET name = ?, name_key = ?, description = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (payload["name"], category_key(payload["name"]), payload["description"], category_id),
        )
        execute_commit("UPDATE menus SET category = ? WHERE category_id = ?", (payload["name"], category_id))
    except Exception:
        return render_owner_categories(
            {
                "mode": "edit",
                "id": category_id,
                "name": payload["name"],
                "description": payload["description"] or "",
                "errors": {"name": "Nama kategori sudah digunakan."},
            }
        )

    flash("Kategori berhasil diperbarui.", "success")
    return redirect(url_for("owner_categories"))


@app.route("/owner/categories/<int:category_id>/delete", methods=["POST"])
@owner_required
def owner_category_delete(category_id):
    init_menu_table()
    category = get_category_by_id(category_id)

    if category is None:
        flash("Kategori tidak ditemukan.", "error")
        return redirect(url_for("owner_categories"))

    menu_count = fetch_scalar(
        get_db().execute("SELECT COUNT(*) FROM menus WHERE category_id = ?", (category_id,))
    ) or 0
    if menu_count > 0:
        flash(
            f"Kategori ini masih digunakan oleh {menu_count} menu. Pindahkan menu ke kategori lain sebelum menghapus kategori.",
            "error",
        )
        return redirect(url_for("owner_categories"))

    execute_commit("DELETE FROM categories WHERE id = ?", (category_id,))
    flash("Kategori berhasil dihapus.", "success")
    return redirect(url_for("owner_categories"))


@app.route("/api/owner/categories", methods=["GET"])
@owner_required
def get_owner_categories():
    init_menu_table()
    search = request.args.get("q", "").strip()
    return jsonify({"success": True, "categories": fetch_category_management_rows(search)})


@app.route("/api/owner/categories", methods=["POST"])
@owner_required
def add_owner_category():
    init_menu_table()
    data = request.get_json(silent=True) or {}
    payload, errors = validate_category_payload(data)

    if errors:
        return jsonify({"success": False, "message": next(iter(errors.values())), "errors": errors}), 400

    try:
        cursor = execute_commit(
            """
            INSERT INTO categories (name, name_key, description)
            VALUES (?, ?, ?)
            """,
            (payload["name"], category_key(payload["name"]), payload["description"]),
        )
        category = get_category_by_id(cursor.lastrowid)
    except Exception:
        return jsonify({"success": False, "message": "Nama kategori sudah digunakan."}), 400

    return jsonify(
        {
            "success": True,
            "message": "Kategori berhasil ditambahkan.",
            "category": row_to_dict(category),
        }
    )


@app.route("/api/owner/categories/<int:category_id>", methods=["PUT", "PATCH"])
@owner_required
def update_owner_category(category_id):
    init_menu_table()
    category = get_category_by_id(category_id)
    if category is None:
        return jsonify({"success": False, "message": "Kategori tidak ditemukan."}), 404

    data = request.get_json(silent=True) or {}
    payload, errors = validate_category_payload(data, exclude_id=category_id)

    if errors:
        return jsonify({"success": False, "message": next(iter(errors.values())), "errors": errors}), 400

    try:
        execute_commit(
            """
            UPDATE categories
            SET name = ?, name_key = ?, description = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (payload["name"], category_key(payload["name"]), payload["description"], category_id),
        )
        execute_commit("UPDATE menus SET category = ? WHERE category_id = ?", (payload["name"], category_id))
    except Exception:
        return jsonify({"success": False, "message": "Nama kategori sudah digunakan."}), 400

    return jsonify(
        {
            "success": True,
            "message": "Kategori berhasil diperbarui.",
            "category": row_to_dict(get_category_by_id(category_id)),
        }
    )


@app.route("/api/owner/categories/<int:category_id>", methods=["DELETE"])
@owner_required
def delete_owner_category(category_id):
    init_menu_table()
    category = get_category_by_id(category_id)
    if category is None:
        return jsonify({"success": False, "message": "Kategori tidak ditemukan."}), 404

    menu_count = fetch_scalar(
        get_db().execute("SELECT COUNT(*) FROM menus WHERE category_id = ?", (category_id,))
    ) or 0
    if menu_count > 0:
        return jsonify(
            {
                "success": False,
                "message": f"Kategori ini masih digunakan oleh {menu_count} menu. Pindahkan menu ke kategori lain sebelum menghapus kategori.",
            }
        ), 400

    execute_commit("DELETE FROM categories WHERE id = ?", (category_id,))
    return jsonify({"success": True, "message": "Kategori berhasil dihapus."})


@app.route("/api/owner/menus", methods=["GET"])
@owner_required
def get_owner_menus():
    init_menu_table()
    db = get_db()

    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 6, type=int)
    search = request.args.get("q", "").strip()
    category_id = request.args.get("category_id", type=int)

    if page < 1:
        page = 1

    offset = (page - 1) * per_page
    where_parts = []
    params = []

    if search:
        where_parts.append("(m.name LIKE ? OR COALESCE(c.name, m.category) LIKE ? OR m.code LIKE ?)")
        keyword = f"%{search}%"
        params.extend([keyword, keyword, keyword])

    if category_id:
        where_parts.append("m.category_id = ?")
        params.append(category_id)

    where_clause = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
    total = fetch_scalar(
        db.execute(
            f"""
            SELECT COUNT(*)
            FROM menus m
            LEFT JOIN categories c ON c.id = m.category_id
            {where_clause}
            """,
            params,
        )
    )
    cursor = db.execute(
        f"""
        SELECT
            m.id,
            m.name,
            m.category_id,
            COALESCE(c.name, m.category) AS category,
            m.code,
            m.price
        FROM menus m
        LEFT JOIN categories c ON c.id = m.category_id
        {where_clause}
        ORDER BY m.id ASC
        LIMIT ? OFFSET ?
        """,
        params + [per_page, offset],
    )

    menus = fetch_all_dict(cursor)

    return jsonify(
        {
            "menus": menus,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": (total + per_page - 1) // per_page,
        }
    )


@app.route("/api/owner/menus", methods=["POST"])
@owner_required
def add_owner_menu():
    init_menu_table()
    data = request.get_json(silent=True) or {}

    name = str(data.get("name", "")).strip()
    category = get_menu_category_from_value(data.get("category_id"), data.get("category"))
    price_value = data.get("price")

    if not name or not category or price_value is None:
        return jsonify({"success": False, "message": "Semua field wajib diisi."}), 400

    try:
        price = parse_menu_price(price_value)
    except (TypeError, ValueError):
        return jsonify({"success": False, "message": "Harga harus berupa angka minimal Rp 500 dan kelipatan Rp 500."}), 400

    try:
        category_name = category["name"]
        code = generate_menu_code(category_name, name)
        execute_commit(
            "INSERT INTO menus (name, category, category_id, code, price) VALUES (?, ?, ?, ?, ?)",
            (name, category_name, category["id"], code, price),
        )
        return jsonify({"success": True, "message": "Menu berhasil ditambahkan.", "code": code})
    except Exception:
        return jsonify({"success": False, "message": "Kode menu sudah digunakan atau data tidak valid."}), 400


@app.route("/api/owner/menus/<int:menu_id>", methods=["PUT"])
@owner_required
def update_owner_menu(menu_id):
    init_menu_table()
    data = request.get_json(silent=True) or {}

    name = str(data.get("name", "")).strip()
    category = get_menu_category_from_value(data.get("category_id"), data.get("category"))
    price_value = data.get("price")

    if not name or not category or price_value is None:
        return jsonify({"success": False, "message": "Semua field wajib diisi."}), 400

    try:
        price = parse_menu_price(price_value)
    except (TypeError, ValueError):
        return jsonify({"success": False, "message": "Harga harus berupa angka minimal Rp 500 dan kelipatan Rp 500."}), 400

    try:
        category_name = category["name"]
        execute_commit(
            "UPDATE menus SET name = ?, category = ?, category_id = ?, price = ? WHERE id = ?",
            (name, category_name, category["id"], price, menu_id),
        )
        return jsonify({"success": True, "message": "Menu berhasil diperbarui."})
    except Exception:
        return jsonify({"success": False, "message": "Gagal memperbarui menu."}), 400


@app.route("/owner/products")
@owner_required
def owner_products():
    return render_template(
        "dashboard_placeholder.html",
        full_name=session.get("username", "Owner"),
        role="Owner",
        page_title="Produk Owner",
    )


@app.route("/owner/reports")
@owner_required
def owner_reports():
    return render_template(
        "owner_financial_reports.html",
        owner_name=get_owner_name(),
        active_page="reports",
        report=build_financial_report(request.args),
    )


@app.route("/owner/reports/print")
@owner_required
def owner_reports_print():
    return render_template(
        "owner_financial_report_print.html",
        owner_name=get_owner_name(),
        active_page="reports",
        report=build_financial_report(request.args),
    )


@app.route("/owner/users")
@owner_required
def owner_users():
    init_db()
    page = request.args.get("page", 1, type=int)
    per_page = 6
    owner_id = session.get("user_id")

    if page < 1:
        page = 1

    db = get_db()
    role_where = cashier_role_filter("role")
    total = fetch_scalar(
        db.execute(
            f"SELECT COUNT(*) FROM users WHERE {role_where} AND owner_id = ?",
            (*CASHIER_ROLE_ALIASES, owner_id),
        )
    ) or 0
    total_pages = max(1, (total + per_page - 1) // per_page)
    if total > 0 and page > total_pages:
        return redirect(url_for("owner_staff", page=total_pages))

    offset = (page - 1) * per_page
    staff_rows = db.execute(
        f"""
        SELECT id, full_name, email, staff_phone, staff_position, joined_date, staff_status, is_active, created_at
        FROM users
        WHERE {role_where} AND owner_id = ?
        ORDER BY id ASC
        LIMIT ? OFFSET ?
        """,
        (*CASHIER_ROLE_ALIASES, owner_id, per_page, offset),
    ).fetchall()

    return render_template(
        "owner_staff.html",
        owner_name=get_owner_name(),
        active_page="staff",
        staff_members=[format_staff_member(staff) for staff in staff_rows],
        invitations=get_owner_invitations(session.get("user_id")),
        page=page,
        total_pages=total_pages,
        total=total,
        per_page=per_page,
    )


@app.route("/owner/staff/invite", methods=["GET", "POST"])
@owner_required
def owner_staff_invite():
    init_db()
    owner_id = session.get("user_id")
    if request.method == "POST":
        expires_days = request.form.get("expires_days", "7")
        try:
            expires_days = int(expires_days)
            if expires_days < 1 or expires_days > 30:
                raise ValueError()
        except ValueError:
            expires_days = 7
        try:
            invitation = create_cashier_invitation(owner_id, expires_days=expires_days)
            flash("Kode undangan kasir berhasil dibuat.", "success")
            return render_template(
                "owner_staff_invite.html",
                owner_name=get_owner_name(),
                active_page="staff",
                invitation=invitation,
                invitations=get_owner_invitations(owner_id),
            )
        except Exception:
            flash("Gagal membuat kode undangan. Silakan coba lagi.", "error")
    return render_template(
        "owner_staff_invite.html",
        owner_name=get_owner_name(),
        active_page="staff",
        invitation=None,
        invitations=get_owner_invitations(session.get("user_id")),
    )


@app.route("/owner/staff")
@owner_required
def owner_staff():
    return owner_users()


@app.route("/owner/users/add", methods=["GET", "POST"])
@owner_required
def owner_users_add():
    init_db()

    if request.method == "POST":
        form_data = get_staff_form_data()
        errors, joined_date = validate_staff_form(form_data)

        if errors:
            return render_template(
                "owner_staff_add.html",
                owner_name=get_owner_name(),
                active_page="staff",
                form_data=form_data,
                staff_positions=STAFF_POSITIONS,
                errors=errors,
            )

        try:
            execute_commit(
                """
                INSERT INTO users (
                    full_name, email, password_hash, role, owner_id, staff_phone, staff_position,
                    joined_date, staff_status, is_active
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    form_data["full_name"],
                    form_data["email"],
                    generate_password_hash(STAFF_DEFAULT_PASSWORD),
                    CASHIER_ROLE,
                    session.get("user_id"),
                    form_data["staff_phone"],
                    "Kasir",
                    joined_date,
                    "Aktif",
                    1,
                ),
            )
        except Exception:
            return render_template(
                "owner_staff_add.html",
                owner_name=get_owner_name(),
                active_page="staff",
                form_data=form_data,
                staff_positions=STAFF_POSITIONS,
                errors=["Email sudah terdaftar. Gunakan email kasir yang berbeda."],
            )

        flash(f"Kasir berhasil ditambahkan. Password awal: {STAFF_DEFAULT_PASSWORD}", "success")
        return redirect(url_for("owner_staff"))

    return render_template(
        "owner_staff_add.html",
        owner_name=get_owner_name(),
        active_page="staff",
        form_data={},
        staff_positions=STAFF_POSITIONS,
        errors=[],
    )


@app.route("/owner/users/<int:staff_id>/edit", methods=["GET", "POST"])
@owner_required
def owner_users_edit(staff_id):
    init_db()
    db = get_db()
    role_where = cashier_role_filter("role")
    staff = db.execute(
        f"""
        SELECT id, full_name, email, staff_phone, staff_position, joined_date, staff_status, is_active, created_at
        FROM users
        WHERE id = ? AND {role_where} AND owner_id = ?
        """,
        (staff_id, *CASHIER_ROLE_ALIASES, session.get("user_id")),
    ).fetchone()

    if staff is None:
        flash("Data kasir tidak ditemukan.", "error")
        return redirect(url_for("owner_staff"))

    staff_data = format_staff_member(staff)

    if request.method == "POST":
        form_data = get_staff_form_data()
        errors, joined_date = validate_staff_form(form_data)

        if errors:
            return render_template(
                "owner_staff_edit.html",
                owner_name=get_owner_name(),
                active_page="staff",
                staff=staff_data,
                form_data=form_data,
                staff_positions=STAFF_POSITIONS,
                staff_statuses=STAFF_STATUSES,
                errors=errors,
            )

        try:
            execute_commit(
                f"""
                UPDATE users
                SET full_name = ?, email = ?, staff_phone = ?, staff_position = ?,
                    joined_date = ?, staff_status = ?, is_active = ?
                WHERE id = ? AND {role_where} AND owner_id = ?
                """,
                (
                    form_data["full_name"],
                    form_data["email"],
                    form_data["staff_phone"],
                    "Kasir",
                    joined_date,
                    form_data["staff_status"],
                    1 if form_data["is_active"] else 0,
                    staff_id,
                    *CASHIER_ROLE_ALIASES,
                    session.get("user_id"),
                ),
            )
        except Exception:
            return render_template(
                "owner_staff_edit.html",
                owner_name=get_owner_name(),
                active_page="staff",
                staff=staff_data,
                form_data=form_data,
                staff_positions=STAFF_POSITIONS,
                staff_statuses=STAFF_STATUSES,
                errors=["Email sudah digunakan akun lain. Gunakan email kasir yang berbeda."],
            )

        flash("Data kasir berhasil diperbarui.", "success")
        return redirect(url_for("owner_staff"))

    return render_template(
        "owner_staff_edit.html",
        owner_name=get_owner_name(),
        active_page="staff",
        staff=staff_data,
        form_data={},
        staff_positions=STAFF_POSITIONS,
        staff_statuses=STAFF_STATUSES,
        errors=[],
    )


@app.route("/owner/<path:unused_path>")
@owner_required
def owner_fallback(unused_path):
    return redirect(url_for("owner_menu"))


@app.route("/pos")
@staff_required
def pos():
    init_menu_table()
    init_pos_tables()
    db = get_db()
    products = db.execute(
        """
        SELECT
            m.id,
            m.name,
            m.description,
            m.price,
            m.image,
            m.stock,
            COALESCE(c.name, m.category) AS category,
            m.category_id,
            m.code,
            m.is_active
        FROM menus m
        LEFT JOIN categories c ON c.id = m.category_id
        WHERE m.is_active = 1
        ORDER BY m.id DESC
        """
    ).fetchall()
    products = [dict(product) for product in products]
    active_categories = []
    for product in products:
        category = str(product.get("category") or "").strip()
        product["category"] = category
        if category:
            active_categories.append(category)

    return render_template(
        "pos.html",
        shift=get_current_shift(),
        staff_name=session.get("full_name", "Kasir"),
        menu_categories=get_pos_category_filters(active_categories),
        products=products,
    )


@app.route("/pos/payment")
@staff_required
def pos_payment():
    init_pos_tables()
    return render_template(
        "pos_payment.html",
        shift=get_current_shift(),
        staff_name=session.get("full_name", "Kasir"),
    )


@app.route("/api/pos/checkout", methods=["POST"])
@staff_required
def pos_checkout():
    data = request.get_json(silent=True) or {}
    try:
        transaction = create_pos_transaction(data)
        payment_method = "QRIS" if str(data.get("payment_method") or "").strip().lower() == "qris" else "Cash"
        received_amount = (
            transaction["total_amount"]
            if payment_method == "QRIS"
            else parse_pos_amount(data.get("received_amount"), "Nominal diterima")
        )
        change_amount = max(received_amount - transaction["total_amount"], 0)
        remember_payment_details(
            transaction["order_code"],
            payment_method,
            transaction["total_amount"],
            received_amount,
            change_amount,
        )
    except ValueError as exc:
        return jsonify({"success": False, "message": str(exc)}), 400
    except Exception:
        app.logger.exception("POS checkout failed.")
        return jsonify({"success": False, "message": "Gagal menyimpan transaksi POS. Silakan coba lagi."}), 500

    return jsonify(
        {
            "success": True,
            "message": f"Transaksi {transaction['order_code']} berhasil disimpan.",
            "transaction": {
                **transaction,
                "subtotal_display": format_currency(transaction["subtotal_amount"]),
                "discount_display": format_currency(transaction["discount_amount"]),
                "total_display": format_currency(transaction["total_amount"]),
                "received_amount": received_amount,
                "received_display": format_currency(received_amount),
                "change_amount": change_amount,
                "change_display": format_currency(change_amount),
                "success_url": url_for("payment_success", order_code=transaction["order_code"]),
                "receipt_url": url_for("pos_receipt", order_code=transaction["order_code"]),
            },
        }
    )


@app.route("/api/pos/qris", methods=["POST"])
@staff_required
def pos_qris_payload():
    data = request.get_json(silent=True) or {}
    try:
        total_amount = parse_pos_amount(data.get("total_amount"), "Total pembayaran")
    except ValueError as exc:
        return jsonify({"success": False, "message": str(exc)}), 400

    if total_amount <= 0:
        return jsonify({"success": False, "message": "Total pembayaran harus lebih dari Rp 0."}), 400

    timestamp = datetime.now().replace(microsecond=0).isoformat(timespec="minutes")
    order_code = normalize_order_code(data.get("order_code")) or generate_invoice_code()
    payload = build_qris_payload(order_code, total_amount, timestamp)

    return jsonify(
        {
            "success": True,
            "order_code": order_code,
            "timestamp": timestamp,
            "payload": payload,
            "qr_url": url_for(
                "pos_qris_code",
                order_code=order_code,
                total=total_amount,
                timestamp=timestamp,
            ),
        }
    )


@app.route("/pos/qris-code/<order_code>.png")
@staff_required
def pos_qris_code(order_code):
    if qrcode is None:
        return "Paket qrcode belum terpasang. Jalankan pip install -r requirements.txt.", 503

    total_amount = parse_pos_amount(request.args.get("total"), "Total pembayaran")
    timestamp = request.args.get("timestamp", datetime.now().replace(microsecond=0).isoformat(timespec="minutes"))
    order_code = normalize_order_code(order_code) or generate_invoice_code()
    payload = build_qris_payload(order_code, total_amount, timestamp)

    qr = qrcode.QRCode(version=None, box_size=12, border=2)
    qr.add_data(payload)
    qr.make(fit=True)
    image = qr.make_image(fill_color="#3A1E1A", back_color="white")
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    buffer.seek(0)
    return send_file(buffer, mimetype="image/png", download_name=f"{order_code}.png")


@app.route("/pos/payment/success/<order_code>")
@staff_required
def payment_success(order_code):
    order_code = normalize_order_code(order_code)
    transaction = fetch_transaction_detail(order_code)
    if transaction is None:
        flash("Transaksi tidak ditemukan.", "error")
        return redirect(url_for("pos"))

    payment = get_payment_details(order_code, transaction)
    return render_template(
        "payment_success.html",
        transaction=transaction,
        payment=payment,
        total_display=format_currency(transaction.get("total_amount") or 0),
        received_display=format_currency(payment["received_amount"]),
        change_display=format_currency(payment["change_amount"]),
    )


@app.route("/pos/receipt/<order_code>")
@staff_required
def pos_receipt(order_code):
    order_code = normalize_order_code(order_code)
    transaction = fetch_transaction_detail(order_code)
    if transaction is None:
        flash("Transaksi tidak ditemukan.", "error")
        return redirect(url_for("pos"))

    payment = get_payment_details(order_code, transaction)
    return render_template(
        "receipt.html",
        transaction=transaction,
        payment=payment,
        received_display=format_currency(payment["received_amount"]),
        change_display=format_currency(payment["change_amount"]),
    )


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


def initialize_database():
    with app.app_context():
        init_db()
        init_menu_table()
        init_pos_tables()


if __name__ == "__main__":
    initialize_database()
    app.run(debug=True)
