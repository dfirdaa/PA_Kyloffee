import os
import ssl
import sqlite3
import uuid
from io import BytesIO
from functools import wraps
from pathlib import Path
from datetime import date, datetime, timedelta

from dotenv import load_dotenv
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

STAFF_INVITATION_CODE = "KYLOFFEE-STAFF"
STAFF_DEFAULT_PASSWORD = os.getenv("STAFF_DEFAULT_PASSWORD", "kyloffee123")
MIN_MENU_PRICE = 500
MENU_CATEGORIES = [
    "Black Series",
    "White Series",
    "Signature Series",
    "Non Coffee",
    "Healthy Juice",
    "Mocktail Series",
    "Food",
]
STAFF_POSITIONS = ["Kasir", "Barista", "Admin", "Supervisor"]
STAFF_STATUSES = ["Aktif", "Cuti", "Nonaktif"]

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
        execute_commit("ALTER TABLE users ADD COLUMN staff_position VARCHAR(100) DEFAULT 'Staff'" if db.is_mysql else "ALTER TABLE users ADD COLUMN staff_position TEXT DEFAULT 'Staff'")
    if "joined_date" not in columns:
        execute_commit("ALTER TABLE users ADD COLUMN joined_date DATE" if db.is_mysql else "ALTER TABLE users ADD COLUMN joined_date TEXT")
    if "staff_status" not in columns:
        execute_commit("ALTER TABLE users ADD COLUMN staff_status VARCHAR(40) DEFAULT 'Aktif'" if db.is_mysql else "ALTER TABLE users ADD COLUMN staff_status TEXT DEFAULT 'Aktif'")
    if "is_active" not in columns:
        execute_commit("ALTER TABLE users ADD COLUMN is_active TINYINT NOT NULL DEFAULT 1" if db.is_mysql else "ALTER TABLE users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")


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
                staff_phone VARCHAR(40),
                staff_position VARCHAR(100) DEFAULT 'Staff',
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
                staff_phone TEXT,
                staff_position TEXT DEFAULT 'Staff',
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


def init_menu_table():
    db = get_db()

    if db.is_mysql:
        execute_script_commit(
            """
            CREATE TABLE IF NOT EXISTS menus (
                id BIGINT PRIMARY KEY AUTO_INCREMENT,
                name VARCHAR(255) NOT NULL,
                category VARCHAR(100) NOT NULL,
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
                staff_id BIGINT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
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
    role = session.get("role")
    if role == "owner":
        return redirect(url_for("owner_menu"))
    if role == "staff":
        return redirect(url_for("pos"))
    session.clear()
    return redirect(url_for("login"))


def staff_required(view_func):
    @wraps(view_func)
    def wrapped_view(**kwargs):
        if not session.get("user_id"):
            flash("Silakan login terlebih dahulu.", "error")
            return redirect(url_for("login"))
        if session.get("role") != "staff":
            return redirect_for_role()

        user = get_db().execute(
            "SELECT role, is_active FROM users WHERE id = ?",
            (session.get("user_id"),),
        ).fetchone()
        user_data = row_to_dict(user)
        if not user_data or str(user_data.get("role") or "").strip().lower() != "staff":
            session.clear()
            flash("Sesi tidak valid. Silakan login ulang.", "error")
            return redirect(url_for("login"))
        if int(user_data.get("is_active", 1) or 0) != 1:
            session.clear()
            flash("Akun staff ini sedang nonaktif. Silakan hubungi owner.", "error")
            return redirect(url_for("login"))

        return view_func(**kwargs)

    return wrapped_view


def owner_required(view_func):
    @wraps(view_func)
    def wrapped_view(**kwargs):
        if not session.get("user_id"):
            flash("Silakan login terlebih dahulu.", "error")
            return redirect(url_for("login"))
        if session.get("role") != "owner":
            return redirect_for_role()
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


def validate_menu_category(category):
    return category in MENU_CATEGORIES


@app.template_global()
def category_key(category):
    return " ".join(str(category or "").strip().lower().split())


def get_ordered_menu_categories(categories):
    known_category_keys = {category_key(item) for item in MENU_CATEGORIES}
    category_lookup = {}
    for category in categories:
        category_name = str(category or "").strip()
        if category_name:
            category_lookup[category_key(category_name)] = category_name

    ordered_categories = [
        category
        for category in MENU_CATEGORIES
        if category_key(category) in category_lookup
    ]
    extra_categories = sorted(
        category
        for key, category in category_lookup.items()
        if key not in known_category_keys
    )
    return ordered_categories + extra_categories


def format_report_datetime(value):
    return f"{format_short_date(value.date())} {value:%H:%M} WIB"


def format_currency(amount):
    return f"Rp {int(amount or 0):,}".replace(",", ".")


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


def get_period_totals(start_date, end_date):
    db = get_db()
    row = row_to_dict(
        db.execute(
            """
            SELECT
                COALESCE(SUM(total_amount), 0) AS revenue,
                COALESCE(SUM(operational_cost), 0) AS cost,
                COUNT(*) AS transactions
            FROM pos_transactions
            WHERE transaction_date BETWEEN ? AND ?
              AND LOWER(status) IN ('selesai', 'paid', 'completed', 'complete')
            """,
            (start_date.isoformat(), end_date.isoformat()),
        ).fetchone()
    )
    revenue = int(row.get("revenue") or 0)
    cost = int(row.get("cost") or 0)
    transactions = int(row.get("transactions") or 0)
    return {
        "revenue": revenue,
        "cost": cost,
        "profit": revenue - cost,
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


def fetch_daily_details(start_date, end_date):
    db = get_db()
    rows = fetch_all_dict(
        db.execute(
            """
            SELECT
                transaction_date,
                COUNT(*) AS transactions,
                COALESCE(SUM(total_amount), 0) AS income,
                COALESCE(SUM(operational_cost), 0) AS cost
            FROM pos_transactions
            WHERE transaction_date BETWEEN ? AND ?
              AND LOWER(status) IN ('selesai', 'paid', 'completed', 'complete')
            GROUP BY transaction_date
            ORDER BY transaction_date DESC
            """,
            (start_date.isoformat(), end_date.isoformat()),
        )
    )

    details = []
    for row in rows:
        income = int(row.get("income") or 0)
        cost = int(row.get("cost") or 0)
        detail_date = parse_report_date(str(row.get("transaction_date")))
        details.append(
            {
                "date": format_short_date(detail_date) if detail_date else row.get("transaction_date"),
                "transactions": int(row.get("transactions") or 0),
                "income": format_currency(income),
                "cost": format_currency(cost),
                "profit": format_currency(income - cost),
            }
        )
    return details


def fetch_recent_transactions(start_date, end_date, limit=5):
    db = get_db()
    rows = fetch_all_dict(
        db.execute(
            """
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
            ORDER BY t.transaction_date DESC, t.transaction_time DESC, t.id DESC
            LIMIT ?
            """,
            (start_date.isoformat(), end_date.isoformat(), limit),
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


def fetch_hourly_sales(start_date, end_date):
    db = get_db()
    rows = fetch_all_dict(
        db.execute(
            """
            SELECT transaction_time, total_amount
            FROM pos_transactions
            WHERE transaction_date BETWEEN ? AND ?
              AND LOWER(status) IN ('selesai', 'paid', 'completed', 'complete')
            """,
            (start_date.isoformat(), end_date.isoformat()),
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


def build_monthly_summary(end_date):
    current_month = end_date.replace(day=1)
    rows = []
    for month_delta in (-2, -1, 0):
        month_start = shift_month(current_month, month_delta)
        month_end = shift_month(month_start, 1) - timedelta(days=1)
        totals = get_period_totals(month_start, month_end)
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
    totals = get_period_totals(start_date, end_date)
    day_count = max((end_date - start_date).days + 1, 1)
    previous_end = start_date - timedelta(days=1)
    previous_start = previous_end - timedelta(days=day_count - 1)
    previous_totals = get_period_totals(previous_start, previous_end)

    today = now.date()
    today_totals = get_period_totals(today, today)
    average_income = totals["revenue"] // day_count
    previous_average = previous_totals["revenue"] // day_count if day_count else 0
    period_label = format_report_period(start_date, end_date)
    daily_details = fetch_daily_details(start_date, end_date)
    recent_transactions = fetch_recent_transactions(start_date, end_date, limit=5)

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
                "label": "Total Biaya Operasional",
                "value": format_currency(totals["cost"]),
                "trend": build_trend_text(totals["cost"], previous_totals["cost"], "Belum ada biaya"),
                "tone": trend_tone(previous_totals["cost"], totals["cost"]),
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
            {"label": "Total Biaya Operasional", "value": format_currency(totals["cost"]), "tone": "danger"},
            {"label": "Total Transaksi", "value": str(totals["transactions"]), "tone": "normal"},
            {"label": "Rata-rata Pendapatan Harian", "value": format_currency(average_income), "tone": "normal"},
        ],
        "net_profit": format_currency(totals["profit"]),
        "net_profit_trend": build_trend_text(totals["profit"], previous_totals["profit"], "Belum ada data periode lalu"),
        "hourly_sales": fetch_hourly_sales(start_date, end_date),
        "daily_details": daily_details,
        "daily_totals": {
            "transactions": str(totals["transactions"]),
            "income": format_currency(totals["revenue"]),
            "cost": format_currency(totals["cost"]),
            "profit": format_currency(totals["profit"]),
        },
        "recent_transactions": recent_transactions,
        "print_transactions": fetch_recent_transactions(start_date, end_date, limit=20),
        "monthly_summary": build_monthly_summary(end_date),
        "daily_income_rows": daily_details[:6],
    }


def parse_menu_price(price_value):
    price = int(price_value)
    if price < MIN_MENU_PRICE:
        raise ValueError
    return price


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
    return name[:1].upper() if name else "S"


def format_staff_member(row):
    data = row_to_dict(row)
    is_active = int(data.get("is_active", 1) or 0) == 1
    status = normalize_staff_status(data.get("staff_status"), is_active)
    joined_date = data.get("joined_date") or data.get("created_at")
    return {
        "id": data.get("id"),
        "full_name": data.get("full_name") or "Staff",
        "initial": staff_initial(data.get("full_name")),
        "email": data.get("email") or "-",
        "phone": data.get("staff_phone") or "-",
        "phone_value": data.get("staff_phone") or "",
        "position": data.get("staff_position") or "Staff",
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
        "staff_position": request.form.get("staff_position", "").strip(),
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
        errors.append("Peran staff wajib diisi.")
    joined_date = parse_staff_date(form_data["joined_date"])
    if not joined_date:
        errors.append("Tanggal bergabung wajib diisi dengan format tanggal yang valid.")
    if form_data["staff_status"] not in STAFF_STATUSES:
        errors.append("Status staff tidak valid.")
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
                t.discount_amount,
                t.tax_amount,
                t.operational_cost,
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
    transaction["discount_display"] = format_currency(transaction.get("discount_amount") or 0)
    transaction["tax_display"] = format_currency(transaction.get("tax_amount") or 0)
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
    tax_amount = parse_pos_amount(data.get("tax_amount"), "Pajak")
    operational_cost = parse_pos_amount(data.get("operational_cost"), "Biaya operasional")

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
    if discount_amount > subtotal_amount + tax_amount:
        raise ValueError("Diskon tidak boleh lebih besar dari subtotal dan pajak.")

    total_amount = subtotal_amount - discount_amount + tax_amount
    if payment_method == "Cash":
        received_amount = parse_pos_amount(data.get("received_amount"), "Nominal diterima")
        if received_amount < total_amount:
            raise ValueError("Nominal diterima kurang dari total pembayaran.")

    now = datetime.now()
    order_code = normalize_order_code(data.get("order_code")) or generate_order_code(now)

    try:
        cursor = db.execute(
            """
            INSERT INTO pos_transactions (
                order_code, transaction_date, transaction_time, customer_name,
                payment_method, subtotal_amount, discount_amount, tax_amount,
                operational_cost, total_amount, item_count, status, staff_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        "tax_amount": tax_amount,
        "operational_cost": operational_cost,
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
        user_role = str(user_data.get("role") or "").strip().lower()
        if user_role == "staff" and int(user_data.get("is_active", 1) or 0) != 1:
            flash("Akun staff ini sedang nonaktif. Silakan hubungi owner.", "error")
            return render_template("login.html", email=email)

        session.clear()
        session["user_id"] = user_data["id"]
        session["full_name"] = user_data["full_name"]
        session["name"] = user_data["full_name"]
        session["username"] = user_data["full_name"]
        session["role"] = user_role
        flash(f"Login sebagai {session['role'].title()} berhasil.", "success")
        return redirect_for_role()

    registered_email = session.pop("registered_email", "")
    query_email = request.args.get("email", "").strip().lower()
    return render_template("login.html", email=query_email or registered_email)


def register_user(role):
    full_name = request.form.get("full_name", "").strip()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")

    errors = validate_auth_fields(full_name=full_name, email=email, password=password)
    if role == "staff":
        invitation_code = request.form.get("invitation_code", "").strip()
        if invitation_code != STAFF_INVITATION_CODE:
            errors.append("Kode undangan tidak valid.")

    if errors:
        for error in errors:
            flash(error, "error")
        template_name = "register_owner.html" if role == "owner" else "register_staff.html"
        return render_template(
            template_name,
            full_name=full_name,
            email=email,
        )

    password_hash = generate_password_hash(password)
    db = get_db()
    try:
        db.execute(
            """
            INSERT INTO users (full_name, email, password_hash, role)
            VALUES (?, ?, ?, ?)
            """,
            (full_name, email, password_hash, role),
        )
        db.commit()
    except Exception:
        db.rollback()
        flash("Email sudah terdaftar atau database sedang bermasalah. Silakan gunakan email lain atau coba lagi.", "error")
        template_name = "register_owner.html" if role == "owner" else "register_staff.html"
        return render_template(
            template_name,
            full_name=full_name,
            email=email,
        )

    role_label = "Owner" if role == "owner" else "Staff"
    flash(f"Registrasi {role_label} berhasil, silakan login.", "success")
    session["registered_email"] = email
    return redirect(url_for("login", email=email))


@app.route("/register/owner", methods=["GET", "POST"])
def register_owner():
    if session.get("user_id"):
        return redirect_for_role()
    if request.method == "POST":
        return register_user("owner")
    return render_template("register_owner.html")


@app.route("/register/staff", methods=["GET", "POST"])
def register_staff():
    if session.get("user_id"):
        return redirect_for_role()
    if request.method == "POST":
        return register_user("staff")
    return render_template("register_staff.html")


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
        SELECT id, name, category, code, price, stock, image, is_active
        FROM menus
        ORDER BY id DESC
        LIMIT ? OFFSET ?
        """,
        (per_page, offset),
    ).fetchall()

    total_pages = max(1, (total + per_page - 1) // per_page)
    return render_template(
        "owner_menu.html",
        owner_name=get_owner_name(),
        active_page="menu",
        menu_categories=MENU_CATEGORIES,
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

    if request.method == "POST":
        form_data = {
            "name": request.form.get("name", "").strip(),
            "category": request.form.get("category", "").strip(),
            "code": request.form.get("code", "").strip().upper(),
            "price": request.form.get("price", "").strip(),
            "stock": request.form.get("stock", "").strip(),
            "description": request.form.get("description", "").strip(),
            "is_active": request.form.get("is_active", "1") == "1",
        }

        errors = []
        if not form_data["name"]:
            errors.append("Nama item wajib diisi.")
        if not form_data["category"]:
            errors.append("Kategori wajib dipilih.")
        elif not validate_menu_category(form_data["category"]):
            errors.append("Kategori tidak valid.")
        if not form_data["code"]:
            errors.append("SKU / ID item wajib diisi.")
        if not form_data["price"]:
            errors.append("Harga satuan wajib diisi.")
        else:
            try:
                price = parse_menu_price(form_data["price"])
            except ValueError:
                errors.append("Harga satuan harus berupa angka minimal Rp 500.")
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
                menu_categories=MENU_CATEGORIES,
                form_data=form_data,
                errors=errors,
            )

        execute_commit(
            """
            INSERT INTO menus (name, category, code, price, stock, description, image, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                form_data["name"],
                form_data["category"],
                form_data["code"],
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
        menu_categories=MENU_CATEGORIES,
        form_data={},
        errors=[],
    )


@app.route("/owner/menu/<int:menu_id>/edit", methods=["GET", "POST"])
@owner_required
def owner_menu_edit(menu_id):
    init_menu_table()
    db = get_db()
    menu = db.execute("SELECT * FROM menus WHERE id = ?", (menu_id,)).fetchone()

    if menu is None:
        flash("Menu tidak ditemukan.", "error")
        return redirect(url_for("owner_menu"))

    if request.method == "POST":
        form_data = {
            "name": request.form.get("name", "").strip(),
            "category": request.form.get("category", "").strip(),
            "code": request.form.get("code", "").strip().upper(),
            "price": request.form.get("price", "").strip(),
            "stock": request.form.get("stock", "").strip(),
            "description": request.form.get("description", "").strip(),
            "is_active": request.form.get("is_active", "1") == "1",
        }

        errors = []
        if not form_data["name"]:
            errors.append("Nama item wajib diisi.")
        if not form_data["category"]:
            errors.append("Kategori wajib dipilih.")
        elif not validate_menu_category(form_data["category"]):
            errors.append("Kategori tidak valid.")
        if not form_data["code"]:
            errors.append("SKU / ID item wajib diisi.")
        if not form_data["price"]:
            errors.append("Harga satuan wajib diisi.")
        else:
            try:
                price = parse_menu_price(form_data["price"])
            except ValueError:
                errors.append("Harga satuan harus berupa angka minimal Rp 500.")
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
                menu_categories=MENU_CATEGORIES,
                menu=dict(menu),
                form_data=form_data,
                errors=errors,
            )

        execute_commit(
            """
            UPDATE menus
            SET name = ?, category = ?, code = ?, price = ?, stock = ?, description = ?, image = ?, is_active = ?
            WHERE id = ?
            """,
            (
                form_data["name"],
                form_data["category"],
                form_data["code"],
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
        menu_categories=MENU_CATEGORIES,
        menu=dict(menu),
        form_data={},
        errors=[],
    )


@app.route("/api/owner/menus", methods=["GET"])
@owner_required
def get_owner_menus():
    init_menu_table()
    db = get_db()

    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 6, type=int)
    search = request.args.get("q", "").strip()

    if page < 1:
        page = 1

    offset = (page - 1) * per_page
    where_clause = ""
    params = []

    if search:
        where_clause = " WHERE name LIKE ? OR category LIKE ? OR code LIKE ?"
        keyword = f"%{search}%"
        params = [keyword, keyword, keyword]

    total = fetch_scalar(db.execute(f"SELECT COUNT(*) FROM menus {where_clause}", params))
    cursor = db.execute(
        f"""
        SELECT id, name, category, code, price
        FROM menus
        {where_clause}
        ORDER BY id ASC
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
    category = str(data.get("category", "")).strip()
    code = str(data.get("code", "")).strip().upper()
    price_value = data.get("price")

    if not name or not category or not code or price_value is None:
        return jsonify({"success": False, "message": "Semua field wajib diisi."}), 400
    if not validate_menu_category(category):
        return jsonify({"success": False, "message": "Kategori tidak valid."}), 400

    try:
        price = parse_menu_price(price_value)
    except (TypeError, ValueError):
        return jsonify({"success": False, "message": "Harga harus berupa angka minimal Rp 500."}), 400

    try:
        execute_commit(
            "INSERT INTO menus (name, category, code, price) VALUES (?, ?, ?, ?)",
            (name, category, code, price),
        )
        return jsonify({"success": True, "message": "Menu berhasil ditambahkan."})
    except Exception:
        return jsonify({"success": False, "message": "Kode menu sudah digunakan atau data tidak valid."}), 400


@app.route("/api/owner/menus/<int:menu_id>", methods=["PUT"])
@owner_required
def update_owner_menu(menu_id):
    init_menu_table()
    data = request.get_json(silent=True) or {}

    name = str(data.get("name", "")).strip()
    category = str(data.get("category", "")).strip()
    code = str(data.get("code", "")).strip().upper()
    price_value = data.get("price")

    if not name or not category or not code or price_value is None:
        return jsonify({"success": False, "message": "Semua field wajib diisi."}), 400
    if not validate_menu_category(category):
        return jsonify({"success": False, "message": "Kategori tidak valid."}), 400

    try:
        price = parse_menu_price(price_value)
    except (TypeError, ValueError):
        return jsonify({"success": False, "message": "Harga harus berupa angka minimal Rp 500."}), 400

    try:
        execute_commit(
            "UPDATE menus SET name = ?, category = ?, code = ?, price = ? WHERE id = ?",
            (name, category, code, price, menu_id),
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

    if page < 1:
        page = 1

    db = get_db()
    offset = (page - 1) * per_page
    total = fetch_scalar(db.execute("SELECT COUNT(*) FROM users WHERE LOWER(role) = ?", ("staff",))) or 0
    staff_rows = db.execute(
        """
        SELECT id, full_name, email, staff_phone, staff_position, joined_date, staff_status, is_active, created_at
        FROM users
        WHERE LOWER(role) = ?
        ORDER BY id ASC
        LIMIT ? OFFSET ?
        """,
        ("staff", per_page, offset),
    ).fetchall()

    total_pages = max(1, (total + per_page - 1) // per_page)
    return render_template(
        "owner_staff.html",
        owner_name=get_owner_name(),
        active_page="staff",
        staff_members=[format_staff_member(staff) for staff in staff_rows],
        page=page,
        total_pages=total_pages,
        total=total,
        per_page=per_page,
    )


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
                    full_name, email, password_hash, role, staff_phone, staff_position,
                    joined_date, staff_status, is_active
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    form_data["full_name"],
                    form_data["email"],
                    generate_password_hash(STAFF_DEFAULT_PASSWORD),
                    "staff",
                    form_data["staff_phone"],
                    form_data["staff_position"],
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
                errors=["Email sudah terdaftar. Gunakan email staff yang berbeda."],
            )

        flash(f"Staff berhasil ditambahkan. Password awal: {STAFF_DEFAULT_PASSWORD}", "success")
        return redirect(url_for("owner_users"))

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
    staff = db.execute(
        """
        SELECT id, full_name, email, staff_phone, staff_position, joined_date, staff_status, is_active, created_at
        FROM users
        WHERE id = ? AND LOWER(role) = ?
        """,
        (staff_id, "staff"),
    ).fetchone()

    if staff is None:
        flash("Data staff tidak ditemukan.", "error")
        return redirect(url_for("owner_users"))

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
                """
                UPDATE users
                SET full_name = ?, email = ?, staff_phone = ?, staff_position = ?,
                    joined_date = ?, staff_status = ?, is_active = ?
                WHERE id = ? AND LOWER(role) = ?
                """,
                (
                    form_data["full_name"],
                    form_data["email"],
                    form_data["staff_phone"],
                    form_data["staff_position"],
                    joined_date,
                    form_data["staff_status"],
                    1 if form_data["is_active"] else 0,
                    staff_id,
                    "staff",
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
                errors=["Email sudah digunakan akun lain. Gunakan email yang berbeda."],
            )

        flash("Data staff berhasil diperbarui.", "success")
        return redirect(url_for("owner_users"))

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
        SELECT id, name, description, price, image, stock, category, code, is_active
        FROM menus
        WHERE is_active = 1
        ORDER BY id DESC
        """
    ).fetchall()
    return render_template(
        "pos.html",
        shift=get_current_shift(),
        staff_name=session.get("full_name", "Staff"),
        menu_categories=MENU_CATEGORIES,
        products=[dict(product) for product in products],
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
                "tax_display": format_currency(transaction["tax_amount"]),
                "operational_cost_display": format_currency(transaction["operational_cost"]),
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
