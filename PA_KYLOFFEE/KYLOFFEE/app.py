import os
import ssl
import sqlite3
import uuid
from functools import wraps
from pathlib import Path
from datetime import datetime

from dotenv import load_dotenv
from flask import (
    Flask,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
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


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


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
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )


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


def build_financial_report():
    return {
        "period": "1 Okt - 31 Okt 2023",
        "printed_at": "1 Nov 2023 08:30 WIB",
        "dashboard_metrics": [
            {
                "label": "TOTAL PENDAPATAN BULAN INI",
                "value": "Rp 12.450.000",
                "trend": "+12.5% dari bulan lalu",
                "tone": "positive",
            },
            {
                "label": "TOTAL BIAYA OPERASIONAL",
                "value": "Rp 5.200.000",
                "trend": "-2.1% dari bulan lalu",
                "tone": "negative",
            },
            {
                "label": "LABA BERSIH",
                "value": "Rp 7.250.000",
                "trend": "+15.2% dari bulan lalu",
                "tone": "positive",
            },
            {
                "label": "TOTAL TRANSAKSI",
                "value": "842",
                "trend": "+4.2% dari bulan lalu",
                "tone": "positive",
            },
            {
                "label": "PENDAPATAN HARI INI",
                "value": "Rp 450.000",
                "trend": "",
                "tone": "neutral",
            },
            {
                "label": "RATA-RATA PENDAPATAN HARIAN",
                "value": "Rp 415.000",
                "trend": "",
                "tone": "neutral",
            },
        ],
        "sales_hours": [
            {"time": "08:00", "height": 34, "active": False},
            {"time": "10:00", "height": 50, "active": False},
            {"time": "12:00", "height": 82, "active": True},
            {"time": "14:00", "height": 98, "active": True},
            {"time": "16:00", "height": 92, "active": True},
            {"time": "18:00", "height": 64, "active": True},
            {"time": "20:00", "height": 44, "active": False},
            {"time": "22:00", "height": 28, "active": False},
        ],
        "recent_transactions": [
            {"id": "#POS-8492", "time": "10:42 AM", "items": "3 Item", "customer": "Jordan Smith", "total": "Rp 32.500", "status": "Selesai"},
            {"id": "#POS-8491", "time": "10:38 AM", "items": "1 Item", "customer": "Elena Rodriguez", "total": "Rp 6.750", "status": "Selesai"},
            {"id": "#POS-8490", "time": "10:25 AM", "items": "5 Item", "customer": "Marcus Thorne", "total": "Rp 54.200", "status": "Menunggu"},
        ],
        "monthly_summary": [
            {"month": "Agustus 2023", "income": "Rp 38.500.000", "profit": "Rp 22.100.000", "highlight": False},
            {"month": "September 2023", "income": "Rp 40.200.000", "profit": "Rp 24.500.000", "highlight": False},
            {"month": "Oktober 2023", "income": "Rp 42.850.250", "profit": "Rp 27.430.250", "highlight": True},
        ],
        "daily_income": [
            {"date": "12 Okt 2023", "trx": "124", "income": "Rp 1.450.000"},
            {"date": "11 Okt 2023", "trx": "118", "income": "Rp 1.320.000"},
            {"date": "10 Okt 2023", "trx": "132", "income": "Rp 1.510.000"},
            {"date": "09 Okt 2023", "trx": "105", "income": "Rp 1.150.000"},
        ],
        "print_summary": [
            {"label": "Total Pendapatan (Revenue)", "value": "Rp 42.850.250", "tone": "normal"},
            {"label": "Total Biaya Operasional", "value": "Rp 18.500.000", "tone": "danger"},
            {"label": "Total Transaksi", "value": "3.124", "tone": "normal"},
            {"label": "Rata-rata Pendapatan Harian", "value": "Rp 1.382.266", "tone": "normal"},
        ],
        "net_profit": "Rp 24.350.250",
        "daily_details": [
            {"date": "25 Okt 2023", "transactions": "102", "income": "Rp 1.450.000", "cost": "Rp 600.000", "profit": "Rp 850.000"},
            {"date": "26 Okt 2023", "transactions": "98", "income": "Rp 1.320.000", "cost": "Rp 580.000", "profit": "Rp 740.000"},
            {"date": "27 Okt 2023", "transactions": "115", "income": "Rp 1.680.000", "cost": "Rp 620.000", "profit": "Rp 1.060.000"},
            {"date": "28 Okt 2023", "transactions": "140", "income": "Rp 2.100.000", "cost": "Rp 750.000", "profit": "Rp 1.350.000"},
            {"date": "29 Okt 2023", "transactions": "135", "income": "Rp 1.950.000", "cost": "Rp 700.000", "profit": "Rp 1.250.000"},
        ],
        "daily_totals": {"transactions": "590", "income": "Rp 8.500.000", "cost": "Rp 3.250.000", "profit": "Rp 5.250.000"},
        "print_transactions": [
            {"id": "#ORD-3124", "date": "31 Okt 2023", "time": "14:30", "customer": "Bpk. Budi", "method": "QRIS", "total": "Rp 85.000", "status": "Selesai"},
            {"id": "#ORD-3123", "date": "31 Okt 2023", "time": "14:15", "customer": "Ibu Siti", "method": "Cash", "total": "Rp 45.000", "status": "Selesai"},
            {"id": "#ORD-3122", "date": "31 Okt 2023", "time": "13:50", "customer": "Guest", "method": "QRIS", "total": "Rp 120.000", "status": "Selesai"},
            {"id": "#ORD-3121", "date": "31 Okt 2023", "time": "13:20", "customer": "Andi", "method": "Kartu Debit", "total": "Rp 65.000", "status": "Dibatalkan"},
        ],
    }


def parse_menu_price(price_value):
    price = int(price_value)
    if price < MIN_MENU_PRICE:
        raise ValueError
    return price


def get_current_shift():
    current_hour = datetime.now().hour
    if 5 <= current_hour < 12:
        return "Pagi"
    if 12 <= current_hour < 18:
        return "Siang"
    return "Malam"


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
            flash("Email atau password salah.", "error")
            return render_template("login.html", email=email)

        session.clear()
        session["user_id"] = user["id"]
        session["full_name"] = user["full_name"]
        session["name"] = user["full_name"]
        session["username"] = user["full_name"]
        session["role"] = user["role"].lower()
        flash(f"Login sebagai {session['role'].title()} berhasil.", "success")
        return redirect_for_role()

    return render_template("login.html")


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
    return redirect(url_for("login"))


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
        report=build_financial_report(),
    )


@app.route("/owner/reports/print")
@owner_required
def owner_reports_print():
    return render_template(
        "owner_financial_report_print.html",
        owner_name=get_owner_name(),
        active_page="reports",
        report=build_financial_report(),
    )


@app.route("/owner/users")
@owner_required
def owner_users():
    return render_template(
        "dashboard_placeholder.html",
        full_name=session.get("username", "Owner"),
        role="Owner",
        page_title="User Owner",
    )


@app.route("/owner/<path:unused_path>")
@owner_required
def owner_fallback(unused_path):
    return redirect(url_for("owner_menu"))


@app.route("/pos")
@staff_required
def pos():
    init_menu_table()
    db = get_db()
    products = db.execute(
        """
        SELECT id, name, description, price, image, stock, category, code, is_active
        FROM menus
        WHERE is_active = 1
        ORDER BY id DESC
        """
    ).fetchall()
    category_rows = db.execute(
        """
        SELECT DISTINCT category
        FROM menus
        WHERE is_active = 1
        ORDER BY category ASC
        """
    ).fetchall()

    return render_template(
        "pos.html",
        shift=get_current_shift(),
        menu_categories=get_ordered_menu_categories(row["category"] for row in category_rows),
        products=[dict(product) for product in products],
    )


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


def initialize_database():
    with app.app_context():
        init_db()
        init_menu_table()


if __name__ == "__main__":
    initialize_database()
    app.run(debug=True)
