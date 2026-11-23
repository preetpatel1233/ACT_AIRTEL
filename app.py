"""GeoTrack - mobile-first lat/long project tracker (Flask + PostgreSQL / Supabase / SQLite)."""
import io
import os
import random
import sqlite3
from datetime import datetime, timedelta
from xml.sax.saxutils import escape

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

try:
    import psycopg2
    import psycopg2.extras
    PSYCOPG2_AVAILABLE = True
except ImportError:
    PSYCOPG2_AVAILABLE = False

from flask import Flask, g, jsonify, request, send_file, render_template_string, session
from werkzeug.security import generate_password_hash, check_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DATABASE_PATH", os.path.join(BASE, "geotrack.db"))

SUPERADMIN_EMAIL = os.environ.get("SUPERADMIN_EMAIL", "preet31012005@gmail.com")
SUPERADMIN_PASSWORD = os.environ.get("SUPERADMIN_PASSWORD", "Preet@123")

# Supabase / PostgreSQL Configuration
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    sb_host = os.environ.get("SUPABASE_DB_HOST")
    sb_user = os.environ.get("SUPABASE_DB_USER")
    sb_pass = os.environ.get("SUPABASE_DB_PASSWORD")
    sb_port = os.environ.get("SUPABASE_DB_PORT", "6543")
    if sb_host and sb_user and sb_pass:
        DATABASE_URL = f"postgresql://{sb_user}:{sb_pass}@{sb_host}:{sb_port}/postgres"

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "geotrack_superadmin_auth_secret_2026")

# status: 1 = landmark + line, 2 = line only, 3 = end (landmark + line, then chain breaks)
STATUS_LABEL = {1: "Landmark + Line", 2: "Line only", 3: "End"}


# ---------------------------------------------------------------- database compat layer
class CompatCursor:
    def __init__(self, cur, is_postgres=False):
        self.cur = cur
        self.is_postgres = is_postgres
        self.lastrowid = None

    def execute(self, sql, params=None):
        if self.is_postgres:
            sql = sql.replace("?", "%s")
            sql = sql.replace("COLLATE NOCASE", "")
            sql = sql.replace("INSERT OR IGNORE INTO", "INSERT INTO")
            if "ON CONFLICT DO NOTHING" not in sql.upper() and ("project_members" in sql or "users" in sql) and "INSERT INTO" in sql.upper():
                sql = sql.rstrip("; ") + " ON CONFLICT DO NOTHING"

            if sql.strip().upper().startswith("INSERT") and "RETURNING" not in sql.upper() and "projects(" in sql:
                sql = sql.rstrip("; ") + " RETURNING id"
                if params:
                    self.cur.execute(sql, params)
                else:
                    self.cur.execute(sql)
                row = self.cur.fetchone()
                if row:
                    self.lastrowid = row[0] if isinstance(row, (tuple, list)) else row.get("id")
                return self

            if params:
                self.cur.execute(sql, params)
            else:
                self.cur.execute(sql)
            return self
        else:
            if params:
                self.cur.execute(sql, params)
            else:
                self.cur.execute(sql)
            self.lastrowid = getattr(self.cur, "lastrowid", None)
            return self

    def fetchone(self):
        return self.cur.fetchone()

    def fetchall(self):
        return self.cur.fetchall()

    def __iter__(self):
        return iter(self.cur)


class CompatConnection:
    def __init__(self, conn, is_postgres=False):
        self.conn = conn
        self.is_postgres = is_postgres

    def cursor(self):
        if self.is_postgres:
            cur = self.conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        else:
            cur = self.conn.cursor()
        return CompatCursor(cur, is_postgres=self.is_postgres)

    def execute(self, sql, params=None):
        cur = self.cursor()
        cur.execute(sql, params)
        return cur

    def commit(self):
        return self.conn.commit()

    def rollback(self):
        return self.conn.rollback()

    def close(self):
        return self.conn.close()


def get_raw_db():
    if DATABASE_URL and PSYCOPG2_AVAILABLE:
        try:
            pg_url = DATABASE_URL
            if pg_url.startswith("postgres://"):
                pg_url = "postgresql://" + pg_url[11:]
            conn = psycopg2.connect(pg_url, connect_timeout=5)
            conn.autocommit = False
            return CompatConnection(conn, is_postgres=True)
        except Exception as e:
            print(f"  ⚠️ PostgreSQL/Supabase connection failed ({e}). Falling back to local SQLite.")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return CompatConnection(conn, is_postgres=False)


def get_db():
    if "db" not in g:
        g.db = get_raw_db()
    return g.db


@app.teardown_appcontext
def close_db(_):
    db = g.pop("db", None)
    if db:
        db.close()


def generate_share_code(db):
    """Generate a unique 4-digit ID starting with '#', e.g. #4829."""
    for _ in range(5000):
        code = f"#{random.randint(1000, 9999)}"
        existing = db.execute("SELECT 1 FROM projects WHERE share_code = ?", (code,)).fetchone()
        if not existing:
            return code
    return f"#{random.randint(10000, 99999)}"


def init_db():
    db = get_raw_db()
    if db.is_postgres:
        with db.conn.cursor() as cur:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS projects (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                closed_loop INTEGER NOT NULL DEFAULT 0,
                deleted_at TEXT DEFAULT NULL,
                owner_id INTEGER DEFAULT NULL,
                share_code TEXT DEFAULT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS points (
                id SERIAL PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                seq INTEGER NOT NULL,
                latitude REAL NOT NULL,
                longitude REAL NOT NULL,
                lat_text TEXT NOT NULL,
                lng_text TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                status INTEGER NOT NULL CHECK (status IN (1,2,3)),
                remark TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_points_project ON points(project_id, seq);
            CREATE TABLE IF NOT EXISTS loops (
                id SERIAL PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                point_id INTEGER REFERENCES points(id) ON DELETE CASCADE,
                loop_no INTEGER NOT NULL DEFAULT 1,
                name TEXT NOT NULL DEFAULT '',
                latitude REAL NOT NULL,
                longitude REAL NOT NULL,
                lat_text TEXT NOT NULL DEFAULT '',
                lng_text TEXT NOT NULL DEFAULT '',
                red_drum_number TEXT NOT NULL DEFAULT '',
                red_cable_id TEXT NOT NULL DEFAULT '',
                red_start TEXT NOT NULL DEFAULT '',
                red_end TEXT NOT NULL DEFAULT '',
                white_drum_number TEXT NOT NULL DEFAULT '',
                white_cable_id TEXT NOT NULL DEFAULT '',
                white_start TEXT NOT NULL DEFAULT '',
                white_end TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_loops_project ON loops(project_id);
            CREATE INDEX IF NOT EXISTS idx_loops_point ON loops(point_id);
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS project_members (
                id SERIAL PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                joined_at TEXT NOT NULL,
                UNIQUE(project_id, user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_pm_user ON project_members(user_id);
            CREATE INDEX IF NOT EXISTS idx_pm_project ON project_members(project_id);
            """)
        db.commit()
    else:
        db.conn.executescript(
            """
            PRAGMA journal_mode = WAL;
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                closed_loop INTEGER NOT NULL DEFAULT 0,
                deleted_at TEXT DEFAULT NULL,
                owner_id INTEGER DEFAULT NULL,
                share_code TEXT DEFAULT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS points (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                seq INTEGER NOT NULL,
                latitude REAL NOT NULL,
                longitude REAL NOT NULL,
                lat_text TEXT NOT NULL,
                lng_text TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                status INTEGER NOT NULL CHECK (status IN (1,2,3)),
                remark TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_points_project ON points(project_id, seq);
            CREATE TABLE IF NOT EXISTS loops (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                point_id INTEGER REFERENCES points(id) ON DELETE CASCADE,
                loop_no INTEGER NOT NULL DEFAULT 1,
                name TEXT NOT NULL DEFAULT '',
                latitude REAL NOT NULL,
                longitude REAL NOT NULL,
                lat_text TEXT NOT NULL DEFAULT '',
                lng_text TEXT NOT NULL DEFAULT '',
                red_drum_number TEXT NOT NULL DEFAULT '',
                red_cable_id TEXT NOT NULL DEFAULT '',
                red_start TEXT NOT NULL DEFAULT '',
                red_end TEXT NOT NULL DEFAULT '',
                white_drum_number TEXT NOT NULL DEFAULT '',
                white_cable_id TEXT NOT NULL DEFAULT '',
                white_start TEXT NOT NULL DEFAULT '',
                white_end TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_loops_project ON loops(project_id);
            CREATE INDEX IF NOT EXISTS idx_loops_point ON loops(point_id);
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS project_members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                joined_at TEXT NOT NULL,
                UNIQUE(project_id, user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_pm_user ON project_members(user_id);
            CREATE INDEX IF NOT EXISTS idx_pm_project ON project_members(project_id);
            """
        )
    # Add deleted_at column if upgrading from older DB
    try:
        db.execute("SELECT deleted_at FROM projects LIMIT 1")
    except Exception:
        db.rollback()
        try:
            db.execute("ALTER TABLE projects ADD COLUMN deleted_at TEXT DEFAULT NULL")
            db.commit()
        except Exception:
            db.rollback()

    # Add remark column if upgrading from older DB
    try:
        db.execute("SELECT remark FROM points LIMIT 1")
    except Exception:
        db.rollback()
        try:
            db.execute("ALTER TABLE points ADD COLUMN remark TEXT NOT NULL DEFAULT ''")
            db.commit()
        except Exception:
            db.rollback()

    # Add owner_id and share_code columns if upgrading
    try:
        db.execute("SELECT owner_id FROM projects LIMIT 1")
    except Exception:
        db.rollback()
        try:
            db.execute("ALTER TABLE projects ADD COLUMN owner_id INTEGER DEFAULT NULL")
            db.commit()
        except Exception:
            db.rollback()

    try:
        db.execute("SELECT share_code FROM projects LIMIT 1")
    except Exception:
        db.rollback()
        try:
            db.execute("ALTER TABLE projects ADD COLUMN share_code TEXT DEFAULT NULL")
            db.commit()
        except Exception:
            db.rollback()

    # Backfill share_code for any projects without one
    rows_no_code = db.execute("SELECT id FROM projects WHERE share_code IS NULL OR share_code = ''").fetchall()
    for (pid_val,) in rows_no_code:
        code_val = generate_share_code(db)
        db.execute("UPDATE projects SET share_code = ? WHERE id = ?", (code_val, pid_val))
    db.commit()

    # Seed or update Superadmin account
    admin_email = SUPERADMIN_EMAIL
    admin_pass = SUPERADMIN_PASSWORD
    existing_admin = db.execute("SELECT id FROM users WHERE LOWER(email) = LOWER(?)", (admin_email,)).fetchone()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not existing_admin:
        db.execute(
            "INSERT INTO users (email, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
            (admin_email, generate_password_hash(admin_pass), "superadmin", now_str)
        )
    else:
        db.execute(
            "UPDATE users SET password_hash = ?, role = 'superadmin' WHERE id = ?",
            (generate_password_hash(admin_pass), existing_admin[0])
        )
    db.commit()

    # Ensure existing projects belong to superadmin so they remain accessible
    admin_row = db.execute("SELECT id FROM users WHERE LOWER(email) = LOWER(?)", (admin_email,)).fetchone()
    if admin_row:
        admin_id = admin_row[0]
        db.execute("UPDATE projects SET owner_id = ? WHERE owner_id IS NULL", (admin_id,))
        db.execute("""
            INSERT INTO project_members (project_id, user_id, joined_at)
            SELECT id, ?, ? FROM projects
            ON CONFLICT DO NOTHING
        """ if db.is_postgres else """
            INSERT OR IGNORE INTO project_members (project_id, user_id, joined_at)
            SELECT id, ?, ? FROM projects
        """, (admin_id, now_str))
        db.commit()

    db.close()



def purge_expired():
    """Permanently delete projects that have been in the recycle bin for > 30 days."""
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA foreign_keys = ON")
    cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    deleted = db.execute(
        "SELECT id, name FROM projects WHERE deleted_at IS NOT NULL AND deleted_at < ?", (cutoff,)
    ).fetchall()
    if deleted:
        db.execute("DELETE FROM projects WHERE deleted_at IS NOT NULL AND deleted_at < ?", (cutoff,))
        db.commit()
        for row in deleted:
            print(f"  🗑 Permanently deleted project '{row[1]}' (expired from recycle bin)")
    db.close()


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def project_points(pid):
    rows = get_db().execute(
        """SELECT p.*, COUNT(l.id) AS loop_count
           FROM points p
           LEFT JOIN loops l ON l.point_id = p.id
           WHERE p.project_id = ?
           GROUP BY p.id
           ORDER BY p.seq""",
        (pid,)
    ).fetchall()
    return [dict(r) for r in rows]


def build_segments(points, closed_loop=False):
    """Return list of polylines (each a list of [lat,lng]).
    A line joins each point to the previous one unless the previous was 'End'."""
    segs, cur = [], []
    for p in points:
        cur.append([p["latitude"], p["longitude"]])
        if p["status"] == 3:
            segs.append(cur)
            cur = []
    if cur:
        segs.append(cur)
    segs = [s for s in segs if len(s) > 1]
    if closed_loop and points:
        segs.append([[points[-1]["latitude"], points[-1]["longitude"]],
                     [points[0]["latitude"], points[0]["longitude"]]])
    return segs


# ---------------------------------------------------------------- pages
@app.route("/")
def index():
    return send_file(os.path.join(BASE, "index.html"), mimetype="text/html")


# ---------------------------------------------------------------- auth & users
@app.post("/api/auth/login")
def auth_login():
    data = request.json or {}
    email = (data.get("email") or "").strip()
    password = (data.get("password") or "").strip()

    if not email or not password:
        return jsonify(error="Email and password are required"), 400

    db = get_db()
    user = db.execute("SELECT * FROM users WHERE LOWER(email) = LOWER(?)", (email,)).fetchone()
    if not user and "@" not in email:
        user = db.execute("SELECT * FROM users WHERE LOWER(email) = LOWER(?)", (email + "@gmail.com",)).fetchone()
    elif not user and "@" in email:
        user = db.execute("SELECT * FROM users WHERE LOWER(email) = LOWER(?)", (email.split("@")[0],)).fetchone()
    if not user:
        return jsonify(error="Invalid email or password"), 401

    pwd_hash = user["password_hash"]
    valid = False
    try:
        valid = check_password_hash(pwd_hash, password)
    except Exception:
        pass
    if not valid and pwd_hash == password:
        valid = True

    if not valid:
        return jsonify(error="Invalid email or password"), 401

    session["user_id"] = user["id"]
    session["role"] = user["role"]
    session["email"] = user["email"]

    return jsonify(
        ok=True,
        user={
            "id": user["id"],
            "email": user["email"],
            "role": user["role"]
        }
    )


@app.get("/api/auth/me")
def auth_me():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify(authenticated=False), 200
    db = get_db()
    user = db.execute("SELECT id, email, role, created_at FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        session.clear()
        return jsonify(authenticated=False), 200
    return jsonify(authenticated=True, user=dict(user))


@app.post("/api/auth/logout")
def auth_logout():
    session.clear()
    return jsonify(ok=True)


@app.get("/api/admin/users")
def admin_list_users():
    role = session.get("role")
    if role != "superadmin":
        return jsonify(error="Access denied: Superadmin only"), 403

    db = get_db()
    rows = db.execute("SELECT id, email, role, created_at FROM users ORDER BY id").fetchall()
    return jsonify(users=[dict(r) for r in rows])


@app.post("/api/admin/users")
def admin_create_user():
    role = session.get("role")
    if role != "superadmin":
        return jsonify(error="Access denied: Superadmin only"), 403

    data = request.json or {}
    email = (data.get("email") or "").strip()
    password = (data.get("password") or "").strip()
    user_role = (data.get("role") or "user").strip().lower()
    if user_role not in ("user", "superadmin", "viewer", "loop"):
        user_role = "user"

    if not email or not password:
        return jsonify(error="Email and password are required"), 400
    if len(password) < 4:
        return jsonify(error="Password must be at least 4 characters"), 400

    db = get_db()
    existing = db.execute("SELECT id FROM users WHERE LOWER(email) = LOWER(?)", (email,)).fetchone()
    if existing:
        return jsonify(error="A user with this email/account already exists"), 409

    db.execute(
        "INSERT INTO users (email, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
        (email, generate_password_hash(password), user_role, now())
    )
    db.commit()
    return jsonify(ok=True, message=f"User '{email}' created successfully")


@app.delete("/api/admin/users/<int:uid>")
def admin_delete_user(uid):
    role = session.get("role")
    if role != "superadmin":
        return jsonify(error="Access denied: Superadmin only"), 403

    db = get_db()
    u = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    if not u:
        return jsonify(error="User not found"), 404

    if u["email"].lower() == SUPERADMIN_EMAIL.lower():
        return jsonify(error="Cannot delete primary superadmin"), 400

    db.execute("DELETE FROM users WHERE id = ?", (uid,))
    db.commit()
    return jsonify(ok=True)


# ---------------------------------------------------------------- API
def check_write_permission(allow_loop=False):
    role = session.get("role")
    if role == "viewer":
        return jsonify(error="Viewer role is read-only. Action not permitted."), 403
    if role == "loop" and not allow_loop:
        return jsonify(error="Loop role can only edit loops. Action not permitted."), 403
    return None


def can_access_project(pid, user_id):
    """Check if user owns project or joined via 4-digit ID."""
    if not user_id:
        return False
    db = get_db()
    row = db.execute(
        """SELECT 1 FROM projects p
           LEFT JOIN project_members pm ON p.id = pm.project_id
           WHERE p.id = ? AND p.deleted_at IS NULL AND (p.owner_id = ? OR pm.user_id = ?)""",
        (pid, user_id, user_id)
    ).fetchone()
    return bool(row)


@app.get("/api/projects")
def list_projects():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify([])
    db = get_db()

    # Strictly return only projects owned by this user or joined via 4-digit ID
    rows = db.execute(
        """SELECT p.id, p.name, p.created_at, p.closed_loop, p.share_code, p.owner_id,
                  (SELECT COUNT(*) FROM points WHERE project_id=p.id) AS count
           FROM projects p
           LEFT JOIN project_members pm ON p.id = pm.project_id
           WHERE p.deleted_at IS NULL AND (p.owner_id = ? OR pm.user_id = ?)
           GROUP BY p.id
           ORDER BY p.id DESC""",
        (user_id, user_id)
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.post("/api/projects")
def create_project():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify(error="Please sign in to create a project"), 401
    name = (request.json.get("name") or "").strip()
    if not name:
        return jsonify(error="Project name required"), 400
    db = get_db()
    share_code = generate_share_code(db)
    t = now()
    try:
        cur = db.execute(
            "INSERT INTO projects(name, created_at, owner_id, share_code) VALUES(?,?,?,?)",
            (name, t, user_id, share_code)
        )
        pid = cur.lastrowid
        db.execute(
            "INSERT OR IGNORE INTO project_members(project_id, user_id, joined_at) VALUES(?,?,?)",
            (pid, user_id, t)
        )
        db.commit()
    except sqlite3.IntegrityError:
        return jsonify(error="A project with this name already exists"), 409
    return jsonify(id=pid, name=name, share_code=share_code)


@app.post("/api/projects/join")
def join_project():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify(error="Please sign in to add a project"), 401

    code = (request.json.get("code") or "").strip()
    if not code:
        return jsonify(error="Please enter the 4-digit project ID (e.g. #1234)"), 400

    if not code.startswith("#"):
        code = f"#{code}"

    db = get_db()
    proj = db.execute(
        "SELECT * FROM projects WHERE LOWER(share_code) = LOWER(?) AND deleted_at IS NULL",
        (code,)
    ).fetchone()

    if not proj:
        return jsonify(error=f"No active project found with ID '{code}'. Please check the 4-digit code and try again."), 404

    # Add to project_members
    db.execute(
        "INSERT OR IGNORE INTO project_members(project_id, user_id, joined_at) VALUES(?,?,?)",
        (proj["id"], user_id, now())
    )
    db.commit()

    pts = project_points(proj["id"])
    return jsonify(
        ok=True,
        message=f"Project '{proj['name']}' ({proj['share_code']}) added successfully!",
        project={
            "id": proj["id"],
            "name": proj["name"],
            "share_code": proj["share_code"],
            "closed_loop": proj["closed_loop"],
            "count": len(pts)
        }
    )


@app.delete("/api/projects/<int:pid>")
def delete_project(pid):
    """Soft-delete for owner / superadmin, or remove from user list for members."""
    err = check_write_permission()
    if err:
        return err
    user_id = session.get("user_id")
    role = session.get("role")
    db = get_db()
    proj = db.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    if not proj:
        return jsonify(error="Project not found"), 404

    # If superadmin or owner: move to recycle bin
    if role == "superadmin" or (user_id and proj["owner_id"] == user_id):
        db.execute("UPDATE projects SET deleted_at=? WHERE id=? AND deleted_at IS NULL", (now(), pid))
        db.commit()
        return jsonify(ok=True, action="deleted")
    else:
        # Non-owner member: just remove from user's project list
        db.execute("DELETE FROM project_members WHERE project_id=? AND user_id=?", (pid, user_id))
        db.commit()
        return jsonify(ok=True, action="removed")


@app.get("/api/recycle-bin")
def list_recycle_bin():
    """List projects in the recycle bin with days remaining."""
    rows = get_db().execute(
        """SELECT p.id, p.name, p.created_at, p.deleted_at,
                  (SELECT COUNT(*) FROM points WHERE project_id=p.id) AS count
           FROM projects p WHERE p.deleted_at IS NOT NULL ORDER BY p.deleted_at DESC"""
    ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        deleted_dt = datetime.strptime(d["deleted_at"], "%Y-%m-%d %H:%M:%S")
        expire_dt = deleted_dt + timedelta(days=30)
        days_left = max(0, (expire_dt - datetime.now()).days)
        d["days_left"] = days_left
        result.append(d)
    return jsonify(result)


@app.post("/api/recycle-bin/<int:pid>/restore")
def restore_project(pid):
    """Restore a project from the recycle bin."""
    err = check_write_permission()
    if err:
        return err
    db = get_db()
    db.execute("UPDATE projects SET deleted_at=NULL WHERE id=?", (pid,))
    db.commit()
    return jsonify(ok=True)


@app.delete("/api/recycle-bin/<int:pid>")
def permanent_delete(pid):
    """Permanently delete a project and all its points."""
    err = check_write_permission()
    if err:
        return err
    db = get_db()
    db.execute("DELETE FROM projects WHERE id=? AND deleted_at IS NOT NULL", (pid,))
    db.commit()
    return jsonify(ok=True)


@app.get("/api/projects/<int:pid>")
def get_project(pid):
    user_id = session.get("user_id")
    if not can_access_project(pid, user_id):
        return jsonify(error="Project not found or not added to your account. Enter the 4-digit Project ID to access it."), 403
    proj = get_db().execute("SELECT * FROM projects WHERE id=? AND deleted_at IS NULL", (pid,)).fetchone()
    if not proj:
        return jsonify(error="Not found"), 404
    pts = project_points(pid)
    return jsonify(
        project=dict(proj),
        points=pts,
        segments=build_segments(pts, bool(proj["closed_loop"])),
    )


@app.post("/api/projects/<int:pid>/points")
def add_point(pid):
    err = check_write_permission()
    if err:
        return err
    user_id = session.get("user_id")
    if not can_access_project(pid, user_id):
        return jsonify(error="Access denied: Enter the 4-digit Project ID to join this project first."), 403
    data = request.json
    items = data if isinstance(data, list) else [data]
    if not items:
        return jsonify(error="No point data"), 400

    db = get_db()
    if not db.execute("SELECT 1 FROM projects WHERE id=?", (pid,)).fetchone():
        return jsonify(error="Project not found"), 404

    cur_seq = db.execute(
        "SELECT COALESCE(MAX(seq),0) FROM points WHERE project_id=?", (pid,)
    ).fetchone()[0]

    for d in items:
        d = d or {}
        try:
            lat_text = str(d["latitude"]).strip()
            lng_text = str(d["longitude"]).strip()
            lat, lng = float(lat_text), float(lng_text)
            status = int(d["status"])
        except (KeyError, ValueError, TypeError):
            return jsonify(error="Latitude, longitude and status are required"), 400
        if not (-90 <= lat <= 90 and -180 <= lng <= 180) or status not in (1, 2, 3):
            return jsonify(error="Invalid latitude / longitude / status"), 400

        cur_seq += 1
        remark = (d.get("remark") or "").strip()
        db.execute(
            """INSERT INTO points(project_id, seq, latitude, longitude, lat_text, lng_text,
                                  name, status, remark, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (pid, cur_seq, lat, lng, lat_text, lng_text, (d.get("name") or "").strip(), status, remark, now()),
        )
    db.commit()
    return jsonify(ok=True, seq=cur_seq)


@app.put("/api/points/<int:point_id>")
def update_point(point_id):
    err = check_write_permission()
    if err:
        return err
    user_id = session.get("user_id")
    db = get_db()
    pt = db.execute("SELECT * FROM points WHERE id=?", (point_id,)).fetchone()
    if not pt:
        return jsonify(error="Point not found"), 404
    if not can_access_project(pt["project_id"], user_id):
        return jsonify(error="Access denied: Enter the 4-digit Project ID first."), 403

    data = request.json or {}
    try:
        lat_text = str(data["latitude"]).strip().replace(",", ".")
        lng_text = str(data["longitude"]).strip().replace(",", ".")
        lat, lng = float(lat_text), float(lng_text)
        status = int(data["status"])
    except (KeyError, ValueError, TypeError):
        return jsonify(error="Latitude, longitude and status are required"), 400

    if not (-90 <= lat <= 90 and -180 <= lng <= 180) or status not in (1, 2, 3):
        return jsonify(error="Invalid latitude / longitude / status"), 400

    name = (data.get("name") or "").strip()
    remark = (data.get("remark") or "").strip()

    db.execute(
        """UPDATE points 
           SET latitude=?, longitude=?, lat_text=?, lng_text=?, name=?, status=?, remark=?
           WHERE id=?""",
        (lat, lng, lat_text, lng_text, name, status, remark, point_id),
    )
    # Keep loop records in sync if coordinates or name changed
    db.execute(
        """UPDATE loops 
           SET latitude=?, longitude=?, lat_text=?, lng_text=?, name=?
           WHERE point_id=?""",
        (lat, lng, lat_text, lng_text, name, point_id),
    )
    db.commit()
    return jsonify(ok=True)


@app.delete("/api/points/<int:point_id>")
def delete_point(point_id):
    err = check_write_permission()
    if err:
        return err
    user_id = session.get("user_id")
    db = get_db()
    pt = db.execute("SELECT project_id FROM points WHERE id=?", (point_id,)).fetchone()
    if not pt or not can_access_project(pt["project_id"], user_id):
        return jsonify(error="Access denied: Enter the 4-digit Project ID first."), 403
    db.execute("DELETE FROM points WHERE id=?", (point_id,))
    db.commit()
    return jsonify(ok=True)


@app.post("/api/projects/<int:pid>/loop")
def set_loop(pid):
    err = check_write_permission()
    if err:
        return err
    user_id = session.get("user_id")
    if not can_access_project(pid, user_id):
        return jsonify(error="Access denied: Enter the 4-digit Project ID first."), 403
    closed = 1 if request.json.get("closed") else 0
    db = get_db()
    db.execute("UPDATE projects SET closed_loop=? WHERE id=?", (closed, pid))
    db.commit()
    return jsonify(ok=True, closed=closed)


# ---------------------------------------------------------------- loop details
@app.get("/api/projects/<int:pid>/points/<int:pt_id>/loops")
def get_point_loops(pid, pt_id):
    user_id = session.get("user_id")
    if not can_access_project(pid, user_id):
        return jsonify(error="Access denied: Enter the 4-digit Project ID first."), 403
    rows = get_db().execute(
        "SELECT * FROM loops WHERE project_id=? AND point_id=? ORDER BY loop_no",
        (pid, pt_id)
    ).fetchall()
    return jsonify(loops=[dict(r) for r in rows])


@app.post("/api/projects/<int:pid>/points/<int:pt_id>/loops")
def save_point_loops(pid, pt_id):
    err = check_write_permission(allow_loop=True)
    if err:
        return err
    user_id = session.get("user_id")
    if not can_access_project(pid, user_id):
        return jsonify(error="Access denied: Enter the 4-digit Project ID first."), 403
    data = request.json or {}
    loops_data = data.get("loops", [])
    db = get_db()
    pt = db.execute("SELECT * FROM points WHERE id=? AND project_id=?", (pt_id, pid)).fetchone()
    if not pt:
        return jsonify(error="Point not found"), 404

    # Replace existing loops for this point
    db.execute("DELETE FROM loops WHERE project_id=? AND point_id=?", (pid, pt_id))
    t = now()
    for i, lp in enumerate(loops_data, 1):
        db.execute(
            """INSERT INTO loops (
                project_id, point_id, loop_no, name, latitude, longitude, lat_text, lng_text,
                red_drum_number, red_cable_id, red_start, red_end,
                white_drum_number, white_cable_id, white_start, white_end, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                pid, pt_id, i,
                pt["name"] or f"#{pt['seq']}",
                pt["latitude"], pt["longitude"],
                pt["lat_text"], pt["lng_text"],
                str(lp.get("red_drum_number") or "").strip(),
                str(lp.get("red_cable_id") or "").strip(),
                str(lp.get("red_start") or "").strip(),
                str(lp.get("red_end") or "").strip(),
                str(lp.get("white_drum_number") or "").strip(),
                str(lp.get("white_cable_id") or "").strip(),
                str(lp.get("white_start") or "").strip(),
                str(lp.get("white_end") or "").strip(),
                t
            )
        )
    db.commit()
    return jsonify(ok=True, count=len(loops_data))


# ---------------------------------------------------------------- exports
def _load(pid):
    user_id = session.get("user_id")
    if not can_access_project(pid, user_id):
        return None, None
    proj = get_db().execute("SELECT * FROM projects WHERE id=? AND deleted_at IS NULL", (pid,)).fetchone()
    if not proj:
        return None, None
    return dict(proj), project_points(pid)


@app.get("/api/projects/<int:pid>/export/excel")
def export_excel(pid):
    from openpyxl import Workbook
    from openpyxl.styles import Font

    proj, pts = _load(pid)
    if not proj:
        return "Not found", 404
    wb = Workbook()
    ws = wb.active
    ws.title = "Points"
    ws.append(["#", "Name", "Latitude", "Longitude", "Status", "Remark", "Saved at"])
    for c in ws[1]:
        c.font = Font(bold=True)
    for p in pts:
        ws.append([p["seq"], p["name"], p["lat_text"], p["lng_text"],
                   STATUS_LABEL[p["status"]], p.get("remark") or "", p["created_at"]])
    for col, w in zip("ABCDEFG", (6, 26, 16, 16, 18, 20, 20)):
        ws.column_dimensions[col].width = w
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=f"{proj['name']}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.get("/api/projects/<int:pid>/export/loops-excel")
def export_loops_excel(pid):
    user_id = session.get("user_id")
    if not can_access_project(pid, user_id):
        return "Access denied: Enter the 4-digit project ID first.", 403
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment

    proj = get_db().execute("SELECT * FROM projects WHERE id=? AND deleted_at IS NULL", (pid,)).fetchone()
    if not proj:
        return "Not found", 404

    rows = get_db().execute(
        """SELECT l.* FROM loops l
           JOIN points p ON l.point_id = p.id
           WHERE l.project_id = ?
           ORDER BY p.seq, l.loop_no""",
        (pid,)
    ).fetchall()

    if not rows:
        rows = get_db().execute(
            "SELECT * FROM loops WHERE project_id = ? ORDER BY id", (pid,)
        ).fetchall()

    wb = Workbook()
    ws = wb.active
    ws.title = "Loop Details"

    # Header columns matching exact user image
    headers = [
        "Name", "latitude", "longitude",
        "Red-Drum number", "Red-Cable ID", "Red-Start", "Red-End",
        "White-Drum number", "White-Cable ID", "White-Start", "White-End"
    ]
    ws.append(headers)

    header_font = Font(name="Calibri", size=11, bold=True)
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    for r in rows:
        ws.append([
            r["name"],
            r["lat_text"] or str(r["latitude"]),
            r["lng_text"] or str(r["longitude"]),
            r["red_drum_number"],
            r["red_cable_id"],
            r["red_start"],
            r["red_end"],
            r["white_drum_number"],
            r["white_cable_id"],
            r["white_start"],
            r["white_end"]
        ])

    for col, w in zip("ABCDEFGHIJK", (18, 14, 14, 18, 18, 14, 14, 18, 18, 14, 14)):
        ws.column_dimensions[col].width = w

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=f"{proj['name']}_Loops.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.get("/api/projects/<int:pid>/export/both-excel")
def export_both_excel(pid):
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment

    proj, pts = _load(pid)
    if not proj:
        return "Not found", 404

    wb = Workbook()

    # Sheet 1: Points
    ws1 = wb.active
    ws1.title = "Points"
    ws1.append(["#", "Name", "Latitude", "Longitude", "Status", "Remark", "Saved at"])
    for c in ws1[1]:
        c.font = Font(bold=True)
    for p in pts:
        ws1.append([p["seq"], p["name"], p["lat_text"], p["lng_text"],
                    STATUS_LABEL[p["status"]], p.get("remark") or "", p["created_at"]])
    for col, w in zip("ABCDEFG", (6, 26, 16, 16, 18, 20, 20)):
        ws1.column_dimensions[col].width = w

    # Sheet 2: Loop Details
    ws2 = wb.create_sheet(title="Loop Details")
    headers = [
        "Name", "latitude", "longitude",
        "Red-Drum number", "Red-Cable ID", "Red-Start", "Red-End",
        "White-Drum number", "White-Cable ID", "White-Start", "White-End"
    ]
    ws2.append(headers)
    for col_idx in range(1, len(headers) + 1):
        cell = ws2.cell(row=1, column=col_idx)
        cell.font = Font(name="Calibri", size=11, bold=True)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    rows = get_db().execute(
        """SELECT l.* FROM loops l
           JOIN points p ON l.point_id = p.id
           WHERE l.project_id = ?
           ORDER BY p.seq, l.loop_no""",
        (pid,)
    ).fetchall()
    if not rows:
        rows = get_db().execute(
            "SELECT * FROM loops WHERE project_id = ? ORDER BY id", (pid,)
        ).fetchall()

    for r in rows:
        ws2.append([
            r["name"],
            r["lat_text"] or str(r["latitude"]),
            r["lng_text"] or str(r["longitude"]),
            r["red_drum_number"],
            r["red_cable_id"],
            r["red_start"],
            r["red_end"],
            r["white_drum_number"],
            r["white_cable_id"],
            r["white_start"],
            r["white_end"]
        ])
    for col, w in zip("ABCDEFGHIJK", (18, 14, 14, 18, 18, 14, 14, 18, 18, 14, 14)):
        ws2.column_dimensions[col].width = w

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=f"{proj['name']}_Complete.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def _loop_pdf_table(pid):
    from reportlab.platypus import Table, TableStyle
    from reportlab.lib import colors

    rows = get_db().execute(
        """SELECT l.* FROM loops l
           JOIN points p ON l.point_id = p.id
           WHERE l.project_id = ?
           ORDER BY p.seq, l.loop_no""",
        (pid,)
    ).fetchall()
    if not rows:
        rows = get_db().execute(
            "SELECT * FROM loops WHERE project_id = ? ORDER BY id", (pid,)
        ).fetchall()

    table_data = [[
        "Name", "Latitude", "Longitude",
        "Red Drum", "Red Cable", "Red (Start - End)",
        "White Drum", "White Cable", "White (Start - End)"
    ]]
    for r in rows:
        red_se = f"{r['red_start']} - {r['red_end']}" if (r['red_start'] or r['red_end']) else ""
        white_se = f"{r['white_start']} - {r['white_end']}" if (r['white_start'] or r['white_end']) else ""
        table_data.append([
            r["name"],
            str(r["lat_text"] or r["latitude"]),
            str(r["lng_text"] or r["longitude"]),
            r["red_drum_number"],
            r["red_cable_id"],
            red_se,
            r["white_drum_number"],
            r["white_cable_id"],
            white_se
        ])

    if len(table_data) == 1:
        table_data.append(["(No loops recorded yet)", "", "", "", "", "", "", "", ""])

    t = Table(table_data, repeatRows=1, colWidths=[90, 80, 80, 65, 65, 95, 65, 65, 95])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#c62828")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.whitesmoke]),
    ]))
    return t


@app.get("/api/projects/<int:pid>/export/loops-pdf")
def export_loops_pdf(pid):
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer

    proj, pts = _load(pid)
    if not proj:
        return "Not found", 404
    styles = getSampleStyleSheet()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=30, rightMargin=30, topMargin=30, bottomMargin=30)
    els = [
        Paragraph(f"Loop Records: {escape(proj['name'])}", styles["Title"]),
        Paragraph(f"Created {proj['created_at']}", styles["Normal"]),
        Spacer(1, 14),
        _loop_pdf_table(pid)
    ]
    doc.build(els)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=f"{proj['name']}_Loops.pdf",
                     mimetype="application/pdf")


@app.get("/api/projects/<int:pid>/export/both-pdf")
def export_both_pdf(pid):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak

    proj, pts = _load(pid)
    if not proj:
        return "Not found", 404
    styles = getSampleStyleSheet()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=30, rightMargin=30, topMargin=30, bottomMargin=30)
    els = [
        Paragraph(f"Project Survey: {escape(proj['name'])}", styles["Title"]),
        Paragraph(f"Created {proj['created_at']} &nbsp;|&nbsp; {len(pts)} points", styles["Normal"]),
        Spacer(1, 12),
    ]

    # Points Table
    pt_rows = [["#", "Name", "Latitude", "Longitude", "Status", "Remark", "Saved at"]]
    for p in pts:
        pt_rows.append([p["seq"], p["name"], p["lat_text"], p["lng_text"],
                        STATUS_LABEL[p["status"]], p.get("remark") or "", p["created_at"]])
    pt_table = Table(pt_rows, repeatRows=1, colWidths=[30, 150, 100, 100, 110, 120, 120])
    pt_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1565c0")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.whitesmoke]),
    ]))
    els.append(pt_table)

    # Page Break for Loop details
    els.append(PageBreak())
    els.append(Paragraph(f"Loop Records — {escape(proj['name'])}", styles["Heading1"]))
    els.append(Spacer(1, 10))
    els.append(_loop_pdf_table(pid))

    doc.build(els)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=f"{proj['name']}_Complete.pdf",
                     mimetype="application/pdf")


@app.get("/api/projects/<int:pid>/export/pdf")
def export_pdf(pid):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.graphics.shapes import Drawing, Line, Circle, String, Rect

    proj, pts = _load(pid)
    if not proj:
        return "Not found", 404
    styles = getSampleStyleSheet()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=30, rightMargin=30, topMargin=30)
    els = [Paragraph(f"Project: {escape(proj['name'])}", styles["Title"]),
           Paragraph(f"Created {proj['created_at']} &nbsp;|&nbsp; {len(pts)} points", styles["Normal"]),
           Spacer(1, 12)]

    # simple vector plot of the route
    W, H, pad = 520, 300, 20
    d = Drawing(W, H)
    d.add(Rect(0, 0, W, H, strokeColor=colors.grey, fillColor=colors.whitesmoke))
    if pts:
        lats = [p["latitude"] for p in pts]
        lngs = [p["longitude"] for p in pts]
        mnla, mxla, mnlo, mxlo = min(lats), max(lats), min(lngs), max(lngs)
        dla = (mxla - mnla) or 1e-6
        dlo = (mxlo - mnlo) or 1e-6
        sc = min((W - 2 * pad) / dlo, (H - 2 * pad) / dla)
        ox = (W - dlo * sc) / 2
        oy = (H - dla * sc) / 2

        def xy(la, lo):
            return ox + (lo - mnlo) * sc, oy + (la - mnla) * sc

        for seg in build_segments(pts, bool(proj["closed_loop"])):
            for a, b in zip(seg, seg[1:]):
                x1, y1 = xy(*a)
                x2, y2 = xy(*b)
                d.add(Line(x1, y1, x2, y2, strokeColor=colors.HexColor("#1565c0"), strokeWidth=2))
        for p in pts:
            if p["status"] in (1, 3):
                x, y = xy(p["latitude"], p["longitude"])
                d.add(Circle(x, y, 4, fillColor=colors.red, strokeColor=colors.white))
                if p["name"]:
                    d.add(String(x + 6, y + 4, p["name"][:20], fontSize=7))
    els += [d, Spacer(1, 14)]

    rows = [["#", "Name", "Latitude", "Longitude", "Status", "Remark"]]
    for p in pts:
        rows.append([p["seq"], p["name"], p["lat_text"], p["lng_text"], STATUS_LABEL[p["status"]], p.get("remark") or ""])
    t = Table(rows, repeatRows=1, colWidths=[25, 120, 85, 85, 90, 115])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1565c0")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.whitesmoke]),
    ]))
    els.append(t)
    doc.build(els)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=f"{proj['name']}.pdf",
                     mimetype="application/pdf")


@app.get("/api/projects/<int:pid>/export/kml")
def export_kml(pid):
    """KML opens directly in Google Earth."""
    proj, pts = _load(pid)
    if not proj:
        return "Not found", 404
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>',
           f"<name>{escape(proj['name'])}</name>",
           '<Style id="l"><LineStyle><color>ffc06515</color><width>3</width></LineStyle></Style>']
    for p in pts:
        if p["status"] in (1, 3):
            desc = STATUS_LABEL[p['status']]
            if p.get('remark'):
                desc += f" — {escape(p['remark'])}"
            out.append(
                f"<Placemark><name>{escape(p['name'] or '#%d' % p['seq'])}</name>"
                f"<description>{desc}</description>"
                f"<Point><coordinates>{p['lng_text']},{p['lat_text']},0</coordinates></Point></Placemark>")
    for i, seg in enumerate(build_segments(pts, bool(proj["closed_loop"])), 1):
        coords = " ".join(f"{lo},{la},0" for la, lo in seg)
        out.append(f"<Placemark><name>Line {i}</name><styleUrl>#l</styleUrl>"
                   f"<LineString><tessellate>1</tessellate><coordinates>{coords}</coordinates>"
                   f"</LineString></Placemark>")
    out.append("</Document></kml>")
    return send_file(io.BytesIO("\n".join(out).encode("utf-8")), as_attachment=True,
                     download_name=f"{proj['name']}.kml",
                     mimetype="application/vnd.google-earth.kml+xml")


if __name__ == "__main__":
    init_db()
    purge_expired()
    import socket
    try:
        ip = socket.gethostbyname(socket.gethostname())
    except OSError:
        ip = "<your-pc-ip>"

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "True").lower() in ("true", "1", "yes")

    print(f"\n  ✅ GeoTrack is running!")
    print(f"  On PC  :  http://localhost:{port}")
    print(f"  On Phone:  http://{ip}:{port}")
    print()
    print(f"  📱 To enable GPS on your phone (one-time setup):")
    print(f"     1. Open Chrome on phone")
    print(f"     2. Go to:  chrome://flags/#unsafely-treat-insecure-origin-as-secure")
    print(f"     3. Paste:  http://{ip}:{port}")
    print(f"     4. Set to 'Enabled', tap 'Relaunch'")
    print(f"     5. Now open http://{ip}:{port} — GPS will work!\n")

    app.run(host=host, port=port, debug=debug)
else:
    init_db()
    purge_expired()
