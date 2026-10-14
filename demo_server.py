#!/usr/bin/env python3
"""
Eswatini Handball Association (EHA) – Registration System v1.3
SQLite · Roles · Transfers by license · Photos · Stats · Notifications
"""
from flask import Flask, request, jsonify, render_template_string, session, send_from_directory, send_file
from datetime import datetime, timedelta
from functools import wraps
from werkzeug.utils import secure_filename
import sqlite3, random, os, hashlib, socket, uuid, io
try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas as pdf_canvas
    from reportlab.lib.utils import ImageReader
    HAS_REPORTLAB = True
except ImportError:
    HAS_REPORTLAB = False

app = Flask(__name__, static_folder="static")
app.secret_key = os.environ.get("EHA_SECRET_KEY", "eha-handball-2026-change-in-production")
BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "handball.db")
UPLOAD_DIR = os.path.join(BASE, "static", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
SEASON = 2026
ALLOWED_EXT = {"png", "jpg", "jpeg", "gif", "webp"}

def hash_pw(p): return hashlib.sha256(p.encode()).hexdigest()

def get_conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c

def q(sql, params=None, fetch=True):
    conn = get_conn()
    try:
        cur = conn.execute(sql, params or ())
        if fetch: return [dict(r) for r in cur.fetchall()]
        conn.commit(); return cur.rowcount
    finally: conn.close()

def q1(sql, params=None):
    rows = q(sql, params); return rows[0] if rows else None

def get_setting(k, d=None):
    r = q1("SELECT value FROM settings WHERE key=?", (k,)); return r["value"] if r else d

def set_setting(k, v):
    q("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, str(v)), fetch=False)

def fees():
    return {"team": float(get_setting("team_fee","150")), "player": float(get_setting("player_fee","50")),
            "transfer": float(get_setting("transfer_fee","50"))}

def season_start_year():
    """Canonical year key stored in DB (start of season)."""
    try:
        return int(get_setting("season_start_year", str(SEASON)))
    except Exception:
        return SEASON

def season_label():
    """Display form e.g. 2026/27"""
    y = season_start_year()
    custom = get_setting("season_label", "")
    if custom:
        return custom
    return f"{y}/{str(y+1)[-2:]}"

def gen_license():
    y = season_start_year()
    return f"HAE-{str(y)[-2:]}-{random.randint(100000,999999)}"
def gen_ref(): return f"HAE-TX-{int(datetime.now().timestamp()*1000)}-{random.randint(100,999)}"

def current_user():
    uid = session.get("user_id")
    if not uid: return None
    return q1("SELECT user_id,username,full_name,role,team_id FROM users WHERE user_id=? AND is_active=1", (uid,))

def login_required(f):
    @wraps(f)
    def d(*a,**k):
        if not current_user(): return jsonify(success=False,error="Login required"),401
        return f(*a,**k)
    return d

def role_required(*roles):
    def deco(f):
        @wraps(f)
        def d(*a,**k):
            u=current_user()
            if not u: return jsonify(success=False,error="Login required"),401
            if u["role"] not in roles: return jsonify(success=False,error="Access denied"),403
            return f(*a,**k)
        return d
    return deco

def save_photo(file_storage):
    if not file_storage or not file_storage.filename: return None
    ext = file_storage.filename.rsplit(".",1)[-1].lower()
    if ext not in ALLOWED_EXT: return None
    name = f"{uuid.uuid4().hex}.{ext}"
    path = os.path.join(UPLOAD_DIR, name)
    file_storage.save(path)
    return f"/static/uploads/{name}"

def notify(user_id, title, body, link_type=None, link_id=None):
    if not user_id: return
    q("INSERT INTO notifications(user_id,title,body,link_type,link_id) VALUES(?,?,?,?,?)",
      (user_id, title, body, link_type, link_id), fetch=False)

def coach_user_for_team(team_id):
    """Primary coach user_id for a team (first active coach)."""
    row = q1("""SELECT u.user_id FROM coaches c JOIN users u ON u.user_id=c.user_id
                WHERE c.team_id=? AND c.is_active=1 LIMIT 1""", (team_id,))
    return row["user_id"] if row else None

def run_fee_reminders(force=False):
    """Create notifications for unpaid player registration fees (at most once per 24h per reg)."""
    sy = season_start_year()
    rows = q("""SELECT ar.registration_id, ar.team_id, ar.player_id, ar.fee_reminded_at, ar.player_fee_status,
        p.legal_name, p.global_license_number
        FROM annual_registrations ar JOIN players p ON p.player_id=ar.player_id
        WHERE ar.registration_status='PENDING_APPROVAL'
          AND COALESCE(ar.player_fee_status,'PENDING') IN ('PENDING','AWAITING_CONFIRMATION')
          AND ar.season_year IN (?,?)""", (sy, SEASON))
    fee = fees()["player"]
    now = datetime.utcnow()
    sent = 0
    for ar in rows or []:
        last = ar.get("fee_reminded_at")
        if last and not force:
            try:
                prev = datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
                if (now - prev).total_seconds() < 24 * 3600:
                    continue
            except Exception:
                pass
        name = ar.get("legal_name") or "Player"
        lic = ar.get("global_license_number") or ""
        body = f"{name} ({lic}) — registration fee E{fee} still unpaid. Pay via MoMo, EFT or Cash+POP."
        for row in q("SELECT user_id FROM coaches WHERE team_id=? AND is_active=1 AND user_id IS NOT NULL", (ar["team_id"],)):
            notify(row["user_id"], "Fee reminder", body, "player_reg", ar["player_id"])
        for row in q("SELECT user_id FROM managers WHERE team_id=? AND is_active=1 AND user_id IS NOT NULL", (ar["team_id"],)):
            notify(row["user_id"], "Fee reminder", body, "player_reg", ar["player_id"])
        q("UPDATE annual_registrations SET fee_reminded_at=? WHERE registration_id=?",
          (now.strftime("%Y-%m-%d %H:%M:%S"), ar["registration_id"]), fetch=False)
        sent += 1
    return sent

def init_db():
    conn = get_conn()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
        full_name TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('ADMIN','OFFICER','COACH','MANAGER')),
        team_id INTEGER, phone TEXT, is_active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS teams (
        team_id INTEGER PRIMARY KEY AUTOINCREMENT, team_name TEXT NOT NULL,
        division TEXT DEFAULT 'National', category TEXT DEFAULT 'Male',
        region TEXT DEFAULT 'National',
        primary_jersey_color TEXT, secondary_jersey_color TEXT,
        manager_name TEXT NOT NULL, manager_phone TEXT,
        team_fee_status TEXT DEFAULT 'PENDING', created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(team_name, division, category));
    CREATE TABLE IF NOT EXISTS managers (
        manager_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER REFERENCES users(user_id),
        team_id INTEGER REFERENCES teams(team_id),
        legal_name TEXT NOT NULL, phone TEXT, email TEXT,
        national_id TEXT, date_of_birth TEXT, gender TEXT,
        address TEXT, region TEXT DEFAULT 'National', photo_path TEXT, license_number TEXT UNIQUE,
        season_year INTEGER NOT NULL, is_active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS coaches (
        coach_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER REFERENCES users(user_id),
        team_id INTEGER NOT NULL REFERENCES teams(team_id),
        legal_name TEXT NOT NULL, phone TEXT, email TEXT,
        national_id TEXT, date_of_birth TEXT, gender TEXT,
        address TEXT, region TEXT DEFAULT 'National', photo_path TEXT, license_number TEXT UNIQUE,
        qualifications TEXT, season_year INTEGER NOT NULL, is_active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS players (
        player_id INTEGER PRIMARY KEY AUTOINCREMENT,
        global_license_number TEXT UNIQUE NOT NULL, legal_name TEXT NOT NULL,
        date_of_birth TEXT NOT NULL, gender TEXT NOT NULL, blood_type TEXT,
        nationality TEXT, national_id TEXT, address TEXT, phone TEXT, email TEXT,
        region TEXT DEFAULT 'National',
        height_cm REAL, weight_kg REAL, photo_path TEXT,
        emergency_contact_name TEXT, emergency_contact_phone TEXT,
        medical_notes TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS annual_registrations (
        registration_id INTEGER PRIMARY KEY AUTOINCREMENT,
        player_id INTEGER NOT NULL REFERENCES players(player_id) ON DELETE CASCADE,
        team_id INTEGER NOT NULL REFERENCES teams(team_id),
        season_year INTEGER NOT NULL, preferred_position TEXT, dominant_hand TEXT,
        jersey_number INTEGER, medical_waiver_signed INTEGER DEFAULT 0,
        player_fee_status TEXT DEFAULT 'PENDING', is_active_roster INTEGER DEFAULT 1,
        registration_status TEXT DEFAULT 'PENDING_APPROVAL',
        approved_by INTEGER,
        approved_at TEXT,
        rejection_reason TEXT,
        payment_method TEXT,
        pop_path TEXT,
        fee_reminded_at TEXT,
        UNIQUE(player_id, season_year));
    CREATE TABLE IF NOT EXISTS transfer_history (
        transfer_id INTEGER PRIMARY KEY AUTOINCREMENT,
        player_id INTEGER REFERENCES players(player_id),
        from_team_id INTEGER REFERENCES teams(team_id),
        to_team_id INTEGER REFERENCES teams(team_id),
        window_type TEXT NOT NULL, fee_paid_status TEXT DEFAULT 'PENDING',
        from_coach_agreed INTEGER DEFAULT 0, to_coach_agreed INTEGER DEFAULT 0,
        approved_by_admin INTEGER DEFAULT 0, status TEXT DEFAULT 'PENDING',
        request_type TEXT DEFAULT 'PULL',
        fee_payer_team_id INTEGER,
        requested_by INTEGER REFERENCES users(user_id),
        notes TEXT, transfer_timestamp TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS financial_ledger (
        transaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
        reference_number TEXT UNIQUE NOT NULL, momo_mpamba_reference TEXT,
        amount REAL NOT NULL, payment_type TEXT NOT NULL,
        payment_status TEXT DEFAULT 'PENDING', payer_phone_number TEXT NOT NULL,
        related_team_id INTEGER, related_player_id INTEGER, related_transfer_id INTEGER,
        timestamp TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS player_stats (
        stat_id INTEGER PRIMARY KEY AUTOINCREMENT,
        player_id INTEGER NOT NULL REFERENCES players(player_id),
        season_year INTEGER NOT NULL, match_date TEXT, opponent TEXT,
        goals INTEGER DEFAULT 0, assists INTEGER DEFAULT 0, saves INTEGER DEFAULT 0,
        yellow_cards INTEGER DEFAULT 0, red_cards INTEGER DEFAULT 0,
        minutes_played INTEGER DEFAULT 0, notes TEXT,
        recorded_by INTEGER REFERENCES users(user_id),
        created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS notifications (
        notification_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(user_id),
        title TEXT NOT NULL, body TEXT, link_type TEXT, link_id INTEGER,
        is_read INTEGER DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS share_links (
        token TEXT PRIMARY KEY,
        team_id INTEGER NOT NULL REFERENCES teams(team_id),
        created_by INTEGER REFERENCES users(user_id),
        expires_at TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    """)
    defaults = {"team_fee":"150","player_fee":"50","transfer_fee":"50",
                "transfer_window_active":"1","transfer_window_type":"MID_SEASON",
                "season_start_year":"2026","season_label":"2026/27",
                "season_start_date":"2026-03-01","season_end_date":"2027-02-28"}
    for k,v in defaults.items():
        conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",(k,v))
    conn.commit()
    # Soft migrations for existing DBs
    cols = {r[1] for r in conn.execute("PRAGMA table_info(transfer_history)").fetchall()}
    if "request_type" not in cols:
        conn.execute("ALTER TABLE transfer_history ADD COLUMN request_type TEXT DEFAULT 'PULL'")
    if "fee_payer_team_id" not in cols:
        conn.execute("ALTER TABLE transfer_history ADD COLUMN fee_payer_team_id INTEGER")
    tcols = {r[1] for r in conn.execute("PRAGMA table_info(teams)").fetchall()}
    if "division" not in tcols:
        conn.execute("ALTER TABLE teams ADD COLUMN division TEXT DEFAULT 'National'")
    if "category" not in tcols:
        conn.execute("ALTER TABLE teams ADD COLUMN category TEXT DEFAULT 'Male'")
    if "region" not in tcols:
        conn.execute("ALTER TABLE teams ADD COLUMN region TEXT DEFAULT 'National'")
    pcols = {r[1] for r in conn.execute("PRAGMA table_info(players)").fetchall()}
    if "region" not in pcols:
        conn.execute("ALTER TABLE players ADD COLUMN region TEXT DEFAULT 'National'")
    ccols = {r[1] for r in conn.execute("PRAGMA table_info(coaches)").fetchall()}
    if "region" not in ccols:
        conn.execute("ALTER TABLE coaches ADD COLUMN region TEXT DEFAULT 'National'")
    mcols = {r[1] for r in conn.execute("PRAGMA table_info(managers)").fetchall()}
    if "region" not in mcols:
        conn.execute("ALTER TABLE managers ADD COLUMN region TEXT DEFAULT 'National'")
    # Player registration approval columns (existing DBs)
    arcols = {r[1] for r in conn.execute("PRAGMA table_info(annual_registrations)").fetchall()}
    if "registration_status" not in arcols:
        conn.execute("ALTER TABLE annual_registrations ADD COLUMN registration_status TEXT DEFAULT 'APPROVED'")
    if "approved_by" not in arcols:
        conn.execute("ALTER TABLE annual_registrations ADD COLUMN approved_by INTEGER")
    if "approved_at" not in arcols:
        conn.execute("ALTER TABLE annual_registrations ADD COLUMN approved_at TEXT")
    if "rejection_reason" not in arcols:
        conn.execute("ALTER TABLE annual_registrations ADD COLUMN rejection_reason TEXT")
    if "payment_method" not in arcols:
        conn.execute("ALTER TABLE annual_registrations ADD COLUMN payment_method TEXT")
    if "pop_path" not in arcols:
        conn.execute("ALTER TABLE annual_registrations ADD COLUMN pop_path TEXT")
    if "fee_reminded_at" not in arcols:
        conn.execute("ALTER TABLE annual_registrations ADD COLUMN fee_reminded_at TEXT")
    # Backfill: existing rows are treated as already approved and on roster
    try:
        conn.execute("""UPDATE annual_registrations
            SET registration_status='APPROVED'
            WHERE registration_status IS NULL OR registration_status=''""")
        conn.execute("""UPDATE annual_registrations SET is_active_roster=1
            WHERE registration_status='APPROVED'""")
    except Exception:
        pass
    conn.commit()

    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]==0:
        conn.execute("INSERT INTO users(username,password_hash,full_name,role,phone) VALUES(?,?,?,?,?)",
                     ("admin",hash_pw("admin123"),"System Administrator","ADMIN","26876000001"))
        conn.execute("INSERT INTO users(username,password_hash,full_name,role,phone) VALUES(?,?,?,?,?)",
                     ("officer",hash_pw("officer123"),"League Officer","OFFICER","26876000002"))
        for trow in [
            ("Mbabane Lions","U20","Male","Hhohho","Blue","White","Sipho Dlamini","26876111111"),
            ("Manzini Tigers","U18","Female","Manzini","Orange","Black","Thandi Nkosi","26876222222"),
            ("Siteki Eagles","National","Male","Lubombo","Green","Gold","Musa Shongwe","26876333333"),
            ("Nhlangano Stars","U14","Female","Shiselweni","Red","White","Nomsa Mamba","26876444444")]:
            conn.execute("""INSERT INTO teams(team_name,division,category,region,primary_jersey_color,secondary_jersey_color,manager_name,manager_phone,team_fee_status)
                VALUES(?,?,?,?,?,?,?,?,'PAID')""", trow)
        for i,(uname,name,tid,phone) in enumerate([
            ("coach_lions","Coach Sibusiso Dlamini",1,"26876555551"),
            ("coach_tigers","Coach Ayanda Zwane",2,"26876555552"),
            ("coach_eagles","Coach Themba Hlophe",3,"26876555553"),
            ("coach_stars","Coach Lindiwe Fakudze",4,"26876555554")],1):
            cur=conn.execute("INSERT INTO users(username,password_hash,full_name,role,team_id,phone) VALUES(?,?,?,?,?,?)",
                             (uname,hash_pw("coach123"),name,"COACH",tid,phone))
            conn.execute("INSERT INTO coaches(user_id,team_id,legal_name,phone,license_number,season_year,gender) VALUES(?,?,?,?,?,?,?)",
                         (cur.lastrowid,tid,name,phone,f"COACH-{SEASON}-{1000+i}",SEASON,"Male" if i%2 else "Female"))
        for name,dob,gender,tid,pos,hand in [
            ("Sibusiso Mamba","1998-05-12","Male",1,"Pivot","Right"),
            ("Nolwazi Shongwe","2001-11-03","Female",1,"Wing","Left"),
            ("Themba Dube","1995-08-22","Male",1,"Goalkeeper","Right"),
            ("Ayanda Khumalo","1999-02-14","Female",2,"Back","Right"),
            ("Sipho Maseko","1997-07-19","Male",2,"Pivot","Left"),
            ("Thandiwe Nkosi","2000-04-08","Female",2,"Wing","Right"),
            ("Musa Dlamini","1996-12-01","Male",3,"Back","Right"),
            ("Nomcebo Hlophe","2002-03-25","Female",3,"Goalkeeper","Left"),
            ("Bongani Fakudze","1994-09-30","Male",4,"Pivot","Right"),
            ("Zanele Mamba","2001-06-15","Female",4,"Wing","Left")]:
            lic=f"HAE-{str(SEASON)[-2:]}-{random.randint(100000,999999)}"
            cur=conn.execute("INSERT INTO players(global_license_number,legal_name,date_of_birth,gender,nationality) VALUES(?,?,?,?,?)",
                             (lic,name,dob,gender,"Eswatini"))
            conn.execute("""INSERT INTO annual_registrations(player_id,team_id,season_year,preferred_position,dominant_hand,
                medical_waiver_signed,player_fee_status,registration_status)
                VALUES(?,?,?,?,?,1,'PAID','APPROVED')""",
                         (cur.lastrowid,tid,SEASON,pos,hand))
        conn.commit()
        print("Seeded demo data")
    conn.close()

# ── HTML (compact but feature-complete) ─────────────────────
HTML = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0a2f5c"><title>EHA Registration</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&family=Sora:wght@600;700;800&display=swap" rel="stylesheet">
<style>
:root{
  --primary:#0a2f5c;
  --primary-mid:#12447f;
  --primary-dark:#061d3a;
  --accent:#e0392c;
  --accent-dark:#b8291f;
  --gold:#f2b134;
  --p:#0a2f5c;--p2:#061d3a;--a:#e0392c;
  --bg:#e8eef6;
  --card:#ffffff;
  --border:#d9e2ef;
  --text:#152033;
  --muted:#5f6b7c;
  --ok:#0a7a3e;--pend:#b36b00;
  --success:#0a7a3e;--success-bg:#e3f7ec;
  --pending:#b36b00;--pending-bg:#fff4dd;
  --fail:#c0392b;--fail-bg:#fdeaea;
  --radius:16px;
  --safe:env(safe-area-inset-bottom,0px);
  --content-max:1120px;
  --gap:clamp(12px,2vw,18px);
  --shadow-sm:0 1px 2px rgba(10,47,92,.05),0 1px 3px rgba(10,47,92,.04);
  --shadow-md:0 12px 28px -10px rgba(10,47,92,.18);
  --shadow-lg:0 22px 48px -14px rgba(10,47,92,.28);
  --ease:cubic-bezier(.22,1,.36,1);
}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
html{scroll-behavior:smooth;-webkit-text-size-adjust:100%}
body{
  font-family:'Manrope','Segoe UI',system-ui,-apple-system,sans-serif;
  color:var(--text);line-height:1.5;min-height:100vh;overflow-x:hidden;
  padding-bottom:calc(78px + var(--safe));
  background:
    radial-gradient(900px 420px at 100% -8%,rgba(224,57,44,.09),transparent 55%),
    radial-gradient(800px 400px at -5% 0%,rgba(10,47,92,.08),transparent 50%),
    linear-gradient(180deg,#f3f6fb 0%,var(--bg) 40%,#e6edf6 100%);
}
button,input,select,textarea{font:inherit}
.header{
  background:linear-gradient(125deg,var(--primary-dark) 0%,var(--primary) 48%,var(--primary-mid) 100%);
  color:#fff;padding:clamp(12px,2vw,18px) clamp(14px,3vw,28px);
  position:sticky;top:0;z-index:50;display:flex;align-items:center;gap:12px;
  box-shadow:var(--shadow-lg);overflow:hidden;
}
.header::before{
  content:'';position:absolute;width:220px;height:220px;border-radius:50%;
  background:rgba(255,255,255,.05);right:-40px;top:-80px;pointer-events:none;
}
.header::after{
  content:'';position:absolute;left:0;right:0;bottom:0;height:3px;
  background:linear-gradient(90deg,var(--gold),var(--accent) 40%,var(--gold));
}
.header img.logo{
  height:clamp(42px,7vw,54px);width:clamp(42px,7vw,54px);object-fit:contain;
  background:#fff;border-radius:12px;padding:4px;flex-shrink:0;
  box-shadow:0 4px 12px rgba(0,0,0,.15);position:relative;z-index:1;
}
.header h1{
  font-family:'Sora','Manrope',sans-serif;
  font-size:clamp(.95rem,2.2vw,1.3rem);font-weight:700;letter-spacing:.2px;position:relative;z-index:1;
}
.header .sub{font-size:clamp(.68rem,1.4vw,.8rem);opacity:.9;margin-top:2px}
.header .user-info{margin-left:auto;font-size:.72rem;text-align:right;position:relative;z-index:1;line-height:1.35}
.banner{
  background:linear-gradient(90deg,#e3f7ec,#d8f0e4);color:var(--success);
  font-size:clamp(.7rem,1.5vw,.8rem);text-align:center;padding:9px 14px;
  border-bottom:1px solid #c5e6d2;font-weight:600;
}
main{width:min(100% - 24px,var(--content-max));margin:0 auto;padding:clamp(14px,2.5vw,24px) 0 28px}
.panel{display:none;animation:fadeUp .35s var(--ease)}
.panel.active{display:block}
@keyframes fadeUp{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
.stats{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:var(--gap);margin-bottom:var(--gap)}
.stat{
  min-width:0;background:linear-gradient(165deg,#fff 0%,#f5f8fc 100%);
  border:1px solid var(--border);border-radius:var(--radius);
  padding:clamp(14px,2.2vw,18px);text-align:center;
  box-shadow:var(--shadow-sm);position:relative;overflow:hidden;
  transition:transform .2s var(--ease),box-shadow .2s var(--ease);
}
.stat:hover{transform:translateY(-2px);box-shadow:var(--shadow-md)}
.stat::before{
  content:'';position:absolute;top:0;left:0;right:0;height:3px;
  background:linear-gradient(90deg,var(--gold),var(--accent));
}
.stat .v{
  font-family:'Sora',sans-serif;font-size:clamp(1.3rem,3.2vw,1.85rem);font-weight:800;
  background:linear-gradient(135deg,var(--primary),var(--primary-mid));
  -webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent;
  overflow-wrap:anywhere;
}
.stat .l{font-size:clamp(.68rem,1.5vw,.76rem);color:var(--muted);margin-top:5px;font-weight:600}
.card{
  min-width:0;background:var(--card);border:1px solid var(--border);
  border-radius:var(--radius);padding:clamp(16px,2.5vw,22px);
  margin-bottom:var(--gap);box-shadow:var(--shadow-sm);
  transition:box-shadow .2s var(--ease);
}
.card:hover{box-shadow:var(--shadow-md)}
.card h2{
  font-family:'Sora',sans-serif;font-size:clamp(.92rem,1.8vw,1.05rem);
  color:var(--primary);margin-bottom:14px;padding-bottom:10px;
  border-bottom:1px solid var(--border);font-weight:700;
  display:flex;align-items:center;gap:10px;
}
.card h2::before{
  content:'';width:4px;height:18px;border-radius:4px;flex-shrink:0;
  background:linear-gradient(180deg,var(--accent),var(--gold));
}
form{display:flex;flex-direction:column;gap:11px}
label{font-size:.78rem;font-weight:700;color:var(--muted);letter-spacing:.2px}
input,select,textarea{
  width:100%;min-height:48px;padding:12px 14px;
  border:1.5px solid var(--border);border-radius:12px;font-size:16px;
  background:#f7f9fc;color:var(--text);transition:border-color .15s,box-shadow .15s,background .15s;
}
textarea{min-height:80px;resize:vertical}
input:focus,select:focus,textarea:focus{
  outline:none;border-color:var(--primary);background:#fff;
  box-shadow:0 0 0 4px rgba(10,47,92,.12);
}
select{
  appearance:none;-webkit-appearance:none;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8'%3E%3Cpath d='M1 1l5 5 5-5' stroke='%235f6b7c' stroke-width='1.5' fill='none'/%3E%3C/svg%3E");
  background-repeat:no-repeat;background-position:right 14px center;padding-right:38px;
}
.btn{
  display:block;width:100%;min-height:48px;padding:12px 18px;border:none;border-radius:12px;
  font-size:.92rem;font-weight:700;cursor:pointer;text-align:center;color:#fff;
  background:linear-gradient(135deg,var(--primary-mid),var(--primary));
  margin-top:4px;touch-action:manipulation;
  box-shadow:0 6px 16px -6px rgba(10,47,92,.5);
  transition:transform .15s var(--ease),filter .15s,box-shadow .15s;
}
.btn:hover{filter:brightness(1.06);transform:translateY(-1px)}
.btn:active{transform:scale(.98);filter:brightness(.98)}
.btn-a{background:linear-gradient(135deg,var(--accent),var(--accent-dark));box-shadow:0 6px 16px -6px rgba(224,57,44,.55)}
.btn-danger{background:linear-gradient(135deg,#d64545,#a82e2e);box-shadow:0 6px 16px -6px rgba(168,46,46,.45)}
.btn-ghost{
  background:transparent;color:var(--muted);border:1.5px solid var(--border);
  box-shadow:none;width:100%;
}
.btn-ghost:hover{background:#f0f4f9;color:var(--primary);filter:none}
.btn-out,.btn-sm{
  display:inline-block;width:auto;min-height:auto;padding:8px 14px;font-size:.78rem;
  border-radius:10px;margin:4px 4px 0 0;box-shadow:0 3px 10px -4px rgba(10,47,92,.35);
}
.btn-out{background:#6b7688}
.list-item{
  padding:14px 4px;border-bottom:1px solid var(--border);cursor:pointer;
  border-radius:10px;transition:background .15s;
}
.list-item:hover{background:rgba(10,47,92,.03)}
.list-item:last-child{border-bottom:none}
.list-item .title{font-weight:700;font-size:.93rem;overflow-wrap:anywhere}
.list-item .meta{font-size:.76rem;color:var(--muted);margin-top:3px;overflow-wrap:anywhere}
.list-item .row{display:flex;justify-content:space-between;align-items:center;gap:12px}
.list-item .row>div{min-width:0;flex:1}
.avatar{
  width:44px;height:44px;border-radius:12px;object-fit:cover;
  background:linear-gradient(145deg,#e8eef6,#d5dee9);flex-shrink:0;
  border:2px solid #fff;box-shadow:0 2px 6px rgba(10,47,92,.12);
}
.avatar-lg{
  width:100px;height:122px;border-radius:14px;object-fit:cover;
  background:linear-gradient(145deg,#e8eef6,#d5dee9);display:block;margin:0 auto;
  border:3px solid #fff;box-shadow:var(--shadow-md);
}
.profile-hero{text-align:center;margin-bottom:8px;position:relative}
.profile-badge{
  display:inline-block;margin-top:10px;padding:4px 12px;border-radius:999px;
  font-size:.7rem;font-weight:700;letter-spacing:.4px;text-transform:uppercase;
  background:rgba(10,47,92,.08);color:var(--primary);
}
.profile-name{text-align:center;margin:8px 0 14px;font-family:'Sora',sans-serif;color:var(--primary)}
.profile-actions{display:flex;flex-direction:column;gap:8px;margin-top:16px}
.profile-edit{
  margin-top:16px;padding:14px;background:#f5f8fc;border:1px solid var(--border);
  border-radius:12px;
}
.profile-edit h4{font-family:'Sora',sans-serif;color:var(--primary);font-size:.9rem;margin-bottom:10px}
.status{
  display:inline-block;padding:4px 11px;border-radius:999px;
  font-size:.68rem;font-weight:800;letter-spacing:.35px;text-transform:uppercase;white-space:nowrap;flex-shrink:0;
}
.status.PAID,.status.SUCCESSFUL,.status.OPEN,.status.APPROVED,.status.AGREED{background:var(--success-bg);color:var(--success)}
.status.PENDING,.status.CLOSED{background:var(--pending-bg);color:var(--pending)}
.status.FAILED,.status.REJECTED{background:var(--fail-bg);color:var(--fail)}
.msg{
  padding:12px 14px;border-radius:12px;margin-top:10px;font-size:.86rem;font-weight:600;
  word-break:break-word;border-left:4px solid transparent;
}
.msg.ok{background:var(--success-bg);color:var(--success);border-left-color:var(--success)}
.msg.err{background:var(--fail-bg);color:var(--fail);border-left-color:var(--fail)}
.msg.info{background:#e6f3fb;color:#0c5460;border-left-color:#0c8bb8}
.role-badge{
  padding:3px 10px;border-radius:999px;font-size:.68rem;font-weight:700;
  background:rgba(255,255,255,.16);border:1px solid rgba(255,255,255,.28);
}
.login-box{
  max-width:400px;margin:clamp(28px,8vw,56px) auto;padding:clamp(22px,4vw,32px);
  text-align:center;box-shadow:var(--shadow-lg);border:1px solid rgba(10,47,92,.06);
}
.login-box img{height:clamp(76px,14vw,100px);margin-bottom:12px;filter:drop-shadow(0 6px 12px rgba(10,47,92,.15))}
.login-box h2{font-family:'Sora',sans-serif;color:var(--primary);margin-bottom:6px;font-size:1.25rem}
.login-box p{color:var(--muted);font-size:.85rem;margin-bottom:16px}
.bottom-nav{
  position:fixed;bottom:0;left:0;right:0;
  background:rgba(255,255,255,.92);backdrop-filter:blur(14px) saturate(1.2);
  border-top:1px solid var(--border);
  display:flex;justify-content:space-around;
  padding:7px 2px calc(7px + var(--safe));z-index:100;
  box-shadow:0 -8px 28px rgba(10,47,92,.08);
}
.bottom-nav button{
  flex:1;background:none;border:none;padding:6px 2px;
  font-size:clamp(.54rem,1.6vw,.64rem);color:var(--muted);
  display:flex;flex-direction:column;align-items:center;gap:3px;font-weight:700;cursor:pointer;
  border-radius:10px;transition:color .15s,background .15s;
}
.bottom-nav button:hover{background:rgba(10,47,92,.04)}
.bottom-nav button.active{color:var(--primary)}
.bottom-nav button.active .icon{
  background:rgba(10,47,92,.08);border-radius:10px;padding:4px 10px;
}
.bottom-nav .icon{font-size:1.2rem;line-height:1;transition:background .15s,padding .15s}
.hidden{display:none!important}
.team-chip{
  display:inline-block;padding:9px 15px;margin:4px;border-radius:999px;
  border:1.5px solid var(--border);background:#fff;cursor:pointer;
  font-size:.8rem;font-weight:700;color:var(--text);
  box-shadow:var(--shadow-sm);transition:all .15s var(--ease);
}
.team-chip:hover{border-color:var(--primary-mid);color:var(--primary)}
.team-chip.active{
  background:linear-gradient(135deg,var(--primary-mid),var(--primary));
  color:#fff;border-color:transparent;box-shadow:0 6px 14px -6px rgba(10,47,92,.45);
}
.section-label{
  font-size:.72rem;font-weight:800;color:var(--muted);margin:14px 0 8px;
  text-transform:uppercase;letter-spacing:.55px;
}
.empty-hint{color:var(--muted);font-size:.88rem;padding:12px 0;text-align:center}
.modal-bg{
  position:fixed;inset:0;background:rgba(6,29,58,.55);z-index:200;
  display:flex;align-items:flex-end;justify-content:center;padding:0;
  backdrop-filter:blur(4px);animation:fadeUp .25s ease;
}
.modal{
  background:#fff;border-radius:20px 20px 0 0;width:100%;max-width:480px;
  max-height:92vh;overflow-y:auto;padding:22px 20px calc(22px + var(--safe));
  box-shadow:var(--shadow-lg);animation:sheetUp .35s var(--ease);
}
@keyframes sheetUp{from{transform:translateY(40px);opacity:.6}to{transform:none;opacity:1}}
.modal h3{font-family:'Sora',sans-serif;color:var(--primary);margin-bottom:4px}
.profile-row{
  display:flex;justify-content:space-between;gap:12px;padding:10px 0;
  border-bottom:1px solid var(--border);font-size:.86rem;
}
.profile-row .k{color:var(--muted);font-weight:600}
.profile-row .v{font-weight:700;text-align:right;max-width:62%;overflow-wrap:anywhere}
.notif-dot{
  background:var(--accent);color:#fff;border-radius:999px;font-size:.62rem;
  padding:1px 6px;margin-left:3px;font-weight:800;
}
.badge-row{display:flex;flex-wrap:wrap;gap:4px;margin-top:6px}
.db-table{
  width:100%;overflow:auto;max-height:420px;border:1px solid var(--border);
  border-radius:12px;background:#fff;
}
.db-table table{border-collapse:collapse;min-width:100%;font-size:.72rem}
.db-table th,.db-table td{
  border-bottom:1px solid var(--border);padding:8px 10px;text-align:left;
  white-space:nowrap;max-width:160px;overflow:hidden;text-overflow:ellipsis;
}
.db-table th{
  background:linear-gradient(180deg,#f4f7fb,#eef2f8);color:var(--primary);
  position:sticky;top:0;font-weight:800;font-size:.7rem;text-transform:uppercase;letter-spacing:.3px;
}
.db-table tr:nth-child(even){background:#f8fafc}
.id-card{
  width:min(100%,320px);border:2px solid var(--primary);border-radius:14px;
  padding:16px;background:#fff;margin:0 auto;box-shadow:var(--shadow-md);
}
.id-card .card-head{
  display:flex;align-items:center;gap:10px;border-bottom:2px solid var(--primary);
  padding-bottom:10px;margin-bottom:12px;
}
.id-card .card-head img{height:44px}
.id-card .card-head .org{font-size:.72rem;font-weight:800;color:var(--primary);line-height:1.3}
.id-card .card-photo{
  width:92px;height:112px;object-fit:cover;border:1px solid var(--border);
  border-radius:8px;float:right;margin-left:10px;
}
.id-card .card-name{font-size:1.08rem;font-weight:800;color:var(--primary);margin:6px 0 4px;font-family:'Sora',sans-serif}
.id-card .card-line{font-size:.78rem;margin:4px 0;color:#333}
.id-card .card-foot{
  clear:both;margin-top:14px;padding-top:10px;border-top:1px solid var(--border);
  font-size:.65rem;color:var(--muted);text-align:center;font-weight:600;
}
@media print{
  body *{visibility:hidden}
  #print-root,#print-root *{visibility:visible}
  #print-root{position:absolute;left:0;top:0}
  .no-print{display:none!important}
}
@media (min-width:560px){
  .modal-bg{align-items:center;padding:16px}
  .modal{border-radius:18px;max-height:88vh;padding:24px}
}
@media (min-width:640px){
  .stats{grid-template-columns:repeat(4,minmax(0,1fr))}
  .btn:not(.btn-sm):not(.btn-out):not(.btn-ghost){width:auto;display:inline-block;min-width:168px}
  .bottom-nav button{font-size:.68rem}
  .profile-actions{flex-direction:row;flex-wrap:wrap}
  .profile-actions .btn{width:auto;flex:1;min-width:140px}
}
@media (min-width:900px){
  main{padding-top:26px}
  .card{padding:24px}
  .header{padding:16px 32px}
}
@media (min-width:1100px){
  .grid-2{display:grid;grid-template-columns:1fr 1fr;gap:var(--gap)}
}
@media (max-width:400px){
  .header h1{font-size:.88rem}
  .team-chip{font-size:.72rem;padding:7px 11px}
  .bottom-nav button{font-size:.5rem}
}
</style>
</head>
<body>
<div id="login-screen">
  <div class="banner">Eswatini Handball Association · Season 2026/27</div>
  <div class="card login-box">
    <img src="/static/eha-logo.jpg" alt="EHA Logo">
    <h2>EHA Registration</h2>
    <p style="font-size:.8rem;color:#666;margin-bottom:12px">Sign in to continue</p>
    <form id="f-login" style="text-align:left">
      <label>Username</label><input name="username" required autocomplete="username">
      <label>Password</label><input name="password" type="password" required>
      <button type="submit" class="btn">Login</button>
    </form>
    <div id="m-login"></div>
  </div>
</div>

<div id="app-screen" class="hidden">
<div class="banner">EHA · Multi-device · Transfer notifications</div>
<div class="header">
  <img class="logo" src="/static/eha-logo.jpg" alt="EHA">
  <div><h1>Eswatini Handball Association</h1><div class="sub">Registration &amp; Transfers</div></div>
  <div class="user-info">
    <div id="hdr-name">–</div>
    <div><span class="role-badge" id="hdr-role">–</span>
    <button class="btn btn-out" id="btn-logout" style="margin-left:4px">Out</button></div>
  </div>
</div>
<main>
  <div id="dash" class="panel active">
    <div class="stats">
      <div class="stat"><div class="v" id="s-teams-all">–</div><div class="l">Teams</div></div>
      <div class="stat"><div class="v" id="s-players-all">–</div><div class="l">Players</div></div>
      <div class="stat"><div class="v" id="s-coaches-all">–</div><div class="l">Coaches</div></div>
      <div class="stat"><div class="v" id="s-managers-all">–</div><div class="l">Managers</div></div>
    </div>
    <div class="stats">
      <div class="stat"><div class="v" id="s-teams">–</div><div class="l">Teams Paid</div></div>
      <div class="stat"><div class="v" id="s-players">–</div><div class="l">Players Paid</div></div>
      <div class="stat"><div class="v" id="s-window">–</div><div class="l">Transfer Window</div></div>
      <div class="stat"><div class="v" id="s-fees">–</div><div class="l">Team/Player Fee</div></div>
    </div>
    <div class="card"><h2>Registration by region</h2>
      <div id="region-stats" class="db-table">Loading…</div>
    </div>
    <div class="card"><h2>Notifications <span id="notif-count"></span></h2><div id="notif-list">–</div></div>
    <div class="card"><h2>Permissions</h2><div id="perms" style="font-size:.85rem"></div></div>
  </div>

  <div id="teams" class="panel">
    <div class="card"><h2>Select team</h2><div id="team-chips">…</div></div>
    <div class="card"><h2 id="roster-title">Roster</h2>
      <p id="roster-empty" style="color:#888;font-size:.85rem">Select a team.</p>
      <div id="roster-coaches" class="hidden"></div>
      <div id="roster-players" class="hidden"></div>
    </div>
    <div class="card officer-register hidden"><h2 id="team-form-title">Register Team</h2>
      <form id="f-team">
        <input type="hidden" name="team_id" id="edit-team-id" value="">
        <label>Team Name *</label><input name="team_name" id="team-name" required>
        <label>Division *</label>
        <select name="division" id="team-division" required>
          <option value="U14">U14</option><option value="U18">U18</option>
          <option value="U20">U20</option><option value="National" selected>National</option>
        </select>
        <label>Category *</label>
        <select name="category" id="team-category" required>
          <option value="Male">Male</option><option value="Female">Female</option>
        </select>
        <label>Region *</label>
        <select name="region" id="team-region" required>
          <option value="Hhohho">Hhohho</option>
          <option value="Manzini">Manzini</option>
          <option value="Lubombo">Lubombo</option>
          <option value="Shiselweni">Shiselweni</option>
          <option value="National">National</option>
        </select>
        <label>Manager name *</label><input name="manager_name" id="team-manager" required>
        <label>Manager phone</label><input name="manager_phone" id="team-phone" type="tel">
        <label>Jersey colour</label><input name="primary_jersey_color" id="team-jersey">
        <button class="btn" type="submit" id="btn-team-save">Create Team</button>
        <button class="btn btn-out" type="button" id="btn-team-cancel" style="display:none;margin-top:6px">Cancel edit</button>
      </form><div id="m-team"></div>
    </div>
    <div class="card"><h2>All teams</h2><div id="list-all-teams">…</div></div>
  </div>

  <div id="players" class="panel">
    <div class="card officer-register hidden"><h2>Register Player</h2>
      <form id="f-player" enctype="multipart/form-data">
        <label>Photo (passport size)</label><input name="photo" type="file" accept="image/*">
        <label>Legal Name *</label><input name="legal_name" required>
        <label>Date of Birth *</label><input name="date_of_birth" type="date" required>
        <label>Gender *</label><select name="gender" required><option value="">Select</option><option>Male</option><option>Female</option></select>
        <label>Team *</label><select name="team_id" id="sel-team" required></select>
        <label>Region *</label>
        <select name="region" required>
          <option value="Hhohho">Hhohho</option>
          <option value="Manzini">Manzini</option>
          <option value="Lubombo">Lubombo</option>
          <option value="Shiselweni">Shiselweni</option>
          <option value="National">National</option>
        </select>
        <label>Position</label><select name="preferred_position"><option value="">–</option><option>Goalkeeper</option><option>Pivot</option><option>Wing</option><option>Back</option></select>
        <label>Dominant Hand</label><select name="dominant_hand"><option value="">–</option><option>Left</option><option>Right</option></select>
        <label>Jersey #</label><input name="jersey_number" type="number">
        <label>Nationality</label><input name="nationality" value="Eswatini">
        <label>National ID</label><input name="national_id">
        <label>Phone</label><input name="phone" type="tel">
        <label>Email</label><input name="email" type="email">
        <label>Address</label><input name="address">
        <label>Height (cm)</label><input name="height_cm" type="number" step="0.1">
        <label>Weight (kg)</label><input name="weight_kg" type="number" step="0.1">
        <label>Blood type</label><input name="blood_type">
        <label>Emergency contact</label><input name="emergency_contact_name">
        <label>Emergency phone</label><input name="emergency_contact_phone" type="tel">
        <label>Medical notes</label><textarea name="medical_notes" rows="2"></textarea>
        <button class="btn" type="submit">Register Player</button>
      </form><div id="m-player"></div>
    </div>
    <div class="card" id="pending-regs-card"><h2>Pending player approvals</h2>
      <p style="font-size:.82rem;color:var(--muted);margin-bottom:10px">
        1) Coach/Manager pays fee (MoMo, EFT or Cash+POP) · 2) Admin approves · 3) Player appears on roster.
      </p>
      <button class="btn btn-sm admin-only hidden" type="button" onclick="sendFeeReminders()" style="margin-bottom:10px">Send fee reminders</button>
      <div id="list-pending-regs">…</div>
    </div>
    <div class="card"><h2>Players</h2><div id="list-players">…</div></div>
  </div>

  <div id="coaches" class="panel">
    <div class="card officer-register hidden"><h2>Register Coach</h2>
      <form id="f-coach" enctype="multipart/form-data">
        <label>Photo</label><input name="photo" type="file" accept="image/*">
        <label>Full Name *</label><input name="legal_name" required>
        <label>Team *</label><select name="team_id" id="sel-coach-team" required></select>
        <label>Region *</label>
        <select name="region" required>
          <option value="Hhohho">Hhohho</option>
          <option value="Manzini">Manzini</option>
          <option value="Lubombo">Lubombo</option>
          <option value="Shiselweni">Shiselweni</option>
          <option value="National">National</option>
        </select>
        <label>Phone</label><input name="phone" type="tel">
        <label>Email</label><input name="email" type="email">
        <label>Date of Birth</label><input name="date_of_birth" type="date">
        <label>Gender</label><select name="gender"><option value="">–</option><option>Male</option><option>Female</option></select>
        <label>National ID</label><input name="national_id">
        <label>Address</label><input name="address">
        <label>Qualifications</label><input name="qualifications">
        <label>Login username *</label><input name="username" required>
        <label>Password *</label><input name="password" type="password" required value="coach123">
        <button class="btn" type="submit">Register Coach</button>
      </form><div id="m-coach"></div>
    </div>
    <div class="card"><h2>Coaches</h2><div id="list-coaches">…</div></div>
  </div>

  <div id="managers" class="panel">
    <div class="card officer-register hidden"><h2>Register Manager</h2>
      <form id="f-manager" enctype="multipart/form-data">
        <label>Photo</label><input name="photo" type="file" accept="image/*">
        <label>Full Name *</label><input name="legal_name" required>
        <label>Team *</label><select name="team_id" id="sel-mgr-team" required></select>
        <label>Region *</label>
        <select name="region" required>
          <option value="Hhohho">Hhohho</option>
          <option value="Manzini">Manzini</option>
          <option value="Lubombo">Lubombo</option>
          <option value="Shiselweni">Shiselweni</option>
          <option value="National">National</option>
        </select>
        <label>Phone</label><input name="phone" type="tel">
        <label>Email</label><input name="email" type="email">
        <label>Gender</label><select name="gender"><option value="">–</option><option>Male</option><option>Female</option></select>
        <label>Login username *</label><input name="username" required>
        <label>Password *</label><input name="password" type="password" required value="manager123">
        <button class="btn" type="submit">Register Manager</button>
      </form><div id="m-manager"></div>
    </div>
    <div class="card"><h2>Managers</h2><div id="list-managers">…</div></div>
  </div>
  <div id="transfers" class="panel">
    <div class="card"><h2>Window</h2><div id="win-status">…</div></div>
    <div class="card coach-transfer hidden">
      <h2>Pick a player to transfer</h2>
      <label>Filter by team</label>
      <select id="xfer-team-filter"><option value="">All teams</option></select>
      <input id="xfer-search" placeholder="Search name or license…" style="margin-top:8px">
      <div id="xfer-player-list" style="max-height:220px;overflow-y:auto;margin-top:8px">…</div>
    </div>
    <div class="card coach-transfer hidden"><h2>Transfer request</h2>
      <form id="f-transfer">
        <label>Request type *</label>
        <select name="request_type" id="xfer-type" required>
          <option value="PULL">Pull player to my team (I pay the fee)</option>
          <option value="OFFER">Offer my player to another team (they pay if they accept)</option>
        </select>
        <label>Selected player</label>
        <input id="xfer-player-name" readonly placeholder="Click a player above">
        <label>Player license *</label><input name="license" id="xfer-license" required placeholder="HAE-26-xxxxxx">
        <label>Current team</label><input id="xfer-from-team" readonly>
        <label>Other team *</label><select name="other_team_id" id="xfer-to-team" required><option value="">Select team</option></select>
        <p id="xfer-hint" style="font-size:.78rem;color:#666">Pull: choose any player from another team → you pay fee. Offer: choose your player → other team pays if they accept.</p>
        <label>Notes</label><input name="notes">
        <button class="btn" type="submit">Submit request</button>
      </form><div id="m-transfer"></div>
    </div>
    <div class="card"><h2>Transfer inbox</h2><div id="list-transfers">…</div></div>
  </div>

  <div id="settings" class="panel">
    <div class="card admin-only hidden"><h2>Transfer window</h2>
      <form id="f-window">
        <label>Status</label><select name="transfer_window_active"><option value="1">OPEN</option><option value="0">CLOSED</option></select>
        <label>Type</label><select name="transfer_window_type"><option value="MID_SEASON">Mid-season</option><option value="END_OF_SEASON">End-of-season</option></select>
        <button class="btn" type="submit">Save</button>
      </form><div id="m-window"></div>
    </div>
    <div class="card admin-only hidden"><h2>Fees (E)</h2>
      <form id="f-fees">
        <label>Team fee</label><input name="team_fee" type="number" step="0.01" required>
        <label>Player fee</label><input name="player_fee" type="number" step="0.01" required>
        <label>Transfer fee</label><input name="transfer_fee" type="number" step="0.01" required>
        <button class="btn" type="submit">Save fees</button>
      </form><div id="m-fees"></div>
    </div>
    <div class="card admin-only hidden"><h2>Season</h2>
      <form id="f-season">
        <label>Season start date</label><input name="season_start_date" type="date" required>
        <label>Season end date</label><input name="season_end_date" type="date" required>
        <p style="font-size:.78rem;color:#666">Label is auto-set as YYYY/YY (e.g. 2026/27) from the start year.</p>
        <button class="btn" type="submit">Save season</button>
      </form><div id="m-season"></div>
    </div>
    <div class="card admin-only hidden"><h2>Database browser</h2>
      <label>Table</label>
      <select id="db-table-select"><option value="">Select a table…</option></select>
      <button class="btn" type="button" id="btn-db-load" style="margin-top:8px">Load entries</button>
      <div id="db-meta" style="font-size:.78rem;color:#666;margin-top:8px"></div>
      <div id="db-view" class="db-table" style="margin-top:10px"></div>
    </div>
    <div class="card" id="settings-ro"><p style="font-size:.85rem;color:#666">Settings: Admin only.</p></div>
  </div>
</main>
<nav class="bottom-nav">
  <button class="active" data-p="dash"><span class="icon">⌂</span>Home</button>
  <button data-p="teams"><span class="icon">☰</span>Teams</button>
  <button data-p="players"><span class="icon">👤</span>Players</button>
  <button data-p="coaches"><span class="icon">◎</span>Coaches</button>
  <button data-p="managers"><span class="icon">📋</span>Managers</button>
  <button data-p="transfers"><span class="icon">⇄</span>Transfers<span id="nav-notif"></span></button>
  <button data-p="settings"><span class="icon">⚙</span>Settings</button>
</nav>
</div>
<div id="modal-bg" class="modal-bg hidden" onclick="if(event.target===this)closeModal()"><div class="modal" id="modal-body"></div></div>

<script>
let USER=null, SELECTED_TEAM=null, FEES={team:150,player:50,transfer:50};
const $=s=>document.querySelector(s), $$=s=>document.querySelectorAll(s);
function msg(el,t,type='ok'){el.innerHTML=`<div class="msg ${type}">${t}</div>`}
function closeModal(){$('#modal-bg').classList.add('hidden')}
function openModal(html){$('#modal-body').innerHTML=html;$('#modal-bg').classList.remove('hidden')}
function photoUrl(p){return p||'/static/eha-logo.jpg'}

function applyRoleUI(){
  const r=USER?.role;
  $$('.admin-only').forEach(el=>el.classList.toggle('hidden',r!=='ADMIN'));
  $$('.officer-register').forEach(el=>el.classList.toggle('hidden',!(r==='ADMIN'||r==='OFFICER')));
  $$('.coach-transfer').forEach(el=>el.classList.toggle('hidden',!(r==='ADMIN'||r==='OFFICER'||r==='COACH')));
  $('#settings-ro')?.classList.toggle('hidden',r==='ADMIN');
  $('#perms').textContent={
    ADMIN:'Full control · window/fees · edit bios · approve transfers after both coaches agree + fee paid.',
    OFFICER:'Register teams/players/coaches · request transfers by license · view all.',
    COACH:'Browse all rosters · Pull any player to your team (you pay) or Offer your player (they pay) · agree · notifications.',
  }[r]||'';
}

async function checkSession(){const r=await fetch('/api/me').then(r=>r.json());if(r.success&&r.user){USER=r.user;showApp()}else showLogin()}
function showLogin(){$('#login-screen').classList.remove('hidden');$('#app-screen').classList.add('hidden')}
function showApp(){
  $('#login-screen').classList.add('hidden');$('#app-screen').classList.remove('hidden');
  $('#hdr-name').textContent=USER.full_name;$('#hdr-role').textContent=USER.role;
  applyRoleUI(); refreshAll();
  setInterval(loadNotifs,15000);
}
function refreshAll(){loadDash();loadTeamChips();loadPlayers();loadCoaches();loadManagers();loadTransfers();loadNotifs();loadSettingsForm()}

$('#f-login').onsubmit=async e=>{e.preventDefault();const b=Object.fromEntries(new FormData(e.target));
  const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}).then(r=>r.json());
  if(r.success){USER=r.user;showApp()}else msg($('#m-login'),r.error,'err')};
$('#btn-logout').onclick=async()=>{await fetch('/api/logout',{method:'POST'});USER=null;showLogin()};
$$('.bottom-nav button').forEach(b=>b.onclick=()=>{$$('.bottom-nav button').forEach(x=>x.classList.remove('active'));$$('.panel').forEach(x=>x.classList.remove('active'));b.classList.add('active');$('#'+b.dataset.p).classList.add('active');window.scrollTo(0,0)});

async function loadDash(){
  try{ fetch('/api/registrations/run-reminders').catch(()=>{}); }catch{}
  try{
    const f=await fetch('/api/finance').then(r=>r.json());
    $('#s-teams').textContent=f.teams_paid;$('#s-players').textContent=f.players_paid;
    FEES=f.fees||FEES;$('#s-fees').textContent=`E${FEES.team}/${FEES.player}`;
    if(f.totals){
      $('#s-teams-all').textContent=f.totals.teams??'–';
      $('#s-players-all').textContent=f.totals.players??'–';
      $('#s-coaches-all').textContent=f.totals.coaches??'–';
      $('#s-managers-all').textContent=f.totals.managers??'–';
    }
    if(f.season_label){const b=document.querySelector('.banner'); if(b) b.textContent=`EHA · Season ${f.season_label} · Multi-device`}
    const box=$('#region-stats');
    if(box && f.by_region){
      if(!f.by_region.length){box.innerHTML='<p class="empty-hint">No regional data yet.</p>'}
      else{
        box.innerHTML=`<table><thead><tr>
          <th>Region</th><th>Teams</th><th>Players</th><th>Coaches</th><th>Managers</th>
        </tr></thead><tbody>`+
          f.by_region.map(r=>`<tr>
            <td><strong>${r.region}</strong></td>
            <td>${r.teams}</td><td>${r.players}</td><td>${r.coaches}</td><td>${r.managers}</td>
          </tr>`).join('')+
          `<tr style="font-weight:800;background:#eef2f8">
            <td>Total</td>
            <td>${f.totals.teams}</td><td>${f.totals.players}</td><td>${f.totals.coaches}</td><td>${f.totals.managers}</td>
          </tr></tbody></table>`;
      }
    }
  }catch{}
  try{const w=await fetch('/api/transfers/window').then(r=>r.json());$('#s-window').textContent=w.active?'OPEN':'CLOSED';
    $('#win-status').innerHTML=`<div class="msg ${w.active?'ok':'info'}">${w.message}</div>`}catch{}
}
async function loadNotifs(){
  try{
    const r=await fetch('/api/notifications').then(r=>r.json());
    const n=r.unread||0;
    $('#notif-count').innerHTML=n?`<span class="notif-dot">${n}</span>`:'';
    $('#nav-notif').innerHTML=n?`<span class="notif-dot">${n}</span>`:'';
    const list=$('#notif-list');
    if(!r.notifications?.length){list.innerHTML='<p style="color:#888;font-size:.85rem">No notifications.</p>';return}
    list.innerHTML=r.notifications.slice(0,15).map(n=>`
      <div class="list-item" style="cursor:default;opacity:${n.is_read?0.7:1}">
        <div class="title">${n.title}</div><div class="meta">${n.body||''} · ${n.created_at||''}</div>
      </div>`).join('');
  }catch{}
}

async function loadTeamChips(){
  const r=await fetch('/api/teams').then(r=>r.json());
  const box=$('#team-chips'); const sel=$('#sel-team'),selc=$('#sel-coach-team');
  if(!r.teams?.length){box.innerHTML='No teams';return}
  box.innerHTML=r.teams.map(t=>`<span class="team-chip ${SELECTED_TEAM===t.team_id?'active':''}" data-id="${t.team_id}" onclick="selectTeam(${t.team_id},'${t.team_name.replace(/'/g,"\\'")}')">${t.team_name} · ${t.region||''} · ${t.division||''} ${t.category||''} (${t.active_players||0})</span>`).join('');
  const o='<option value="">Select</option>'+r.teams.map(t=>`<option value="${t.team_id}">${t.team_name} (${t.division||''} ${t.category||''})</option>`).join('');
  if(sel)sel.innerHTML=o; if(selc)selc.innerHTML=o;
  const selm=$('#sel-mgr-team'); if(selm)selm.innerHTML=o;
  const list=$('#list-all-teams');
  if(list){
    const isAdmin=USER?.role==='ADMIN';
    list.innerHTML=(r.teams||[]).map(tm=>`<div class="list-item" style="cursor:default">
      <div class="title">${tm.team_name}</div>
      <div class="meta">ID ${tm.team_id} · ${tm.region||'–'} · ${tm.division||'–'} · ${tm.category||'–'} · ${tm.manager_name||''} · ${tm.active_players||0} players</div>
      ${isAdmin?`<div style="margin-top:6px">
        <button class="btn btn-sm" onclick='editTeam(${JSON.stringify(tm)})'>Edit</button>
        <button class="btn btn-sm btn-a" onclick="deleteTeam(${tm.team_id})">Delete</button>
      </div>`:''}
    </div>`).join('')||'<p style="color:#888">No teams</p>';
  }
  if(USER?.role==='COACH'&&USER.team_id&&!SELECTED_TEAM){const t=r.teams.find(x=>x.team_id===USER.team_id);if(t)selectTeam(t.team_id,t.team_name)}
}
window.selectTeam=async(id,name)=>{
  SELECTED_TEAM=id;$$('.team-chip').forEach(c=>c.classList.toggle('active',+c.dataset.id===id));
  $('#roster-title').textContent=name+' · Roster';$('#roster-empty').classList.add('hidden');
  const r=await fetch('/api/teams/'+id).then(r=>r.json());
  if(!r.success)return;
  const cBox=$('#roster-coaches'),pBox=$('#roster-players');cBox.classList.remove('hidden');pBox.classList.remove('hidden');
  let mHtml='';
  if((r.managers||[]).length){
    mHtml=`<div class="section-label">Managers</div>`+(r.managers||[]).map(m=>`
      <div class="list-item" onclick="showManager(${m.manager_id})"><div class="row">
        <img class="avatar" src="${photoUrl(m.photo_path)}" alt="">
        <div style="flex:1;margin-left:10px"><div class="title">${m.legal_name}</div><div class="meta">${m.license_number||''}</div></div>
      </div></div>`).join('');
  }
  cBox.innerHTML=mHtml+`<div class="section-label">Coaches</div>`+(r.coaches||[]).map(c=>`
    <div class="list-item" onclick="showCoach(${c.coach_id})"><div class="row">
      <img class="avatar" src="${photoUrl(c.photo_path)}" alt="">
      <div style="flex:1;margin-left:10px"><div class="title">${c.legal_name}</div><div class="meta">${c.license_number||''}</div></div>
    </div></div>`).join('')||'<p class="empty-hint">None</p>';
  pBox.innerHTML=`<div class="section-label">Players</div>`+(r.players||[]).map(p=>`
    <div class="list-item" onclick="showPlayer(${p.player_id})"><div class="row">
      <img class="avatar" src="${photoUrl(p.photo_path)}" alt="">
      <div style="flex:1;margin-left:10px"><div class="title">${p.legal_name}</div>
      <div class="meta">${p.preferred_position||'–'} · #${p.jersey_number||'–'} · ${p.global_license_number||''}</div></div>
      <span class="status ${p.player_fee_status||'PENDING'}">${p.player_fee_status||'–'}</span>
    </div></div>`).join('')||'<p class="empty-hint">None</p>';
  // Share / PDF cards for Admin & Officer
  let shareBox=$('#roster-share');
  if(!shareBox){
    shareBox=document.createElement('div');
    shareBox.id='roster-share';
    shareBox.className='card';
    pBox.parentNode.insertBefore(shareBox, pBox.nextSibling);
  }
  if(USER?.role==='ADMIN'||USER?.role==='OFFICER'){
    shareBox.classList.remove('hidden');
    shareBox.innerHTML=`<h2>Share team ID cards (PDF)</h2>
      <p style="font-size:.82rem;color:var(--muted);margin-bottom:10px">Generate a PDF of player, coach and manager cards for <b>${name}</b>. Share with the team coach or manager via WhatsApp or email.</p>
      <button class="btn" type="button" onclick="downloadTeamCardsPdf(${id})">Download PDF</button>
      <button class="btn btn-a" type="button" style="margin-top:8px" onclick="openShareTeamCards(${id},'${name.replace(/'/g,"\\'")}')">Share via WhatsApp / Email</button>`;
  } else {
    shareBox.classList.add('hidden');
    shareBox.innerHTML='';
  }
};
window.downloadTeamCardsPdf=id=>{
  window.open('/api/teams/'+id+'/cards.pdf','_blank');
};
window.openShareTeamCards=async(id,name)=>{
  openModal(`<h3>Share team cards</h3>
    <p style="font-size:.85rem;color:var(--muted);margin-bottom:12px">PDF includes players, coaches and managers for <b>${name}</b>.</p>
    <label>Recipient phone (WhatsApp, with country code)</label>
    <input id="share-wa-phone" placeholder="2687xxxxxxx" inputmode="tel">
    <label>Recipient email</label>
    <input id="share-email" type="email" placeholder="coach@example.com">
    <label>Note (optional)</label>
    <input id="share-note" placeholder="Please print and distribute ID cards">
    <div class="profile-actions" style="margin-top:14px">
      <button class="btn" type="button" onclick="doShareTeamCards(${id},'${name.replace(/'/g,"\\'")}','whatsapp')">Open WhatsApp</button>
      <button class="btn btn-a" type="button" onclick="doShareTeamCards(${id},'${name.replace(/'/g,"\\'")}','email')">Open Email</button>
      <button class="btn btn-ghost" type="button" onclick="downloadTeamCardsPdf(${id})">Download PDF only</button>
      <button class="btn btn-ghost" type="button" onclick="closeModal()">Close</button>
    </div>
    <div id="m-share"></div>`);
};
window.doShareTeamCards=async(id,name,channel)=>{
  const phone=($('#share-wa-phone')?.value||'').replace(/\s+/g,'');
  const email=$('#share-email')?.value||'';
  const note=$('#share-note')?.value||'';
  const r=await fetch('/api/teams/'+id+'/share-cards',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({channel,phone,email,note})}).then(r=>r.json());
  if(!r.success){msg($('#m-share'),r.error||'Failed','err');return}
  const text=r.message_text||('EHA team ID cards (PDF) for '+name+': '+r.share_url);
  if(channel==='whatsapp'){
    const num=phone.replace(/[^0-9]/g,'');
    const url=num?('https://wa.me/'+num+'?text='+encodeURIComponent(text)):('https://wa.me/?text='+encodeURIComponent(text));
    window.open(url,'_blank');
    msg($('#m-share'),'WhatsApp opened. Link valid until '+r.expires_at,'ok');
  } else {
    const sub=encodeURIComponent('EHA ID cards – '+name+' ('+r.season_label+')');
    const body=encodeURIComponent(text);
    const mail=email?('mailto:'+email+'?subject='+sub+'&body='+body):('mailto:?subject='+sub+'&body='+body);
    window.location.href=mail;
    msg($('#m-share'),'Email draft opened. Link valid until '+r.expires_at,'ok');
  }
};

window.showPlayer=async id=>{
  const r=await fetch('/api/players/'+id).then(r=>r.json()); if(!r.success)return;
  const p=r.player, stats=r.stats||[], totals=r.totals||{};
  let edit='';
  if(USER?.role==='ADMIN'){
    edit=`<div style="margin-top:12px"><h4 style="color:var(--p)">Admin: edit bio</h4>
      <form id="f-edit-player" onsubmit="return savePlayerBio(event,${p.player_id})">
        <label>Name</label><input name="legal_name" value="${p.legal_name||''}">
        <label>Phone</label><input name="phone" value="${p.phone||''}">
        <label>Email</label><input name="email" value="${p.email||''}">
        <label>Address</label><input name="address" value="${p.address||''}">
        <label>National ID</label><input name="national_id" value="${p.national_id||''}">
        <label>Nationality</label><input name="nationality" value="${p.nationality||''}">
        <label>Region</label><select name="region">
          ${['Hhohho','Manzini','Lubombo','Shiselweni','National'].map(x=>`<option ${p.region===x?'selected':''}>${x}</option>`).join('')}
        </select>
        <label>Height cm</label><input name="height_cm" type="number" value="${p.height_cm||''}">
        <label>Weight kg</label><input name="weight_kg" type="number" value="${p.weight_kg||''}">
        <label>Blood type</label><input name="blood_type" value="${p.blood_type||''}">
        <label>Emergency contact</label><input name="emergency_contact_name" value="${p.emergency_contact_name||''}">
        <label>Emergency phone</label><input name="emergency_contact_phone" value="${p.emergency_contact_phone||''}">
        <label>Medical notes</label><textarea name="medical_notes">${p.medical_notes||''}</textarea>
        <label>New photo</label><input name="photo" type="file" accept="image/*">
        <button class="btn" type="submit">Save bio</button>
      </form><div id="m-edit-player"></div></div>`;
  }
  let statsHtml=`<div class="section-label">Season stats</div>
    <div class="profile-row"><span class="k">Goals</span><span class="v">${totals.goals||0}</span></div>
    <div class="profile-row"><span class="k">Assists</span><span class="v">${totals.assists||0}</span></div>
    <div class="profile-row"><span class="k">Saves</span><span class="v">${totals.saves||0}</span></div>
    <div class="profile-row"><span class="k">Minutes</span><span class="v">${totals.minutes_played||0}</span></div>`;
  if(USER?.role==='ADMIN'||USER?.role==='OFFICER'||(USER?.role==='COACH'&&USER.team_id===p.team_id)){
    statsHtml+=`<form id="f-stat" onsubmit="return addStat(event,${p.player_id})" style="margin-top:10px">
      <label>Match date</label><input name="match_date" type="date" required>
      <label>Opponent</label><input name="opponent">
      <label>Goals</label><input name="goals" type="number" value="0">
      <label>Assists</label><input name="assists" type="number" value="0">
      <label>Saves</label><input name="saves" type="number" value="0">
      <label>Minutes</label><input name="minutes_played" type="number" value="0">
      <button class="btn btn-sm" type="submit">Add match stats</button></form><div id="m-stat"></div>`;
  }
  openModal(`<img class="avatar-lg" src="${photoUrl(p.photo_path)}" alt="">
    <h3>${p.legal_name}</h3>
    <div class="profile-row"><span class="k">License</span><span class="v">${p.global_license_number||'–'}</span></div>
    <div class="profile-row"><span class="k">Team</span><span class="v">${p.team_name||'–'}</span></div>
    <div class="profile-row"><span class="k">Region</span><span class="v">${p.region||'–'}</span></div>
    <div class="profile-row"><span class="k">Position</span><span class="v">${p.preferred_position||'–'} · #${p.jersey_number||'–'}</span></div>
    <div class="profile-row"><span class="k">DOB / Gender</span><span class="v">${p.date_of_birth||'–'} / ${p.gender||'–'}</span></div>
    <div class="profile-row"><span class="k">Hand</span><span class="v">${p.dominant_hand||'–'}</span></div>
    <div class="profile-row"><span class="k">Nationality</span><span class="v">${p.nationality||'–'}</span></div>
    <div class="profile-row"><span class="k">ID</span><span class="v">${p.national_id||'–'}</span></div>
    <div class="profile-row"><span class="k">Phone / Email</span><span class="v">${p.phone||'–'} / ${p.email||'–'}</span></div>
    <div class="profile-row"><span class="k">Address</span><span class="v">${p.address||'–'}</span></div>
    <div class="profile-row"><span class="k">Ht / Wt</span><span class="v">${p.height_cm||'–'} cm / ${p.weight_kg||'–'} kg</span></div>
    <div class="profile-row"><span class="k">Blood</span><span class="v">${p.blood_type||'–'}</span></div>
    <div class="profile-row"><span class="k">Emergency</span><span class="v">${p.emergency_contact_name||'–'} ${p.emergency_contact_phone||''}</span></div>
    <div class="profile-row"><span class="k">Registration</span><span class="v">${p.registration_status||'APPROVED'}</span></div>
    <div class="profile-row"><span class="k">Fee</span><span class="v">${p.player_fee_status||'–'}</span></div>
    ${p.rejection_reason?`<div class="profile-row"><span class="k">Rejection</span><span class="v">${p.rejection_reason}</span></div>`:''}
    ${(()=>{
      if(!p.registration_id||p.registration_status!=='PENDING_APPROVAL')return '';
      const feePaid=p.player_fee_status==='PAID';
      const canPay=USER?.role==='ADMIN'||USER?.role==='OFFICER'||
        ((USER?.role==='COACH'||USER?.role==='MANAGER')&&USER.team_id===p.team_id);
      let btns='';
      if(!feePaid&&p.player_fee_status!=='AWAITING_CONFIRMATION'&&canPay)
        btns+=`<button class="btn btn-a" onclick="closeModal();openPayRegModal(${p.registration_id},'${(p.legal_name||'').replace(/'/g,"\\'")}')">Pay fee (MoMo / EFT / Cash)</button>`;
      if(p.player_fee_status==='AWAITING_CONFIRMATION'&&p.pop_path)
        btns+=`<p style="font-size:.82rem"><a href="${p.pop_path}" target="_blank">View submitted POP</a></p>`;
      if(p.player_fee_status==='AWAITING_CONFIRMATION'&&USER?.role==='ADMIN')
        btns+=`<button class="btn" onclick="confirmCashPop(${p.registration_id});closeModal()">Confirm cash POP</button>`;
      if(USER?.role==='ADMIN'){
        if(feePaid) btns+=`<button class="btn" onclick="approveReg(${p.registration_id});closeModal()">Approve registration</button>`;
        else btns+=`<p style="font-size:.82rem;color:var(--muted)">Approve locked until coach/manager pays the fee.</p>`;
        btns+=`<button class="btn btn-danger" onclick="rejectReg(${p.registration_id});closeModal()">Reject</button>`;
      }
      return btns?`<div class="profile-actions no-print" style="margin-top:10px">${btns}</div>`:'';
    })()}
    ${statsHtml}${edit}
    ${(USER?.role==='ADMIN'||USER?.role==='OFFICER')?`<button class="btn btn-a no-print" style="margin-top:10px" onclick="printPlayerCard(${p.player_id})">Print / Create ID card</button>`:''}
    <button class="btn no-print" style="margin-top:8px" onclick="closeModal()">Close</button>`);
};
window.savePlayerBio=async(e,id)=>{
  e.preventDefault();const fd=new FormData(e.target);
  const r=await fetch('/api/players/'+id,{method:'PATCH',body:fd}).then(r=>r.json());
  msg($('#m-edit-player'),r.success?r.message:r.error,r.success?'ok':'err');
  if(r.success){loadPlayers();setTimeout(()=>showPlayer(id),500)}
  return false;
};
window.addStat=async(e,id)=>{
  e.preventDefault();const body=Object.fromEntries(new FormData(e.target));
  ['goals','assists','saves','minutes_played'].forEach(k=>body[k]=+body[k]||0);
  const r=await fetch('/api/players/'+id+'/stats',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json());
  msg($('#m-stat'),r.success?r.message:r.error,r.success?'ok':'err');
  if(r.success) setTimeout(()=>showPlayer(id),400);
  return false;
};

window.showCoach=async id=>{
  const r=await fetch('/api/coaches/'+id).then(r=>r.json()); if(!r.success)return;
  const c=r.coach;
  const isSelf=USER?.role==='COACH' && USER.user_id===c.user_id;
  const canEdit=USER?.role==='ADMIN'||USER?.role==='OFFICER'||isSelf;
  const canPrint=USER?.role==='ADMIN'||USER?.role==='OFFICER';
  let edit='';
  if(canEdit){
    const teamField=USER?.role==='COACH'
      ? `<input type="hidden" name="team_id" value="${c.team_id||''}"><div class="profile-row"><span class="k">Team</span><span class="v">${c.team_name||'–'} (fixed)</span></div>`
      : `<label>Team</label><select name="team_id" id="edit-coach-team"></select>`;
    edit=`<div class="profile-edit no-print"><h4>Edit coach</h4>
      <form id="f-edit-coach" onsubmit="return saveCoach(event,${c.coach_id})">
        <label>Full name</label><input name="legal_name" value="${(c.legal_name||'').replace(/"/g,'&quot;')}" required>
        <label>Phone</label><input name="phone" value="${(c.phone||'').replace(/"/g,'&quot;')}">
        <label>Email</label><input name="email" value="${(c.email||'').replace(/"/g,'&quot;')}">
        <label>Gender</label><select name="gender"><option value="">–</option>
          <option ${c.gender==='Male'?'selected':''}>Male</option><option ${c.gender==='Female'?'selected':''}>Female</option></select>
        <label>National ID</label><input name="national_id" value="${(c.national_id||'').replace(/"/g,'&quot;')}">
        <label>Date of birth</label><input name="date_of_birth" type="date" value="${c.date_of_birth||''}">
        <label>Address</label><input name="address" value="${(c.address||'').replace(/"/g,'&quot;')}">
        <label>Region</label><select name="region">
          ${['Hhohho','Manzini','Lubombo','Shiselweni','National'].map(x=>`<option ${c.region===x?'selected':''}>${x}</option>`).join('')}
        </select>
        <label>Qualifications</label><input name="qualifications" value="${(c.qualifications||'').replace(/"/g,'&quot;')}">
        ${teamField}
        <button class="btn" type="submit">Save changes</button>
      </form></div>`;
  }
  openModal(`<div class="profile-hero"><img class="avatar-lg" src="${photoUrl(c.photo_path)}" alt=""><div class="profile-badge">Coach</div></div>
    <h3 class="profile-name">${c.legal_name}</h3>
    <div class="profile-row"><span class="k">License</span><span class="v">${c.license_number||'–'}</span></div>
    <div class="profile-row"><span class="k">Team</span><span class="v">${c.team_name||'–'}</span></div>
    <div class="profile-row"><span class="k">Region</span><span class="v">${c.region||'–'}</span></div>
    <div class="profile-row"><span class="k">Phone / Email</span><span class="v">${c.phone||'–'} / ${c.email||'–'}</span></div>
    <div class="profile-row"><span class="k">DOB / Gender</span><span class="v">${c.date_of_birth||'–'} / ${c.gender||'–'}</span></div>
    <div class="profile-row"><span class="k">National ID</span><span class="v">${c.national_id||'–'}</span></div>
    <div class="profile-row"><span class="k">Address</span><span class="v">${c.address||'–'}</span></div>
    <div class="profile-row"><span class="k">Qualifications</span><span class="v">${c.qualifications||'–'}</span></div>
    <div class="profile-row"><span class="k">Login</span><span class="v">@${c.username||'–'}</span></div>
    ${edit}
    <div class="profile-actions no-print">
      ${canPrint?`<button class="btn btn-a" onclick="printCoachCard(${c.coach_id})">Print ID card</button>`:''}
      <button class="btn btn-ghost" onclick="closeModal()">Close</button>
    </div>`);
  if(canEdit && USER?.role!=='COACH'){
    fetch('/api/teams').then(r=>r.json()).then(tr=>{
      const sel=$('#edit-coach-team'); if(!sel)return;
      sel.innerHTML=(tr.teams||[]).map(tm=>`<option value="${tm.team_id}" ${tm.team_id===c.team_id?'selected':''}>${tm.team_name} (${tm.region||''})</option>`).join('');
    });
  }
};
window.saveCoach=async(e,id)=>{
  e.preventDefault();
  const body=Object.fromEntries(new FormData(e.target));
  if(body.team_id) body.team_id=+body.team_id;
  const r=await fetch('/api/coaches/'+id,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json());
  alert(r.message||r.error);
  if(r.success){loadCoaches();setTimeout(()=>showCoach(id),300)}
  return false;
};

async function loadPlayers(){
  const r=await fetch('/api/players').then(r=>r.json());
  const isAdmin=USER?.role==='ADMIN';
  $('#list-players').innerHTML=(r.players||[]).map(p=>{
    const rs=p.registration_status||'APPROVED';
    const rsLabel=rs==='PENDING_APPROVAL'?'AWAITING APPROVAL':rs;
    const rsClass=rs==='APPROVED'?'PAID':(rs==='REJECTED'?'FAILED':'PENDING');
    return `<div class="list-item"><div class="row" onclick="showPlayer(${p.player_id})">
      <img class="avatar" src="${photoUrl(p.photo_path)}"><div style="flex:1;margin-left:10px">
      <div class="title">${p.legal_name}</div>
      <div class="meta">${p.global_license_number||''} · ${p.team_name||'–'} · ${p.preferred_position||''}</div>
      <div class="badge-row"><span class="status ${rsClass}">${rsLabel}</span>
        <span class="status ${p.player_fee_status||'PENDING'}">Fee: ${p.player_fee_status||'–'}</span></div>
      </div>
    </div>
    ${isAdmin?`<button class="btn btn-sm btn-a" style="margin-top:4px" onclick="event.stopPropagation();deletePlayer(${p.player_id})">Delete player</button>`:''}
    </div>`;
  }).join('')||'<p class="empty-hint">None</p>';
  loadPendingRegs();
}
async function loadPendingRegs(){
  const box=$('#list-pending-regs'); if(!box)return;
  if(!(USER?.role==='ADMIN'||USER?.role==='OFFICER'||USER?.role==='COACH'||USER?.role==='MANAGER')){
    const card=$('#pending-regs-card'); if(card) card.classList.add('hidden');
    box.innerHTML='';return}
  const card=$('#pending-regs-card'); if(card) card.classList.remove('hidden');
  try{
    const r=await fetch('/api/registrations/pending').then(r=>r.json());
    if(!r.success){box.innerHTML='<p class="empty-hint">Could not load.</p>';return}
    if(!(r.pending||[]).length){box.innerHTML='<p class="empty-hint">No pending registrations.</p>';return}
    box.innerHTML=r.pending.map(x=>{
      const feePaid=(x.player_fee_status==='PAID');
      const awaiting=(x.player_fee_status==='AWAITING_CONFIRMATION');
      const canPay=USER?.role==='ADMIN'||USER?.role==='OFFICER'||
        ((USER?.role==='COACH'||USER?.role==='MANAGER')&&USER.team_id===x.team_id);
      const feeLabel=feePaid?'PAID':(awaiting?'POP PENDING':'UNPAID');
      const feeClass=feePaid?'PAID':'PENDING';
      return `<div class="list-item" style="cursor:default">
        <div class="row" onclick="showPlayer(${x.player_id})" style="cursor:pointer">
          <img class="avatar" src="${photoUrl(x.photo_path)}">
          <div style="flex:1;margin-left:10px">
            <div class="title">${x.legal_name}</div>
            <div class="meta">${x.global_license_number||''} · ${x.team_name||''} · ${x.region||''}
              ${x.payment_method?' · '+x.payment_method:''}</div>
            <div class="badge-row">
              <span class="status PENDING">AWAITING APPROVAL</span>
              <span class="status ${feeClass}">Fee: ${feeLabel}</span>
            </div>
          </div>
        </div>
        <div style="margin-top:8px">
          ${(!feePaid&&!awaiting&&canPay)?`<button class="btn btn-sm btn-a" onclick="openPayRegModal(${x.registration_id},'${(x.legal_name||'').replace(/'/g,"\\'")}')">Pay fee (MoMo / EFT / Cash)</button>`:''}
          ${awaiting&&x.pop_path?`<p style="font-size:.78rem;margin:4px 0"><a href="${x.pop_path}" target="_blank">View POP</a></p>`:''}
          ${awaiting&&USER?.role==='ADMIN'?`<button class="btn btn-sm" onclick="confirmCashPop(${x.registration_id})">Confirm cash POP</button>`:''}
          ${USER?.role==='ADMIN'?(feePaid
            ?`<button class="btn btn-sm" onclick="approveReg(${x.registration_id})">Approve (add to roster)</button>`
            :`<span style="font-size:.78rem;color:var(--muted)">Approve locked until fee is paid</span>`):''}
          ${USER?.role==='ADMIN'?`<button class="btn btn-sm btn-a" onclick="rejectReg(${x.registration_id})">Reject</button>`:''}
        </div>
      </div>`;
    }).join('');
  }catch{box.innerHTML='';}
}
window.openPayRegModal=(id,name)=>{
  openModal(`<h3>Pay registration fee</h3>
    <p style="font-size:.85rem;color:var(--muted);margin-bottom:10px">Player: <b>${name||''}</b> · Amount: <b>E${FEES.player}</b></p>
    <form id="f-pay-reg" onsubmit="return submitPayReg(event,${id})">
      <label>Payment method *</label>
      <select name="payment_method" id="pay-method" required>
        <option value="MOBILE_MONEY">Mobile Money (MoMo / Mpamba)</option>
        <option value="EFT">EFT / Bank transfer</option>
        <option value="CASH">Cash (upload Proof of Payment)</option>
      </select>
      <label>Phone (MoMo)</label><input name="phone" type="tel" placeholder="2687xxxxxxx">
      <label>EFT reference (if EFT)</label><input name="eft_reference" placeholder="Bank ref">
      <div id="pop-wrap" class="hidden">
        <label>Proof of Payment (image) *</label>
        <input name="pop" type="file" accept="image/*">
      </div>
      <button class="btn" type="submit">Submit payment</button>
    </form>
    <button class="btn btn-ghost" style="margin-top:8px" onclick="closeModal()">Cancel</button>`);
  $('#pay-method')?.addEventListener('change',e=>{
    const w=$('#pop-wrap'); if(!w)return;
    w.classList.toggle('hidden', e.target.value!=='CASH');
  });
};
window.submitPayReg=async(e,id)=>{
  e.preventDefault();
  const fd=new FormData(e.target);
  const method=fd.get('payment_method');
  if(method==='CASH' && !fd.get('pop')?.size && !fd.get('pop')?.name){
    alert('Cash requires a POP image'); return false;
  }
  const r=await fetch('/api/registrations/'+id+'/pay-fee',{method:'POST',body:fd}).then(r=>r.json());
  alert(r.message||r.error);
  if(r.success){closeModal();loadPlayers();loadTeamChips();loadDash();loadNotifs()}
  return false;
};
window.confirmCashPop=async id=>{
  if(!confirm('Confirm cash POP and mark fee as paid?'))return;
  const r=await fetch('/api/registrations/'+id+'/confirm-cash',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).then(r=>r.json());
  alert(r.message||r.error); if(r.success){loadPlayers();loadNotifs()}
};
window.payPlayerRegFee=id=>openPayRegModal(id,'Player');
window.sendFeeReminders=async()=>{
  const r=await fetch('/api/registrations/remind-fees',{method:'POST'}).then(r=>r.json());
  alert(r.message||r.error); if(r.success)loadNotifs();
};
window.approveReg=async id=>{
  if(!confirm('Approve this player? Fee must already be paid.'))return;
  const r=await fetch('/api/registrations/'+id+'/approve',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).then(r=>r.json());
  alert(r.message||r.error); if(r.success){loadPlayers();loadTeamChips();loadDash();loadNotifs()}
};
window.rejectReg=async id=>{
  const reason=prompt('Rejection reason (optional):','Incomplete documents')||'Rejected by Admin';
  const r=await fetch('/api/registrations/'+id+'/reject',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reason})}).then(r=>r.json());
  alert(r.message||r.error); if(r.success){loadPlayers();loadTeamChips();loadDash();loadNotifs()}
};
async function loadCoaches(){
  const r=await fetch('/api/coaches').then(r=>r.json());
  const isAdmin=USER?.role==='ADMIN';
  $('#list-coaches').innerHTML=(r.coaches||[]).map(c=>`
    <div class="list-item"><div class="row" onclick="showCoach(${c.coach_id})">
      <img class="avatar" src="${photoUrl(c.photo_path)}"><div style="flex:1;margin-left:10px">
      <div class="title">${c.legal_name}</div><div class="meta">${c.team_name||'–'} · ${c.license_number||''}</div></div>
    </div>
    ${isAdmin?`<button class="btn btn-sm btn-a" style="margin-top:4px" onclick="event.stopPropagation();deleteCoach(${c.coach_id})">Delete coach</button>`:''}
    </div>`).join('')||'<p style="color:#888">None</p>';
}
async function loadManagers(){
  try{
    const r=await fetch('/api/managers').then(r=>r.json());
    $('#list-managers').innerHTML=(r.managers||[]).map(m=>`
      <div class="list-item" onclick="showManager(${m.manager_id})"><div class="row">
        <img class="avatar" src="${photoUrl(m.photo_path)}"><div style="flex:1;margin-left:10px">
        <div class="title">${m.legal_name}</div><div class="meta">${m.team_name||'–'} · ${m.license_number||''} · @${m.username||''}</div></div>
      </div></div>`).join('')||'<p class="empty-hint">No managers registered yet.</p>';
  }catch{const el=$('#list-managers'); if(el) el.innerHTML='';}
}
window.showManager=async id=>{
  const r=await fetch('/api/managers/'+id).then(r=>r.json()); if(!r.success){alert(r.error||'Not found');return}
  const m=r.manager;
  const canEdit=USER?.role==='ADMIN'||USER?.role==='OFFICER';
  const canDel=USER?.role==='ADMIN';
  let edit='';
  if(canEdit){
    edit=`<div class="profile-edit no-print">
      <h4>Edit manager</h4>
      <form id="f-edit-manager" onsubmit="return saveManager(event,${m.manager_id})">
        <label>Full name</label><input name="legal_name" value="${(m.legal_name||'').replace(/"/g,'&quot;')}" required>
        <label>Phone</label><input name="phone" value="${(m.phone||'').replace(/"/g,'&quot;')}">
        <label>Email</label><input name="email" value="${(m.email||'').replace(/"/g,'&quot;')}">
        <label>Gender</label><select name="gender"><option value="">–</option>
          <option ${m.gender==='Male'?'selected':''}>Male</option><option ${m.gender==='Female'?'selected':''}>Female</option></select>
        <label>National ID</label><input name="national_id" value="${(m.national_id||'').replace(/"/g,'&quot;')}">
        <label>Date of birth</label><input name="date_of_birth" type="date" value="${m.date_of_birth||''}">
        <label>Address</label><input name="address" value="${(m.address||'').replace(/"/g,'&quot;')}">
        <label>Region</label><select name="region">
          ${['Hhohho','Manzini','Lubombo','Shiselweni','National'].map(x=>`<option ${m.region===x?'selected':''}>${x}</option>`).join('')}
        </select>
        <label>Team</label><select name="team_id" id="edit-mgr-team"></select>
        <button class="btn" type="submit">Save changes</button>
      </form></div>`;
  }
  openModal(`<div class="profile-hero">
      <img class="avatar-lg" src="${photoUrl(m.photo_path)}" alt="">
      <div class="profile-badge">Manager</div>
    </div>
    <h3 class="profile-name">${m.legal_name}</h3>
    <div class="profile-row"><span class="k">License</span><span class="v">${m.license_number||'–'}</span></div>
    <div class="profile-row"><span class="k">Team</span><span class="v">${m.team_name||'–'}</span></div>
    <div class="profile-row"><span class="k">Region</span><span class="v">${m.region||'–'}</span></div>
    <div class="profile-row"><span class="k">Phone</span><span class="v">${m.phone||'–'}</span></div>
    <div class="profile-row"><span class="k">Email</span><span class="v">${m.email||'–'}</span></div>
    <div class="profile-row"><span class="k">DOB / Gender</span><span class="v">${m.date_of_birth||'–'} / ${m.gender||'–'}</span></div>
    <div class="profile-row"><span class="k">National ID</span><span class="v">${m.national_id||'–'}</span></div>
    <div class="profile-row"><span class="k">Address</span><span class="v">${m.address||'–'}</span></div>
    <div class="profile-row"><span class="k">Login</span><span class="v">@${m.username||'–'}</span></div>
    ${edit}
    <div class="profile-actions no-print">
      ${(USER?.role==='ADMIN'||USER?.role==='OFFICER')?`<button class="btn btn-a" onclick="printManagerCard(${m.manager_id})">Print ID card</button>`:''}
      ${canDel?`<button class="btn btn-danger" onclick="deleteManager(${m.manager_id});closeModal()">Delete manager</button>`:''}
      <button class="btn btn-ghost" onclick="closeModal()">Close</button>
    </div>`);
  if(canEdit){
    fetch('/api/teams').then(r=>r.json()).then(tr=>{
      const sel=$('#edit-mgr-team');
      if(!sel)return;
      sel.innerHTML=(tr.teams||[]).map(tm=>`<option value="${tm.team_id}" ${tm.team_id===m.team_id?'selected':''}>${tm.team_name} (${tm.division||''} ${tm.category||''})</option>`).join('');
    });
  }
};
window.saveManager=async(e,id)=>{
  e.preventDefault();
  const body=Object.fromEntries(new FormData(e.target));
  if(body.team_id) body.team_id=+body.team_id;
  const r=await fetch('/api/managers/'+id,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json());
  alert(r.message||r.error);
  if(r.success){loadManagers();setTimeout(()=>showManager(id),300)}
  return false;
};
window.printManagerCard=async id=>{
  const r=await fetch('/api/managers/'+id+'/card').then(r=>r.json());
  if(!r.success){alert(r.error);return}
  openPrintCard(r.html);
};
window.editTeam=tm=>{
  $('#edit-team-id').value=tm.team_id;
  $('#team-name').value=tm.team_name||'';
  $('#team-division').value=tm.division||'National';
  $('#team-category').value=tm.category||'Male';
  if($('#team-region')) $('#team-region').value=tm.region||'National';
  $('#team-manager').value=tm.manager_name||'';
  $('#team-phone').value=tm.manager_phone||'';
  $('#team-jersey').value=tm.primary_jersey_color||'';
  $('#btn-team-save').textContent='Save team changes';
  $('#team-form-title').textContent='Edit Team';
  const c=$('#btn-team-cancel'); if(c) c.style.display='block';
  document.querySelector('[data-p="teams"]')?.click();
};
window.deleteTeam=async id=>{
  if(!confirm('Delete this team? Active players must be removed first.'))return;
  const r=await fetch('/api/teams/'+id,{method:'DELETE'}).then(r=>r.json());
  alert(r.message||r.error); if(r.success){loadTeamChips();loadDash()}
};
window.deletePlayer=async id=>{
  if(!confirm('Delete this player permanently?'))return;
  const r=await fetch('/api/players/'+id,{method:'DELETE'}).then(r=>r.json());
  alert(r.message||r.error); if(r.success){loadPlayers();loadTeamChips()}
};
window.deleteCoach=async id=>{
  if(!confirm('Delete this coach?'))return;
  const r=await fetch('/api/coaches/'+id,{method:'DELETE'}).then(r=>r.json());
  alert(r.message||r.error); if(r.success)loadCoaches();
};
window.deleteManager=async id=>{
  if(!confirm('Delete this manager?'))return;
  const r=await fetch('/api/managers/'+id,{method:'DELETE'}).then(r=>r.json());
  alert(r.message||r.error); if(r.success)loadManagers();
};

async function loadTransfers(){
  const r=await fetch('/api/transfers').then(r=>r.json());
  const list=$('#list-transfers');
  if(!r.transfers?.length){list.innerHTML='<p style="color:#888">No transfers.</p>';return}
  list.innerHTML=r.transfers.map(t=>{
    let actions='';
    const st=t.status||'PENDING';
    if((USER.role==='COACH'||USER.role==='ADMIN') && st==='PENDING'){
      if(USER.role==='COACH'&&USER.team_id===t.from_team_id&&!t.from_coach_agreed)
        actions+=`<button class="btn btn-sm" onclick="agreeTransfer(${t.transfer_id},'from')">Agree (releasing)</button>`;
      if(USER.role==='COACH'&&USER.team_id===t.to_team_id&&!t.to_coach_agreed)
        actions+=`<button class="btn btn-sm" onclick="agreeTransfer(${t.transfer_id},'to')">Agree (receiving)</button>`;
    }
    const canPay = t.from_coach_agreed&&t.to_coach_agreed&&t.fee_paid_status!=='PAID'&&st!=='APPROVED';
    if(canPay && (USER.role==='ADMIN'||USER.role==='OFFICER'||(USER.role==='COACH'&&USER.team_id===t.fee_payer_team_id)))
      actions+=`<button class="btn btn-sm btn-a" onclick="payTransfer(${t.transfer_id})">Pay transfer fee (my team)</button>`;
    if(USER.role==='ADMIN'&&t.from_coach_agreed&&t.to_coach_agreed&&t.fee_paid_status==='PAID'&&!t.approved_by_admin)
      actions+=`<button class="btn btn-sm btn-a" onclick="approveTransfer(${t.transfer_id})">Admin approve</button>`;
    return `<div class="list-item" style="cursor:default">
      <div class="title">#${t.transfer_id} · ${t.player_name} · ${t.player_license||''}</div>
      <div class="meta">${t.request_type||'PULL'} · ${t.from_team||'?'} → ${t.to_team||'?'} · fee team ID ${t.fee_payer_team_id||'–'}</div>
      <div class="badge-row">
        <span class="status ${st}">${st}</span>
        <span class="status ${t.from_coach_agreed?'AGREED':'PENDING'}">From coach: ${t.from_coach_agreed?'✓':'…'}</span>
        <span class="status ${t.to_coach_agreed?'AGREED':'PENDING'}">To coach: ${t.to_coach_agreed?'✓':'…'}</span>
        <span class="status ${t.fee_paid_status}">Fee: ${t.fee_paid_status}</span>
        <span class="status ${t.approved_by_admin?'APPROVED':'PENDING'}">Admin: ${t.approved_by_admin?'✓':'…'}</span>
      </div>
      <div style="margin-top:6px">${actions}</div>
    </div>`;
  }).join('');
}
window.agreeTransfer=async(id,side)=>{
  const r=await fetch('/api/transfers/'+id+'/agree',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({side})}).then(r=>r.json());
  alert(r.message||r.error); loadTransfers(); loadNotifs();
};
window.payTransfer=async id=>{
  const phone=prompt('Payer phone (MoMo):','26876'); if(!phone)return;
  const r=await fetch('/api/transfers/'+id+'/pay',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({phone})}).then(r=>r.json());
  alert(r.message||r.error); loadTransfers(); loadNotifs();
};
window.approveTransfer=async id=>{
  const r=await fetch('/api/transfers/'+id+'/approve',{method:'POST'}).then(r=>r.json());
  alert(r.message||r.error); loadTransfers(); loadPlayers(); loadTeamChips(); loadNotifs();
};

async function loadSettingsForm(){
  if(USER?.role!=='ADMIN')return;
  const r=await fetch('/api/settings').then(r=>r.json()); if(!r.success)return;
  const f=$('#f-fees'),w=$('#f-window');
  if(f){f.team_fee.value=r.settings.team_fee;f.player_fee.value=r.settings.player_fee;f.transfer_fee.value=r.settings.transfer_fee}
  if(w){w.transfer_window_active.value=r.settings.transfer_window_active;w.transfer_window_type.value=r.settings.transfer_window_type}
  const s=$('#f-season');
  if(s){s.season_start_date.value=r.settings.season_start_date||'';s.season_end_date.value=r.settings.season_end_date||''}
  loadDbTables();
}
async function loadDbTables(){
  if(USER?.role!=='ADMIN')return;
  try{
    const r=await fetch('/api/admin/db/tables').then(r=>r.json());
    const sel=$('#db-table-select');
    if(!sel||!r.success)return;
    sel.innerHTML='<option value="">Select a table…</option>'+(r.tables||[]).map(tb=>`<option value="${tb}">${tb}</option>`).join('');
  }catch{}
}
async function loadDbEntries(){
  const name=$('#db-table-select')?.value;
  if(!name){alert('Select a table');return}
  const r=await fetch('/api/admin/db/table/'+encodeURIComponent(name)).then(r=>r.json());
  if(!r.success){alert(r.error);return}
  $('#db-meta').textContent=`${r.table} · ${r.count} row(s)`+(r.truncated?` · showing first ${r.rows.length}`:'');
  if(!r.columns?.length){$('#db-view').innerHTML='<p style="color:#888">Empty table</p>';return}
  let html='<table><thead><tr>'+r.columns.map(c=>`<th>${c}</th>`).join('')+'</tr></thead><tbody>';
  for(const row of r.rows){
    html+='<tr>'+r.columns.map(c=>{
      let v=row[c];
      if(v===null||v===undefined)v='';
      else v=String(v);
      // hide password hashes in UI
      if(c.toLowerCase().includes('password')) v='••••••••';
      return `<td title="${v.replace(/"/g,'&quot;')}">${v.length>40?v.slice(0,40)+'…':v}</td>`;
    }).join('')+'</tr>';
  }
  html+='</tbody></table>';
  $('#db-view').innerHTML=html;
}
$('#btn-db-load')?.addEventListener('click',loadDbEntries);
$('#db-table-select')?.addEventListener('change',()=>{ if($('#db-table-select').value) loadDbEntries(); });

$('#f-team')?.addEventListener('submit',async e=>{e.preventDefault();
  const body=Object.fromEntries(new FormData(e.target));
  const tid=body.team_id; delete body.team_id;
  const isEdit=USER?.role==='ADMIN'&&tid;
  const r=await fetch(isEdit?('/api/teams/'+tid):'/api/teams',{method:isEdit?'PATCH':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json());
  msg($('#m-team'),r.success?r.message:r.error,r.success?'ok':'err');
  if(r.success){e.target.reset();$('#edit-team-id').value='';$('#btn-team-save').textContent='Create Team';
    $('#team-form-title').textContent='Register Team';const c=$('#btn-team-cancel');if(c)c.style.display='none';loadTeamChips();loadDash()}});
$('#btn-team-cancel')?.addEventListener('click',()=>{
  $('#f-team').reset(); $('#edit-team-id').value='';
  $('#btn-team-save').textContent='Create Team'; $('#team-form-title').textContent='Register Team';
  $('#btn-team-cancel').style.display='none';
});
$('#f-manager')?.addEventListener('submit',async e=>{e.preventDefault();const fd=new FormData(e.target);
  const r=await fetch('/api/managers',{method:'POST',body:fd}).then(r=>r.json());
  msg($('#m-manager'),r.success?r.message:r.error,r.success?'ok':'err'); if(r.success){e.target.reset();loadManagers()}});
$('#f-player')?.addEventListener('submit',async e=>{e.preventDefault();const fd=new FormData(e.target);
  const r=await fetch('/api/players',{method:'POST',body:fd}).then(r=>r.json());
  msg($('#m-player'),r.success?r.message:r.error,r.success?'ok':'err'); if(r.success){e.target.reset();loadPlayers();loadTeamChips()}});
$('#f-coach')?.addEventListener('submit',async e=>{e.preventDefault();const fd=new FormData(e.target);
  const r=await fetch('/api/coaches',{method:'POST',body:fd}).then(r=>r.json());
  msg($('#m-coach'),r.success?r.message:r.error,r.success?'ok':'err'); if(r.success){e.target.reset();loadCoaches()}});
$('#f-transfer')?.addEventListener('submit',async e=>{e.preventDefault();
  const body=Object.fromEntries(new FormData(e.target));
  body.other_team_id=+body.other_team_id;
  const r=await fetch('/api/transfers/request',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json());
  msg($('#m-transfer'),r.success?r.message:r.error,r.success?'ok':'err'); if(r.success){e.target.reset();loadTransfers();loadNotifs()}});
$('#f-season')?.addEventListener('submit',async e=>{e.preventDefault();const body=Object.fromEntries(new FormData(e.target));
  const r=await fetch('/api/settings/season',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json());
  msg($('#m-season'),r.success?r.message:r.error,r.success?'ok':'err'); if(r.success)loadDash()});
$('#f-window')?.addEventListener('submit',async e=>{e.preventDefault();const body=Object.fromEntries(new FormData(e.target));
  const r=await fetch('/api/settings/window',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json());
  msg($('#m-window'),r.success?r.message:r.error,r.success?'ok':'err'); if(r.success)loadDash()});
$('#f-fees')?.addEventListener('submit',async e=>{e.preventDefault();const body=Object.fromEntries(new FormData(e.target));
  const r=await fetch('/api/settings/fees',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json());
  msg($('#m-fees'),r.success?r.message:r.error,r.success?'ok':'err'); if(r.success)loadDash()});

window.printPlayerCard=async id=>{
  const r=await fetch('/api/players/'+id+'/card').then(r=>r.json());
  if(!r.success){alert(r.error);return}
  openPrintCard(r.html);
};
window.printCoachCard=async id=>{
  const r=await fetch('/api/coaches/'+id+'/card').then(r=>r.json());
  if(!r.success){alert(r.error);return}
  openPrintCard(r.html);
};
function openPrintCard(html){
  openModal(`<div id="print-root">${html}</div>
    <button class="btn no-print" style="margin-top:12px" onclick="window.print()">Print card</button>
    <button class="btn btn-out no-print" style="margin-top:8px;width:100%" onclick="closeModal()">Close</button>`);
}

let XFER_PLAYERS=[];
async function loadXferPicker(){
  const teams=await fetch('/api/teams').then(r=>r.json());
  const tf=$('#xfer-team-filter'), to=$('#xfer-to-team');
  if(tf){tf.innerHTML='<option value="">All teams</option>'+(teams.teams||[]).map(t=>`<option value="${t.team_id}">${t.team_name}</option>`).join('')}
  if(to){to.innerHTML='<option value="">Select destination</option>'+(teams.teams||[]).map(t=>`<option value="${t.team_id}">${t.team_name} (ID ${t.team_id})</option>`).join('')}
  const pl=await fetch('/api/players').then(r=>r.json());
  XFER_PLAYERS=pl.players||[];
  renderXferList();
}
function renderXferList(){
  const q=($('#xfer-search')?.value||'').toLowerCase();
  const teamF=$('#xfer-team-filter')?.value||'';
  let list=XFER_PLAYERS;
  if(teamF) list=list.filter(p=>String(p.team_id)===String(teamF));
  if(q) list=list.filter(p=>(p.legal_name||'').toLowerCase().includes(q)||(p.global_license_number||'').toLowerCase().includes(q));
  // Coaches can only transfer their own players — still show others greyed with note
  const box=$('#xfer-player-list');
  if(!box)return;
  if(!list.length){box.innerHTML='<p style="color:#888;font-size:.85rem">No players.</p>';return}
  box.innerHTML=list.map(p=>{
    const own=USER?.role!=='COACH'||USER.team_id===p.team_id;
    return `<div class="list-item" style="opacity:${own?1:0.55}" onclick="pickXferPlayer(${p.player_id})">
      <div class="row">
        <img class="avatar" src="${photoUrl(p.photo_path)}">
        <div style="flex:1;margin-left:8px">
          <div class="title">${p.legal_name}</div>
          <div class="meta">${p.global_license_number||''} · ${p.team_name||'–'} · ${p.preferred_position||''}${own?'':' · view only'}</div>
        </div>
      </div></div>`;
  }).join('');
}
window.pickXferPlayer=id=>{
  const p=XFER_PLAYERS.find(x=>x.player_id===id); if(!p)return;
  const rtype=$('#xfer-type')?.value||'PULL';
  if(USER?.role==='COACH'){
    if(rtype==='OFFER' && USER.team_id!==p.team_id){
      alert('Offer: select a player from YOUR team.'); return;
    }
    if(rtype==='PULL' && USER.team_id===p.team_id){
      alert('Pull: select a player from ANOTHER team to bring to yours.'); return;
    }
  }
  $('#xfer-player-name').value=p.legal_name||'';
  $('#xfer-license').value=p.global_license_number||'';
  $('#xfer-from-team').value=(p.team_name||'')+' (ID '+(p.team_id||'')+')';
  const sel=$('#xfer-to-team');
  if(sel){
    if(rtype==='PULL' && USER?.role==='COACH'){
      // other team field = player's current team is source; destination is coach team (fixed)
      sel.value='';
      for(const o of sel.options){ if(String(o.value)===String(USER.team_id)){ /* skip own in list display */ } }
    }
    for(const o of sel.options){if(o.value&&String(o.value)!==String(p.team_id)){sel.value=o.value;break;}}
  }
  const hint = rtype==='PULL'
    ? `Pull ${p.legal_name} to your team — you will pay the transfer fee after both sides agree.`
    : `Offer ${p.legal_name} — receiving team pays the fee if they accept.`;
  msg($('#m-transfer'),hint,'info');
};
$('#xfer-type')?.addEventListener('change',()=>{
  const r=$('#xfer-type').value;
  $('#xfer-hint').textContent = r==='PULL'
    ? 'Pull: pick a player from another team → they join YOUR team → YOU pay the fee.'
    : 'Offer: pick YOUR player → choose receiving team → THEY pay the fee if they accept.';
});
$('#xfer-search')?.addEventListener('input',renderXferList);
$('#xfer-team-filter')?.addEventListener('change',renderXferList);

const _refreshAll=refreshAll;
refreshAll=function(){_refreshAll(); loadXferPicker()};

checkSession();
</script>
</body></html>'''

# ── API routes (continued in same file via remaining code) ──
@app.route("/")
def index():
    return render_template_string(HTML)

@app.route("/static/<path:path>")
def static_files(path):
    return send_from_directory(os.path.join(BASE, "static"), path)

@app.route("/api/login", methods=["POST"])
def login():
    d = request.json or {}
    user = q1("SELECT * FROM users WHERE username=? AND is_active=1", (d.get("username","").strip(),))
    if not user or user["password_hash"] != hash_pw(d.get("password","")):
        return jsonify(success=False, error="Invalid credentials"), 401
    session["user_id"] = user["user_id"]
    return jsonify(success=True, user={"user_id":user["user_id"],"username":user["username"],
        "full_name":user["full_name"],"role":user["role"],"team_id":user["team_id"]})

@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear(); return jsonify(success=True)

@app.route("/api/me")
def me():
    u = current_user()
    return (jsonify(success=True, user=u) if u else (jsonify(success=False), 401))

@app.route("/api/health")
def health():
    try:
        q1("SELECT 1"); return jsonify(status="ok", db="sqlite", season=SEASON, version="1.3.0")
    except Exception as e:
        return jsonify(status="error", error=str(e)), 503

@app.route("/api/finance")
@login_required
def finance():
    t = q1("SELECT COUNT(*) c FROM teams WHERE team_fee_status='PAID'")
    sy = season_start_year()
    p = q1("SELECT COUNT(*) c FROM annual_registrations WHERE season_year=? AND player_fee_status='PAID' AND is_active_roster=1",(sy,))
    if not p or p["c"]==0:
        p = q1("SELECT COUNT(*) c FROM annual_registrations WHERE season_year=? AND player_fee_status='PAID' AND is_active_roster=1",(SEASON,))
    teams_all = q1("SELECT COUNT(*) c FROM teams")
    players_all = q1("SELECT COUNT(*) c FROM annual_registrations WHERE is_active_roster=1 AND season_year IN (?,?)",(sy,SEASON))
    # distinct active players prefer current season
    players_all = q1("""SELECT COUNT(DISTINCT player_id) c FROM annual_registrations
        WHERE is_active_roster=1 AND season_year=?""",(sy,))
    if not players_all or players_all["c"]==0:
        players_all = q1("""SELECT COUNT(DISTINCT player_id) c FROM annual_registrations
            WHERE is_active_roster=1 AND season_year=?""",(SEASON,))
    coaches_all = q1("SELECT COUNT(*) c FROM coaches WHERE is_active=1")
    managers_all = q1("SELECT COUNT(*) c FROM managers WHERE is_active=1")
    # by region — prefer person's own region, else team region
    region_names = ["Hhohho","Manzini","Lubombo","Shiselweni","National","Unassigned"]
    # collect from DB any extra regions
    extra = q("""SELECT DISTINCT region FROM (
        SELECT region FROM teams UNION SELECT region FROM players
        UNION SELECT region FROM coaches UNION SELECT region FROM managers)""")
    for e in extra or []:
        rname = (e.get("region") or "").strip() or "Unassigned"
        if rname not in region_names:
            region_names.append(rname)
    regions = []
    for rname in region_names:
        if rname == "Unassigned":
            tcond = "(region IS NULL OR TRIM(region)='')"
            pjoin = """JOIN annual_registrations ar ON ar.player_id=p.player_id AND ar.is_active_roster=1 AND ar.season_year IN (?,?)
                       LEFT JOIN teams t2 ON t2.team_id=ar.team_id
                       WHERE (COALESCE(NULLIF(TRIM(p.region),''), NULLIF(TRIM(t2.region),'')) IS NULL)"""
        else:
            tcond = "region=?"
            pjoin = """JOIN annual_registrations ar ON ar.player_id=p.player_id AND ar.is_active_roster=1 AND ar.season_year IN (?,?)
                       LEFT JOIN teams t2 ON t2.team_id=ar.team_id
                       WHERE COALESCE(NULLIF(TRIM(p.region),''), NULLIF(TRIM(t2.region),''), 'Unassigned')=?"""
        if rname == "Unassigned":
            tc = q1(f"SELECT COUNT(*) c FROM teams WHERE {tcond}")
            pc = q1(f"SELECT COUNT(DISTINCT p.player_id) c FROM players p {pjoin}", (sy, SEASON))
            cc = q1("""SELECT COUNT(*) c FROM coaches c LEFT JOIN teams t2 ON t2.team_id=c.team_id
                WHERE c.is_active=1 AND (COALESCE(NULLIF(TRIM(c.region),''), NULLIF(TRIM(t2.region),'')) IS NULL)""")
            mc = q1("""SELECT COUNT(*) c FROM managers m LEFT JOIN teams t2 ON t2.team_id=m.team_id
                WHERE m.is_active=1 AND (COALESCE(NULLIF(TRIM(m.region),''), NULLIF(TRIM(t2.region),'')) IS NULL)""")
        else:
            tc = q1(f"SELECT COUNT(*) c FROM teams WHERE {tcond}", (rname,))
            pc = q1(f"SELECT COUNT(DISTINCT p.player_id) c FROM players p {pjoin}", (sy, SEASON, rname))
            cc = q1("""SELECT COUNT(*) c FROM coaches c LEFT JOIN teams t2 ON t2.team_id=c.team_id
                WHERE c.is_active=1 AND COALESCE(NULLIF(TRIM(c.region),''), NULLIF(TRIM(t2.region),''), 'Unassigned')=?""", (rname,))
            mc = q1("""SELECT COUNT(*) c FROM managers m LEFT JOIN teams t2 ON t2.team_id=m.team_id
                WHERE m.is_active=1 AND COALESCE(NULLIF(TRIM(m.region),''), NULLIF(TRIM(t2.region),''), 'Unassigned')=?""", (rname,))
        teams_c = tc["c"] if tc else 0
        players_c = pc["c"] if pc else 0
        coaches_c = cc["c"] if cc else 0
        managers_c = mc["c"] if mc else 0
        if teams_c or players_c or coaches_c or managers_c:
            regions.append({"region": rname, "teams": teams_c, "players": players_c, "coaches": coaches_c, "managers": managers_c})
    regions.sort(key=lambda x: (x["region"]=="Unassigned", x["region"]))
    return jsonify(
        teams_paid=t["c"] if t else 0,
        players_paid=p["c"] if p else 0,
        fees=fees(),
        season_label=season_label(),
        season_start_year=sy,
        totals={
            "teams": teams_all["c"] if teams_all else 0,
            "players": players_all["c"] if players_all else 0,
            "coaches": coaches_all["c"] if coaches_all else 0,
            "managers": managers_all["c"] if managers_all else 0,
        },
        by_region=regions or [],
    )

@app.route("/api/notifications")
@login_required
def notifications():
    u = current_user()
    rows = q("SELECT * FROM notifications WHERE user_id=? ORDER BY created_at DESC LIMIT 50", (u["user_id"],))
    unread = q1("SELECT COUNT(*) c FROM notifications WHERE user_id=? AND is_read=0", (u["user_id"],))
    q("UPDATE notifications SET is_read=1 WHERE user_id=? AND is_read=0", (u["user_id"],), fetch=False)
    return jsonify(success=True, notifications=rows, unread=unread["c"] if unread else 0)

@app.route("/api/settings")
@role_required("ADMIN")
def settings_get():
    return jsonify(success=True, settings={
        "team_fee": get_setting("team_fee","150"), "player_fee": get_setting("player_fee","50"),
        "transfer_fee": get_setting("transfer_fee","50"),
        "transfer_window_active": get_setting("transfer_window_active","1"),
        "transfer_window_type": get_setting("transfer_window_type","MID_SEASON"),
        "season_label": season_label(),
        "season_start_year": str(season_start_year()),
        "season_start_date": get_setting("season_start_date","2026-03-01"),
        "season_end_date": get_setting("season_end_date","2027-02-28")})

@app.route("/api/settings/fees", methods=["POST"])
@role_required("ADMIN")
def settings_fees():
    d = request.json or {}
    for k in ("team_fee","player_fee","transfer_fee"):
        if k in d:
            set_setting(k, float(d[k]))
    f = fees()
    return jsonify(success=True, message=f"Fees: Team E{f['team']}, Player E{f['player']}, Transfer E{f['transfer']}", fees=f)

@app.route("/api/settings/window", methods=["POST"])
@role_required("ADMIN")
def settings_window():
    d = request.json or {}
    set_setting("transfer_window_active", str(d.get("transfer_window_active","0")))
    set_setting("transfer_window_type", d.get("transfer_window_type","MID_SEASON"))
    st = "OPEN" if d.get("transfer_window_active")=="1" else "CLOSED"
    return jsonify(success=True, message=f"Window {st}")

@app.route("/api/settings/season", methods=["POST"])
@role_required("ADMIN")
def settings_season():
    d = request.json or {}
    start = d.get("season_start_date") or ""
    end = d.get("season_end_date") or ""
    if not start or not end:
        return jsonify(success=False, error="start and end dates required"), 400
    if end <= start:
        return jsonify(success=False, error="End date must be after start date"), 400
    try:
        y = int(start[:4])
    except Exception:
        return jsonify(success=False, error="Invalid start date"), 400
    label = f"{y}/{str(y+1)[-2:]}"
    set_setting("season_start_date", start)
    set_setting("season_end_date", end)
    set_setting("season_start_year", str(y))
    set_setting("season_label", label)
    return jsonify(success=True, message=f"Season set to {label} ({start} → {end})", season_label=label)

# Allowed tables for admin DB browser (whitelist)
_DB_TABLES = (
    "users", "teams", "players", "coaches", "managers", "annual_registrations",
    "transfer_history", "financial_ledger", "player_stats", "notifications",
    "settings", "seasons",
)

@app.route("/api/admin/db/tables")
@role_required("ADMIN")
def admin_db_tables():
    existing = {r["name"] for r in q("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    tables = [n for n in _DB_TABLES if n in existing]
    # include any other app tables
    for n in sorted(existing):
        if n not in tables:
            tables.append(n)
    return jsonify(success=True, tables=tables)

@app.route("/api/admin/db/table/<table_name>")
@role_required("ADMIN")
def admin_db_table(table_name):
    existing = {r["name"] for r in q("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    if table_name not in existing:
        return jsonify(success=False, error="Unknown table"), 404
    # only allow simple table names
    if not table_name.replace("_", "").isalnum():
        return jsonify(success=False, error="Invalid table name"), 400
    cols = [r["name"] for r in q(f"PRAGMA table_info({table_name})")]
    total = q1(f"SELECT COUNT(*) AS c FROM {table_name}")
    count = total["c"] if total else 0
    limit = 200
    rows = q(f"SELECT * FROM {table_name} LIMIT {limit}")
    # redact password hashes
    for row in rows:
        for k in list(row.keys()):
            if "password" in k.lower() and row[k]:
                row[k] = "••••••••"
    return jsonify(success=True, table=table_name, columns=cols, rows=rows, count=count, truncated=count > limit)

@app.route("/api/teams", methods=["GET","POST"])
@login_required
def teams():
    if request.method=="POST":
        u=current_user()
        if u["role"] not in ("ADMIN","OFFICER"): return jsonify(success=False,error="Denied"),403
        d=request.json or {}
        if not d.get("team_name") or not d.get("manager_name"): return jsonify(success=False,error="Required fields"),400
        try:
            conn=get_conn()
            cur=conn.execute("""INSERT INTO teams(team_name,division,category,region,primary_jersey_color,secondary_jersey_color,manager_name,manager_phone)
                VALUES(?,?,?,?,?,?,?,?)""",
                (d["team_name"],d.get("division") or "National",d.get("category") or "Male",d.get("region") or "National",
                 d.get("primary_jersey_color"),d.get("secondary_jersey_color"),d["manager_name"],d.get("manager_phone")))
            conn.commit(); tid=cur.lastrowid; conn.close()
            return jsonify(success=True, team=q1("SELECT * FROM teams WHERE team_id=?",(tid,)),
                           message=f"Team created ({d.get('division')} {d.get('category')}). Fee E{fees()['team']}."),201
        except sqlite3.IntegrityError: return jsonify(success=False,error="Team exists in this division/category"),409
    # All roles can browse all teams (coaches need this to pick transfer targets)
    rows=q("""SELECT t.*,(SELECT COUNT(*) FROM annual_registrations ar WHERE ar.team_id=t.team_id AND ar.season_year=? AND ar.is_active_roster=1 AND COALESCE(ar.registration_status,'APPROVED')='APPROVED') active_players
        FROM teams t ORDER BY t.team_name""",(season_start_year(),))
    return jsonify(success=True, teams=rows)

@app.route("/api/teams/<int:tid>", methods=["PATCH"])
@role_required("ADMIN")
def team_update(tid):
    d = request.json or {}
    if not q1("SELECT team_id FROM teams WHERE team_id=?", (tid,)):
        return jsonify(success=False, error="Not found"), 404
    q("""UPDATE teams SET team_name=COALESCE(?,team_name), division=COALESCE(?,division), category=COALESCE(?,category),
        region=COALESCE(?,region),
        manager_name=COALESCE(?,manager_name), manager_phone=COALESCE(?,manager_phone),
        primary_jersey_color=COALESCE(?,primary_jersey_color), secondary_jersey_color=COALESCE(?,secondary_jersey_color)
        WHERE team_id=?""",
      (d.get("team_name"), d.get("division"), d.get("category"), d.get("region"), d.get("manager_name"),
       d.get("manager_phone"), d.get("primary_jersey_color"), d.get("secondary_jersey_color"), tid), fetch=False)
    return jsonify(success=True, message="Team updated", team=q1("SELECT * FROM teams WHERE team_id=?", (tid,)))

@app.route("/api/teams/<int:tid>", methods=["DELETE"])
@role_required("ADMIN")
def team_delete(tid):
    active = q1("SELECT COUNT(*) c FROM annual_registrations WHERE team_id=? AND is_active_roster=1", (tid,))
    if active and active["c"] > 0:
        return jsonify(success=False, error=f"Cannot delete: {active['c']} active players. Remove them first."), 400
    q("UPDATE coaches SET is_active=0 WHERE team_id=?", (tid,), fetch=False)
    q("UPDATE managers SET is_active=0 WHERE team_id=?", (tid,), fetch=False)
    q("DELETE FROM teams WHERE team_id=?", (tid,), fetch=False)
    return jsonify(success=True, message="Team deleted")

@app.route("/api/teams/<int:tid>")
@login_required
def team_detail(tid):
    team=q1("SELECT * FROM teams WHERE team_id=?",(tid,))
    if not team: return jsonify(success=False,error="Not found"),404
    sy=season_start_year()
    players=q("""SELECT p.*,ar.preferred_position,ar.dominant_hand,ar.jersey_number,ar.player_fee_status,ar.registration_status
        FROM annual_registrations ar JOIN players p ON p.player_id=ar.player_id
        WHERE ar.team_id=? AND ar.season_year=? AND ar.is_active_roster=1
          AND COALESCE(ar.registration_status,'APPROVED')='APPROVED'
        ORDER BY p.legal_name""",(tid,sy))
    if not players:
        players=q("""SELECT p.*,ar.preferred_position,ar.dominant_hand,ar.jersey_number,ar.player_fee_status,ar.registration_status
            FROM annual_registrations ar JOIN players p ON p.player_id=ar.player_id
            WHERE ar.team_id=? AND ar.season_year=? AND ar.is_active_roster=1
              AND COALESCE(ar.registration_status,'APPROVED')='APPROVED'
            ORDER BY p.legal_name""",(tid,SEASON))
    coaches=q("""SELECT c.*,u.username FROM coaches c LEFT JOIN users u ON u.user_id=c.user_id
        WHERE c.team_id=? AND c.is_active=1 ORDER BY c.legal_name""",(tid,))
    managers=q("""SELECT m.*,u.username FROM managers m LEFT JOIN users u ON u.user_id=m.user_id
        WHERE m.team_id=? AND m.is_active=1 ORDER BY m.legal_name""",(tid,))
    return jsonify(success=True, team=team, players=players, coaches=coaches, managers=managers)

@app.route("/api/players", methods=["GET","POST"])
@login_required
def players():
    if request.method=="POST":
        u=current_user()
        if u["role"] not in ("ADMIN","OFFICER"): return jsonify(success=False,error="Denied"),403
        d=request.form.to_dict() if request.form else (request.json or {})
        if not all(d.get(k) for k in ("legal_name","date_of_birth","gender","team_id")):
            return jsonify(success=False,error="Required fields missing"),400
        photo=save_photo(request.files.get("photo")) if request.files else None
        try:
            conn=get_conn()
            lic=gen_license()
            # default region from team if omitted
            reg = d.get("region")
            if not reg:
                tm=q1("SELECT region FROM teams WHERE team_id=?",(int(d["team_id"]),))
                reg = (tm or {}).get("region") or "National"
            cur=conn.execute("""INSERT INTO players(global_license_number,legal_name,date_of_birth,gender,blood_type,
                nationality,national_id,address,phone,email,region,height_cm,weight_kg,photo_path,
                emergency_contact_name,emergency_contact_phone,medical_notes)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (lic,d["legal_name"],d["date_of_birth"],d["gender"],d.get("blood_type"),
                 d.get("nationality"),d.get("national_id"),d.get("address"),d.get("phone"),d.get("email"),reg,
                 d.get("height_cm") or None,d.get("weight_kg") or None,photo,
                 d.get("emergency_contact_name"),d.get("emergency_contact_phone"),d.get("medical_notes")))
            pid=cur.lastrowid
            # Always pending approval; coach/manager must pay fee before Admin can approve
            team_id=int(d["team_id"])
            reg_status="PENDING_APPROVAL"
            conn.execute("""INSERT INTO annual_registrations(player_id,team_id,season_year,preferred_position,dominant_hand,jersey_number,
                player_fee_status,is_active_roster,registration_status,approved_by,approved_at)
                VALUES(?,?,?,?,?,?, 'PENDING', 0, ?, NULL, NULL)""",
                (pid,team_id,season_start_year(),d.get("preferred_position"),d.get("dominant_hand"),
                d.get("jersey_number") or None, reg_status))
            conn.commit(); conn.close()
            fee_amt=fees()["player"]
            # Notify team coach(es) and manager(s) to pay
            for row in q("""SELECT user_id FROM coaches WHERE team_id=? AND is_active=1 AND user_id IS NOT NULL""",(team_id,)):
                notify(row["user_id"], "Pay player registration fee",
                       f"{d['legal_name']} ({lic}) — pay E{fee_amt} before Admin can approve.", "player_reg", pid)
            for row in q("""SELECT user_id FROM managers WHERE team_id=? AND is_active=1 AND user_id IS NOT NULL""",(team_id,)):
                notify(row["user_id"], "Pay player registration fee",
                       f"{d['legal_name']} ({lic}) — pay E{fee_amt} before Admin can approve.", "player_reg", pid)
            for adm in q("SELECT user_id FROM users WHERE role='ADMIN' AND is_active=1"):
                notify(adm["user_id"], "Player registration pending",
                       f"{d['legal_name']} ({lic}) — waiting for team fee payment, then approval.", "player_reg", pid)
            msg=f"Registered {lic}. Coach/Manager must pay E{fee_amt}, then Admin can approve."
            return jsonify(success=True, player=q1("SELECT * FROM players WHERE player_id=?",(pid,)),
                           registration_status=reg_status, message=msg),201
        except Exception as e: return jsonify(success=False,error=str(e)),500
    sy=season_start_year()
    rows=q("""SELECT p.*,ar.preferred_position,ar.player_fee_status,ar.jersey_number,ar.registration_status,
        ar.registration_id,ar.rejection_reason,ar.payment_method,ar.pop_path,t.team_name,ar.team_id
        FROM players p LEFT JOIN annual_registrations ar ON ar.player_id=p.player_id AND ar.season_year IN (?,?)
        LEFT JOIN teams t ON t.team_id=ar.team_id ORDER BY p.legal_name LIMIT 300""",(sy,SEASON))
    return jsonify(success=True, players=rows)

@app.route("/api/players/<int:pid>")
@login_required
def player_detail(pid):
    u=current_user()
    sy=season_start_year()
    row=q1("""SELECT p.*,ar.preferred_position,ar.dominant_hand,ar.jersey_number,ar.player_fee_status,
        ar.season_year,ar.team_id,ar.registration_status,ar.registration_id,ar.rejection_reason,ar.approved_at,
        ar.payment_method,ar.pop_path,
        t.team_name FROM players p
        LEFT JOIN annual_registrations ar ON ar.player_id=p.player_id AND ar.season_year IN (?,?)
        LEFT JOIN teams t ON t.team_id=ar.team_id WHERE p.player_id=?
        ORDER BY CASE ar.registration_status WHEN 'APPROVED' THEN 0 WHEN 'PENDING_APPROVAL' THEN 1 ELSE 2 END, ar.season_year DESC""",
        (sy,SEASON,pid))
    if not row: return jsonify(success=False,error="Not found"),404
    stats=q("SELECT * FROM player_stats WHERE player_id=? AND season_year=? ORDER BY match_date DESC",(pid,SEASON))
    totals=q1("""SELECT COALESCE(SUM(goals),0) goals,COALESCE(SUM(assists),0) assists,
        COALESCE(SUM(saves),0) saves,COALESCE(SUM(minutes_played),0) minutes_played
        FROM player_stats WHERE player_id=? AND season_year=?""",(pid,SEASON)) or {}
    return jsonify(success=True, player=row, stats=stats, totals=totals)

@app.route("/api/registrations/pending")
@login_required
def registrations_pending():
    u=current_user()
    if u["role"] not in ("ADMIN","OFFICER","COACH","MANAGER"):
        return jsonify(success=False,error="Denied"),403
    sy=season_start_year()
    rows=q("""SELECT ar.registration_id, ar.player_id, ar.team_id, ar.season_year, ar.registration_status,
        ar.preferred_position, ar.player_fee_status, ar.payment_method, ar.pop_path,
        p.legal_name, p.global_license_number, p.date_of_birth, p.gender, p.region, p.photo_path,
        t.team_name
        FROM annual_registrations ar
        JOIN players p ON p.player_id=ar.player_id
        JOIN teams t ON t.team_id=ar.team_id
        WHERE ar.registration_status='PENDING_APPROVAL' AND ar.season_year IN (?,?)
        ORDER BY ar.registration_id DESC""",(sy,SEASON))
    if u["role"] in ("COACH","MANAGER"):
        tid=u.get("team_id")
        if not tid and u["role"]=="MANAGER":
            m=q1("SELECT team_id FROM managers WHERE user_id=? AND is_active=1",(u["user_id"],))
            tid=(m or {}).get("team_id")
        if not tid and u["role"]=="COACH":
            c=q1("SELECT team_id FROM coaches WHERE user_id=? AND is_active=1",(u["user_id"],))
            tid=(c or {}).get("team_id")
        rows=[r for r in rows if r["team_id"]==tid]
    return jsonify(success=True, pending=rows)

@app.route("/api/registrations/<int:rid>/approve", methods=["POST"])
@role_required("ADMIN")
def registration_approve(rid):
    ar=q1("SELECT * FROM annual_registrations WHERE registration_id=?",(rid,))
    if not ar: return jsonify(success=False,error="Not found"),404
    if ar.get("registration_status")=="APPROVED":
        return jsonify(success=False,error="Already approved"),400
    if ar.get("registration_status")=="REJECTED":
        return jsonify(success=False,error="Registration was rejected; register again"),400
    if (ar.get("player_fee_status") or "PENDING") != "PAID":
        return jsonify(success=False,error="Coach/Manager must pay the player registration fee before approval"),402
    u=current_user()
    now=datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    q("""UPDATE annual_registrations SET registration_status='APPROVED', approved_by=?, approved_at=?,
        rejection_reason=NULL, is_active_roster=1 WHERE registration_id=?""",
      (u["user_id"], now, rid), fetch=False)
    pl=q1("SELECT legal_name, global_license_number FROM players WHERE player_id=?",(ar["player_id"],))
    name=(pl or {}).get("legal_name") or "Player"
    lic=(pl or {}).get("global_license_number") or ""
    for adm in q("SELECT user_id FROM users WHERE role IN ('ADMIN','OFFICER') AND is_active=1"):
        notify(adm["user_id"], "Player approved", f"{name} ({lic}) registration approved.", "player_reg", ar["player_id"])
    for row in q("""SELECT user_id FROM coaches WHERE team_id=? AND is_active=1 AND user_id IS NOT NULL""",(ar["team_id"],)):
        notify(row["user_id"], "Player approved", f"{name} ({lic}) is now approved.", "player_reg", ar["player_id"])
    return jsonify(success=True, message=f"Approved {name}.")

def _can_pay_reg(u, team_id):
    if u["role"] in ("ADMIN", "OFFICER"):
        return True
    if u["role"] == "COACH":
        if u.get("team_id") == team_id:
            return True
        c = q1("SELECT team_id FROM coaches WHERE user_id=? AND is_active=1", (u["user_id"],))
        return bool(c and c["team_id"] == team_id)
    if u["role"] == "MANAGER":
        if u.get("team_id") == team_id:
            return True
        m = q1("SELECT team_id FROM managers WHERE user_id=? AND is_active=1", (u["user_id"],))
        return bool(m and m["team_id"] == team_id)
    return False

@app.route("/api/registrations/<int:rid>/pay-fee", methods=["POST"])
@login_required
def registration_pay_fee(rid):
    """Pay via MOBILE_MONEY, EFT (instant sandbox), or CASH with POP upload."""
    ar = q1("SELECT * FROM annual_registrations WHERE registration_id=?", (rid,))
    if not ar:
        return jsonify(success=False, error="Not found"), 404
    if ar.get("registration_status") == "REJECTED":
        return jsonify(success=False, error="Registration was rejected"), 400
    if (ar.get("player_fee_status") or "") == "PAID":
        return jsonify(success=False, error="Fee already paid"), 400
    u = current_user()
    team_id = ar["team_id"]
    if not _can_pay_reg(u, team_id):
        return jsonify(success=False, error="Only the team's coach/manager (or Admin/Officer) can pay this fee"), 403

    # form or JSON
    if request.files or request.form:
        method = (request.form.get("payment_method") or "MOBILE_MONEY").upper()
        phone = request.form.get("phone") or u.get("phone") or "000"
        eft_ref = request.form.get("eft_reference") or ""
        pop = save_photo(request.files.get("pop")) if request.files else None
    else:
        d = request.json or {}
        method = (d.get("payment_method") or "MOBILE_MONEY").upper()
        phone = d.get("phone") or u.get("phone") or "000"
        eft_ref = d.get("eft_reference") or ""
        pop = None

    if method not in ("MOBILE_MONEY", "EFT", "CASH"):
        return jsonify(success=False, error="payment_method must be MOBILE_MONEY, EFT or CASH"), 400

    fee = fees()["player"]
    ref = gen_ref()
    pl = q1("SELECT legal_name, global_license_number FROM players WHERE player_id=?", (ar["player_id"],))
    name = (pl or {}).get("legal_name") or "Player"
    lic = (pl or {}).get("global_license_number") or ""

    if method == "CASH":
        if not pop:
            return jsonify(success=False, error="Cash payment requires Proof of Payment (POP) image upload"), 400
        q("""INSERT INTO financial_ledger(reference_number,amount,payment_type,payment_status,payer_phone_number,related_player_id,related_team_id)
            VALUES(?,?,'PLAYER_AFFILIATION','PENDING',?,?,?)""",
          (ref, fee, phone, ar["player_id"], team_id), fetch=False)
        q("""UPDATE annual_registrations SET player_fee_status='AWAITING_CONFIRMATION',
            payment_method='CASH', pop_path=? WHERE registration_id=?""", (pop, rid), fetch=False)
        for adm in q("SELECT user_id FROM users WHERE role='ADMIN' AND is_active=1"):
            notify(adm["user_id"], "Cash POP submitted — confirm payment",
                   f"{name} ({lic}) cash POP uploaded. Confirm to mark fee paid.", "player_reg", ar["player_id"])
        return jsonify(success=True, message=f"POP submitted for {name}. Waiting for Admin to confirm cash payment.",
                       systemReference=ref, player_fee_status="AWAITING_CONFIRMATION"), 201

    # MoMo / EFT — sandbox marks SUCCESSFUL immediately
    note = eft_ref if method == "EFT" else phone
    q("""INSERT INTO financial_ledger(reference_number,momo_mpamba_reference,amount,payment_type,payment_status,payer_phone_number,related_player_id,related_team_id)
        VALUES(?,?,?,'PLAYER_AFFILIATION','SUCCESSFUL',?,?,?)""",
      (ref, note or None, fee, phone, ar["player_id"], team_id), fetch=False)
    q("""UPDATE annual_registrations SET player_fee_status='PAID', payment_method=? WHERE registration_id=?""",
      (method, rid), fetch=False)
    for adm in q("SELECT user_id FROM users WHERE role='ADMIN' AND is_active=1"):
        notify(adm["user_id"], "Player fee paid — ready to approve",
               f"{name} ({lic}) paid E{fee} via {method}. You can approve.", "player_reg", ar["player_id"])
    return jsonify(success=True, message=f"Fee E{fee} paid via {method} for {name}. Awaiting Admin approval.",
                   systemReference=ref, player_fee_status="PAID")

@app.route("/api/registrations/<int:rid>/confirm-cash", methods=["POST"])
@role_required("ADMIN")
def registration_confirm_cash(rid):
    ar = q1("SELECT * FROM annual_registrations WHERE registration_id=?", (rid,))
    if not ar:
        return jsonify(success=False, error="Not found"), 404
    if (ar.get("player_fee_status") or "") != "AWAITING_CONFIRMATION":
        return jsonify(success=False, error="No cash POP awaiting confirmation"), 400
    q("UPDATE annual_registrations SET player_fee_status='PAID' WHERE registration_id=?", (rid,), fetch=False)
    # mark latest pending ledger for this player as successful
    q("""UPDATE financial_ledger SET payment_status='SUCCESSFUL'
        WHERE related_player_id=? AND payment_type='PLAYER_AFFILIATION' AND payment_status='PENDING'""",
      (ar["player_id"],), fetch=False)
    pl = q1("SELECT legal_name, global_license_number FROM players WHERE player_id=?", (ar["player_id"],))
    name = (pl or {}).get("legal_name") or "Player"
    for row in q("SELECT user_id FROM coaches WHERE team_id=? AND is_active=1 AND user_id IS NOT NULL", (ar["team_id"],)):
        notify(row["user_id"], "Cash payment confirmed", f"{name} fee confirmed by Admin.", "player_reg", ar["player_id"])
    return jsonify(success=True, message=f"Cash payment confirmed for {name}. You can now approve.")

@app.route("/api/registrations/remind-fees", methods=["POST"])
@role_required("ADMIN", "OFFICER")
def remind_fees():
    n = run_fee_reminders(force=True)
    return jsonify(success=True, message=f"Sent fee reminders for {n} player(s).", count=n)

@app.route("/api/registrations/run-reminders")
@login_required
def auto_reminders():
    n = run_fee_reminders(force=False)
    return jsonify(success=True, reminded=n)

@app.route("/api/registrations/<int:rid>/reject", methods=["POST"])
@role_required("ADMIN")
def registration_reject(rid):
    ar=q1("SELECT * FROM annual_registrations WHERE registration_id=?",(rid,))
    if not ar: return jsonify(success=False,error="Not found"),404
    if ar.get("registration_status")=="APPROVED":
        return jsonify(success=False,error="Already approved"),400
    reason=(request.json or {}).get("reason") or "Rejected by Admin"
    u=current_user()
    now=datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    q("""UPDATE annual_registrations SET registration_status='REJECTED', approved_by=?, approved_at=?,
        rejection_reason=?, is_active_roster=0 WHERE registration_id=?""",
      (u["user_id"], now, reason, rid), fetch=False)
    pl=q1("SELECT legal_name, global_license_number FROM players WHERE player_id=?",(ar["player_id"],))
    name=(pl or {}).get("legal_name") or "Player"
    for off in q("SELECT user_id FROM users WHERE role='OFFICER' AND is_active=1"):
        notify(off["user_id"], "Player registration rejected",
               f"{name}: {reason}", "player_reg", ar["player_id"])
    return jsonify(success=True, message=f"Rejected {name}.")

@app.route("/api/players/<int:pid>", methods=["PATCH"])
@role_required("ADMIN")
def player_edit(pid):
    d = request.form.to_dict() if request.form else (request.json or {})
    photo = save_photo(request.files.get("photo")) if request.files else None
    fields = ["legal_name","phone","email","address","national_id","nationality","region","height_cm","weight_kg",
              "blood_type","emergency_contact_name","emergency_contact_phone","medical_notes"]
    sets, vals = [], []
    for f in fields:
        if f in d and d[f] is not None:
            sets.append(f"{f}=?"); vals.append(d[f] if d[f]!="" else None)
    if photo:
        sets.append("photo_path=?"); vals.append(photo)
    if not sets: return jsonify(success=False,error="Nothing to update"),400
    vals.append(pid)
    q(f"UPDATE players SET {', '.join(sets)} WHERE player_id=?", vals, fetch=False)
    return jsonify(success=True, message="Player bio updated", player=q1("SELECT * FROM players WHERE player_id=?",(pid,)))

@app.route("/api/players/<int:pid>", methods=["DELETE"])
@role_required("ADMIN")
def player_delete(pid):
    q("DELETE FROM player_stats WHERE player_id=?", (pid,), fetch=False)
    q("DELETE FROM annual_registrations WHERE player_id=?", (pid,), fetch=False)
    q("DELETE FROM players WHERE player_id=?", (pid,), fetch=False)
    return jsonify(success=True, message="Player deleted")

@app.route("/api/players/<int:pid>/stats", methods=["POST"])
@login_required
def player_stats_add(pid):
    u=current_user()
    reg=q1("SELECT team_id FROM annual_registrations WHERE player_id=? AND season_year=? AND is_active_roster=1",(pid,SEASON))
    if u["role"]=="COACH" and (not reg or reg["team_id"]!=u.get("team_id")):
        return jsonify(success=False,error="Only your roster"),403
    if u["role"] not in ("ADMIN","OFFICER","COACH"):
        return jsonify(success=False,error="Denied"),403
    d=request.json or {}
    q("""INSERT INTO player_stats(player_id,season_year,match_date,opponent,goals,assists,saves,yellow_cards,red_cards,minutes_played,notes,recorded_by)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
      (pid,SEASON,d.get("match_date"),d.get("opponent"),int(d.get("goals") or 0),int(d.get("assists") or 0),
       int(d.get("saves") or 0),int(d.get("yellow_cards") or 0),int(d.get("red_cards") or 0),
       int(d.get("minutes_played") or 0),d.get("notes"),u["user_id"]), fetch=False)
    return jsonify(success=True, message="Stats recorded")

@app.route("/api/coaches", methods=["GET","POST"])
@login_required
def coaches():
    if request.method=="POST":
        u=current_user()
        if u["role"] not in ("ADMIN","OFFICER"): return jsonify(success=False,error="Denied"),403
        d=request.form.to_dict() if request.form else (request.json or {})
        if not all(d.get(k) for k in ("legal_name","team_id","username","password")):
            return jsonify(success=False,error="Required fields"),400
        photo=save_photo(request.files.get("photo")) if request.files else None
        try:
            conn=get_conn()
            cur=conn.execute("INSERT INTO users(username,password_hash,full_name,role,team_id,phone) VALUES(?,?,?,?,?,?)",
                (d["username"],hash_pw(d["password"]),d["legal_name"],"COACH",int(d["team_id"]),d.get("phone")))
            uid=cur.lastrowid
            lic=f"COACH-{SEASON}-{random.randint(1000,9999)}"
            reg=d.get("region")
            if not reg:
                tm=q1("SELECT region FROM teams WHERE team_id=?",(int(d["team_id"]),))
                reg=(tm or {}).get("region") or "National"
            conn.execute("""INSERT INTO coaches(user_id,team_id,legal_name,phone,email,national_id,date_of_birth,gender,address,region,photo_path,license_number,qualifications,season_year)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (uid,int(d["team_id"]),d["legal_name"],d.get("phone"),d.get("email"),d.get("national_id"),
                 d.get("date_of_birth"),d.get("gender"),d.get("address"),reg,photo,lic,d.get("qualifications"),SEASON))
            conn.commit(); conn.close()
            return jsonify(success=True, message=f"Coach registered ({lic}). Login: {d['username']}"),201
        except sqlite3.IntegrityError: return jsonify(success=False,error="Username exists"),409
        except Exception as e: return jsonify(success=False,error=str(e)),500
    rows=q("""SELECT c.*,t.team_name,u.username FROM coaches c
        LEFT JOIN teams t ON t.team_id=c.team_id LEFT JOIN users u ON u.user_id=c.user_id
        WHERE c.season_year=? AND c.is_active=1 ORDER BY c.legal_name""",(SEASON,))
    return jsonify(success=True, coaches=rows)

@app.route("/api/coaches/<int:cid>")
@login_required
def coach_detail(cid):
    row=q1("""SELECT c.*,t.team_name,u.username FROM coaches c
        LEFT JOIN teams t ON t.team_id=c.team_id LEFT JOIN users u ON u.user_id=c.user_id WHERE c.coach_id=?""",(cid,))
    if not row: return jsonify(success=False,error="Not found"),404
    return jsonify(success=True, coach=row)

@app.route("/api/coaches/<int:cid>", methods=["PATCH"])
@login_required
def coach_update(cid):
    row=q1("SELECT * FROM coaches WHERE coach_id=? AND is_active=1",(cid,))
    if not row: return jsonify(success=False,error="Not found"),404
    u=current_user()
    if u["role"] not in ("ADMIN","OFFICER"):
        if u["role"]!="COACH" or u.get("user_id")!=row.get("user_id"):
            return jsonify(success=False,error="You can only edit your own coach profile"),403
    d=request.json or {}
    # coaches cannot change team
    new_team=None
    if u["role"] in ("ADMIN","OFFICER") and d.get("team_id"):
        new_team=int(d["team_id"])
    q("""UPDATE coaches SET
        legal_name=COALESCE(?,legal_name), phone=COALESCE(?,phone), email=COALESCE(?,email),
        gender=COALESCE(?,gender), national_id=COALESCE(?,national_id),
        date_of_birth=COALESCE(?,date_of_birth), address=COALESCE(?,address),
        region=COALESCE(?,region), qualifications=COALESCE(?,qualifications),
        team_id=COALESCE(?,team_id)
        WHERE coach_id=?""",
      (d.get("legal_name"), d.get("phone"), d.get("email"), d.get("gender"),
       d.get("national_id"), d.get("date_of_birth"), d.get("address"),
       d.get("region"), d.get("qualifications"), new_team, cid), fetch=False)
    if row.get("user_id") and d.get("legal_name"):
        q("UPDATE users SET full_name=? WHERE user_id=?",(d["legal_name"], row["user_id"]), fetch=False)
    if row.get("user_id") and new_team:
        q("UPDATE users SET team_id=? WHERE user_id=?",(new_team, row["user_id"]), fetch=False)
    return jsonify(success=True, message="Coach updated")

@app.route("/api/coaches/<int:cid>", methods=["DELETE"])
@role_required("ADMIN")
def coach_delete(cid):
    row=q1("SELECT * FROM coaches WHERE coach_id=?",(cid,))
    if not row: return jsonify(success=False,error="Not found"),404
    q("UPDATE coaches SET is_active=0 WHERE coach_id=?",(cid,),fetch=False)
    if row.get("user_id"):
        q("UPDATE users SET is_active=0 WHERE user_id=?",(row["user_id"],),fetch=False)
    return jsonify(success=True, message="Coach deleted")

@app.route("/api/managers", methods=["GET","POST"])
@login_required
def managers_api():
    if request.method=="POST":
        u=current_user()
        if u["role"] not in ("ADMIN","OFFICER"): return jsonify(success=False,error="Denied"),403
        d=request.form.to_dict() if request.form else (request.json or {})
        if not all(d.get(k) for k in ("legal_name","team_id","username","password")):
            return jsonify(success=False,error="Required fields"),400
        photo=save_photo(request.files.get("photo")) if request.files else None
        try:
            conn=get_conn()
            cur=conn.execute("INSERT INTO users(username,password_hash,full_name,role,team_id,phone) VALUES(?,?,?,?,?,?)",
                (d["username"],hash_pw(d["password"]),d["legal_name"],"MANAGER",int(d["team_id"]),d.get("phone")))
            uid=cur.lastrowid
            lic=f"MGR-{season_start_year()}-{random.randint(1000,9999)}"
            reg=d.get("region")
            if not reg:
                tm=q1("SELECT region FROM teams WHERE team_id=?",(int(d["team_id"]),))
                reg=(tm or {}).get("region") or "National"
            conn.execute("""INSERT INTO managers(user_id,team_id,legal_name,phone,email,national_id,date_of_birth,gender,address,region,photo_path,license_number,season_year)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (uid,int(d["team_id"]),d["legal_name"],d.get("phone"),d.get("email"),d.get("national_id"),
                 d.get("date_of_birth"),d.get("gender"),d.get("address"),reg,photo,lic,season_start_year()))
            conn.execute("UPDATE teams SET manager_name=?, manager_phone=? WHERE team_id=?",
                         (d["legal_name"], d.get("phone"), int(d["team_id"])))
            conn.commit(); conn.close()
            return jsonify(success=True, message=f"Manager registered ({lic}). Login: {d['username']}"),201
        except sqlite3.IntegrityError: return jsonify(success=False,error="Username exists"),409
        except Exception as e: return jsonify(success=False,error=str(e)),500
    rows=q("""SELECT m.*,t.team_name,u.username FROM managers m
        LEFT JOIN teams t ON t.team_id=m.team_id LEFT JOIN users u ON u.user_id=m.user_id
        WHERE m.is_active=1 ORDER BY m.legal_name""")
    return jsonify(success=True, managers=rows)

@app.route("/api/managers/<int:mid>")
@login_required
def manager_detail(mid):
    row=q1("""SELECT m.*,t.team_name,u.username FROM managers m
        LEFT JOIN teams t ON t.team_id=m.team_id LEFT JOIN users u ON u.user_id=m.user_id
        WHERE m.manager_id=? AND m.is_active=1""",(mid,))
    if not row: return jsonify(success=False,error="Not found"),404
    return jsonify(success=True, manager=row)

@app.route("/api/managers/<int:mid>", methods=["PATCH"])
@role_required("ADMIN","OFFICER")
def manager_update(mid):
    d=request.json or {}
    row=q1("SELECT * FROM managers WHERE manager_id=? AND is_active=1",(mid,))
    if not row: return jsonify(success=False,error="Not found"),404
    q("""UPDATE managers SET
        legal_name=COALESCE(?,legal_name), phone=COALESCE(?,phone), email=COALESCE(?,email),
        gender=COALESCE(?,gender), national_id=COALESCE(?,national_id),
        date_of_birth=COALESCE(?,date_of_birth), address=COALESCE(?,address),
        region=COALESCE(?,region),
        team_id=COALESCE(?,team_id)
        WHERE manager_id=?""",
      (d.get("legal_name"), d.get("phone"), d.get("email"), d.get("gender"),
       d.get("national_id"), d.get("date_of_birth"), d.get("address"), d.get("region"),
       int(d["team_id"]) if d.get("team_id") else None, mid), fetch=False)
    if d.get("legal_name") or d.get("phone"):
        tid = int(d["team_id"]) if d.get("team_id") else row["team_id"]
        q("UPDATE teams SET manager_name=COALESCE(?,manager_name), manager_phone=COALESCE(?,manager_phone) WHERE team_id=?",
          (d.get("legal_name"), d.get("phone"), tid), fetch=False)
    if row.get("user_id") and d.get("legal_name"):
        q("UPDATE users SET full_name=? WHERE user_id=?", (d["legal_name"], row["user_id"]), fetch=False)
    if row.get("user_id") and d.get("team_id"):
        q("UPDATE users SET team_id=? WHERE user_id=?", (int(d["team_id"]), row["user_id"]), fetch=False)
    return jsonify(success=True, message="Manager updated", manager=q1("""SELECT m.*,t.team_name,u.username FROM managers m
        LEFT JOIN teams t ON t.team_id=m.team_id LEFT JOIN users u ON u.user_id=m.user_id WHERE m.manager_id=?""",(mid,)))

@app.route("/api/managers/<int:mid>", methods=["DELETE"])
@role_required("ADMIN")
def manager_delete(mid):
    row=q1("SELECT * FROM managers WHERE manager_id=?",(mid,))
    if not row: return jsonify(success=False,error="Not found"),404
    q("UPDATE managers SET is_active=0 WHERE manager_id=?",(mid,),fetch=False)
    if row.get("user_id"):
        q("UPDATE users SET is_active=0 WHERE user_id=?",(row["user_id"],),fetch=False)
    return jsonify(success=True, message="Manager deleted")

@app.route("/api/managers/<int:mid>/card")
@role_required("ADMIN","OFFICER")
def manager_card(mid):
    row=q1("""SELECT m.*,t.team_name FROM managers m
        LEFT JOIN teams t ON t.team_id=m.team_id WHERE m.manager_id=?""",(mid,))
    if not row: return jsonify(success=False,error="Manager not found"),404
    return jsonify(success=True, html=_card_html("Manager", row))

def _card_html(kind, data):
    photo = data.get("photo_path") or "/static/eha-logo.jpg"
    name = data.get("legal_name") or "—"
    team = data.get("team_name") or "—"
    dob = data.get("date_of_birth") or "—"
    lic = data.get("global_license_number") or data.get("license_number") or "—"
    pos = data.get("preferred_position") or ""
    return f"""
    <div class="id-card">
      <div class="card-head">
        <img src="/static/eha-logo.jpg" alt="EHA">
        <div class="org">ESWATINI HANDBALL<br>ASSOCIATION<br>Official {kind} Card</div>
      </div>
      <img class="card-photo" src="{photo}" alt="Photo">
      <div class="card-name">{name}</div>
      <div class="card-line"><b>License:</b> {lic}</div>
      <div class="card-line"><b>Team:</b> {team}</div>
      <div class="card-line"><b>Season:</b> {season_label()}</div>
      <div class="card-line"><b>Date of birth:</b> {dob}</div>
      {f'<div class="card-line"><b>Position:</b> {pos}</div>' if pos else ''}
      <div class="card-foot">EHA · Not transferable · Season {season_label()}</div>
    </div>"""

@app.route("/api/players/<int:pid>/card")
@role_required("ADMIN", "OFFICER")
def player_card(pid):
    row = q1("""SELECT p.*, ar.preferred_position, t.team_name FROM players p
        LEFT JOIN annual_registrations ar ON ar.player_id=p.player_id AND ar.season_year=?
        LEFT JOIN teams t ON t.team_id=ar.team_id WHERE p.player_id=?""", (SEASON, pid))
    if not row: return jsonify(success=False, error="Player not found"), 404
    return jsonify(success=True, html=_card_html("Player", row))

@app.route("/api/coaches/<int:cid>/card")
@role_required("ADMIN", "OFFICER")
def coach_card(cid):
    row = q1("""SELECT c.*, t.team_name FROM coaches c
        LEFT JOIN teams t ON t.team_id=c.team_id WHERE c.coach_id=?""", (cid,))
    if not row: return jsonify(success=False, error="Coach not found"), 404
    return jsonify(success=True, html=_card_html("Coach", row))

def _photo_fs_path(photo_path):
    if not photo_path: return None
    if photo_path.startswith("/static/"):
        fs = os.path.join(BASE, photo_path.lstrip("/"))
        return fs if os.path.isfile(fs) else None
    return None

def _draw_id_card(c, x, y, w, h, kind, data, logo_path):
    """Draw one ID card at (x,y) bottom-left."""
    c.setStrokeColorRGB(0.04, 0.18, 0.36)
    c.setLineWidth(1.6)
    c.roundRect(x, y, w, h, 6, stroke=1, fill=0)
    # header bar
    c.setFillColorRGB(0.04, 0.18, 0.36)
    c.rect(x, y + h - 16*mm, w, 16*mm, stroke=0, fill=1)
    c.setFillColorRGB(1, 1, 1)
    c.setFont("Helvetica-Bold", 8)
    c.drawString(x + 4*mm, y + h - 7*mm, "ESWATINI HANDBALL ASSOCIATION")
    c.setFont("Helvetica", 7)
    c.drawString(x + 4*mm, y + h - 12*mm, f"Official {kind} Card · Season {season_label()}")
    if logo_path and os.path.isfile(logo_path):
        try:
            c.drawImage(logo_path, x + w - 14*mm, y + h - 14.5*mm, width=11*mm, height=11*mm, mask='auto')
        except Exception:
            pass
    # photo
    photo = _photo_fs_path(data.get("photo_path"))
    px, py, pw, ph = x + w - 28*mm, y + 12*mm, 22*mm, 28*mm
    c.setStrokeColorRGB(0.7, 0.75, 0.8)
    c.setLineWidth(0.6)
    c.rect(px, py, pw, ph, stroke=1, fill=0)
    if photo:
        try:
            c.drawImage(photo, px, py, width=pw, height=ph, preserveAspectRatio=True, anchor='c', mask='auto')
        except Exception:
            pass
    else:
        c.setFillColorRGB(0.9, 0.92, 0.95)
        c.rect(px, py, pw, ph, stroke=0, fill=1)
        c.setFillColorRGB(0.5, 0.55, 0.6)
        c.setFont("Helvetica", 7)
        c.drawCentredString(px + pw/2, py + ph/2, "PHOTO")
    # text
    name = (data.get("legal_name") or "—")[:28]
    lic = data.get("global_license_number") or data.get("license_number") or "—"
    team = (data.get("team_name") or "—")[:26]
    dob = data.get("date_of_birth") or "—"
    pos = data.get("preferred_position") or ""
    c.setFillColorRGB(0.04, 0.18, 0.36)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(x + 4*mm, y + h - 24*mm, name)
    c.setFillColorRGB(0.15, 0.15, 0.18)
    c.setFont("Helvetica", 8)
    lines = [
        f"License: {lic}",
        f"Team: {team}",
        f"Season: {season_label()}",
        f"Date of birth: {dob}",
    ]
    if pos:
        lines.append(f"Position: {pos}")
    ty = y + h - 30*mm
    for line in lines:
        c.drawString(x + 4*mm, ty, line[:42])
        ty -= 5*mm
    c.setFont("Helvetica", 6)
    c.setFillColorRGB(0.4, 0.4, 0.45)
    c.drawCentredString(x + w/2, y + 4*mm, f"EHA · Not transferable · {kind}")

def build_team_cards_pdf(team_id):
    if not HAS_REPORTLAB:
        return None, "PDF engine not installed (reportlab)"
    team = q1("SELECT * FROM teams WHERE team_id=?", (team_id,))
    if not team:
        return None, "Team not found"
    sy = season_start_year()
    players = q("""SELECT p.*, ar.preferred_position, t.team_name FROM players p
        JOIN annual_registrations ar ON ar.player_id=p.player_id AND ar.season_year=? AND ar.is_active_roster=1
        JOIN teams t ON t.team_id=ar.team_id
        WHERE ar.team_id=? ORDER BY p.legal_name""", (sy, team_id))
    if not players:
        players = q("""SELECT p.*, ar.preferred_position, t.team_name FROM players p
            JOIN annual_registrations ar ON ar.player_id=p.player_id AND ar.season_year=? AND ar.is_active_roster=1
            JOIN teams t ON t.team_id=ar.team_id
            WHERE ar.team_id=? ORDER BY p.legal_name""", (SEASON, team_id))
    coaches = q("""SELECT c.*, t.team_name FROM coaches c
        LEFT JOIN teams t ON t.team_id=c.team_id WHERE c.team_id=? AND c.is_active=1 ORDER BY c.legal_name""", (team_id,))
    managers = q("""SELECT m.*, t.team_name FROM managers m
        LEFT JOIN teams t ON t.team_id=m.team_id WHERE m.team_id=? AND m.is_active=1 ORDER BY m.legal_name""", (team_id,))
    items = [("Manager", m) for m in managers] + [("Coach", c) for c in coaches] + [("Player", p) for p in players]
    if not items:
        return None, "No people on this team to put on cards"
    buf = io.BytesIO()
    c = pdf_canvas.Canvas(buf, pagesize=A4)
    page_w, page_h = A4
    logo = os.path.join(BASE, "static", "eha-logo.jpg")
    card_w, card_h = 88*mm, 55*mm
    margin_x, margin_y = 12*mm, 12*mm
    gap_x, gap_y = 8*mm, 8*mm
    cols, rows = 2, 4
    # title page header on each page handled per card
    idx = 0
    while idx < len(items):
        # page header
        c.setFont("Helvetica-Bold", 11)
        c.setFillColorRGB(0.04, 0.18, 0.36)
        c.drawString(margin_x, page_h - 10*mm, f"EHA · {team['team_name']} · {team.get('division') or ''} {team.get('category') or ''}")
        c.setFont("Helvetica", 8)
        c.drawRightString(page_w - margin_x, page_h - 10*mm, f"Season {season_label()}")
        start_y = page_h - 14*mm - card_h
        for r in range(rows):
            for col in range(cols):
                if idx >= len(items):
                    break
                kind, data = items[idx]
                x = margin_x + col * (card_w + gap_x)
                y = start_y - r * (card_h + gap_y)
                _draw_id_card(c, x, y, card_w, card_h, kind, data, logo)
                idx += 1
            if idx >= len(items):
                break
        c.showPage()
    c.save()
    buf.seek(0)
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in team["team_name"])[:40]
    filename = f"EHA_cards_{safe}_{season_start_year()}.pdf"
    return buf, filename

@app.route("/api/teams/<int:tid>/cards.pdf")
@role_required("ADMIN", "OFFICER")
def team_cards_pdf(tid):
    buf, name_or_err = build_team_cards_pdf(tid)
    if buf is None:
        return jsonify(success=False, error=name_or_err), 400
    return send_file(buf, mimetype="application/pdf", as_attachment=True, download_name=name_or_err)

@app.route("/api/teams/<int:tid>/share-cards", methods=["POST"])
@role_required("ADMIN", "OFFICER")
def team_share_cards(tid):
    team = q1("SELECT * FROM teams WHERE team_id=?", (tid,))
    if not team:
        return jsonify(success=False, error="Team not found"), 404
    # ensure PDF can be built
    buf, name_or_err = build_team_cards_pdf(tid)
    if buf is None:
        return jsonify(success=False, error=name_or_err), 400
    token = uuid.uuid4().hex
    expires = (datetime.utcnow() + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    u = current_user()
    q("INSERT INTO share_links(token, team_id, created_by, expires_at) VALUES(?,?,?,?)",
      (token, tid, u["user_id"] if u else None, expires), fetch=False)
    # absolute URL based on request host
    base = request.host_url.rstrip("/")
    share_url = f"{base}/api/share/cards/{token}.pdf"
    note = (request.json or {}).get("note") or ""
    msg = (
        f"Eswatini Handball Association – ID cards (PDF)\n"
        f"Team: {team['team_name']} ({team.get('division') or ''} {team.get('category') or ''})\n"
        f"Season: {season_label()}\n"
        f"Download: {share_url}\n"
        f"(Link expires {expires} UTC)"
    )
    if note:
        msg += f"\n\n{note}"
    return jsonify(success=True, share_url=share_url, expires_at=expires,
                   season_label=season_label(), message_text=msg,
                   message="Share link created (7 days)")

@app.route("/api/share/cards/<token>.pdf")
def share_cards_pdf(token):
    row = q1("SELECT * FROM share_links WHERE token=?", (token,))
    if not row:
        return jsonify(success=False, error="Invalid or expired link"), 404
    try:
        exp = datetime.strptime(row["expires_at"], "%Y-%m-%d %H:%M:%S")
        if datetime.utcnow() > exp:
            return jsonify(success=False, error="Link expired"), 410
    except Exception:
        pass
    buf, name_or_err = build_team_cards_pdf(row["team_id"])
    if buf is None:
        return jsonify(success=False, error=name_or_err), 400
    return send_file(buf, mimetype="application/pdf", as_attachment=True, download_name=name_or_err)

@app.route("/api/transfers/window")
@login_required
def transfer_window():
    active=get_setting("transfer_window_active","1")=="1"
    wtype=get_setting("transfer_window_type","MID_SEASON")
    fee=fees()["transfer"]
    msg=f"Window OPEN ({wtype}). Request by player license. Both coaches must agree, fee E{fee}, then Admin approves." if active else "Window CLOSED."
    return jsonify(active=active, window=wtype if active else None, message=msg)

@app.route("/api/transfers", methods=["GET"])
@login_required
def list_transfers():
    rows=q("""SELECT th.*,p.legal_name player_name,p.global_license_number player_license,
        ft.team_name from_team,tt.team_name to_team
        FROM transfer_history th JOIN players p ON p.player_id=th.player_id
        LEFT JOIN teams ft ON ft.team_id=th.from_team_id LEFT JOIN teams tt ON tt.team_id=th.to_team_id
        ORDER BY th.transfer_timestamp DESC LIMIT 80""")
    return jsonify(success=True, transfers=rows)

@app.route("/api/transfers/request", methods=["POST"])
@role_required("ADMIN","OFFICER","COACH")
def transfer_request():
    if get_setting("transfer_window_active","1")!="1":
        return jsonify(success=False,error="Transfer window closed"),403
    d=request.json or {}
    license_no=(d.get("license") or "").strip()
    rtype=(d.get("request_type") or "PULL").upper()
    other=d.get("other_team_id") or d.get("to_team_id")
    if not license_no or not other:
        return jsonify(success=False,error="license and other_team_id required"),400
    if rtype not in ("PULL","OFFER"):
        return jsonify(success=False,error="request_type must be PULL or OFFER"),400
    player=q1("SELECT * FROM players WHERE global_license_number=?",(license_no,))
    if not player: return jsonify(success=False,error="Player license not found"),404
    sy=season_start_year()
    reg=q1("SELECT * FROM annual_registrations WHERE player_id=? AND season_year=? AND is_active_roster=1",(player["player_id"],sy))
    if not reg:
        # fallback legacy season constant
        reg=q1("SELECT * FROM annual_registrations WHERE player_id=? AND season_year=? AND is_active_roster=1",(player["player_id"],SEASON))
    if not reg: return jsonify(success=False,error="Player not on active roster"),404
    if (reg.get("registration_status") or "APPROVED") != "APPROVED":
        return jsonify(success=False,error="Player registration is not approved yet"),403
    u=current_user()
    current_team=reg["team_id"]
    other=int(other)
    # Resolve from/to and fee payer
    if rtype=="PULL":
        # Bring player TO requester's team (or specified for admin/officer)
        if u["role"]=="COACH":
            if current_team==u.get("team_id"):
                return jsonify(success=False,error="Pull: select a player from another team"),400
            to_team=u["team_id"]
            from_team=current_team
        else:
            to_team=other if other!=current_team else None
            if not to_team:
                return jsonify(success=False,error="Select the destination team"),400
            from_team=current_team
        fee_payer=to_team  # requesting/receiving coach pays
        from_agreed, to_agreed = 0, 1 if u["role"]=="COACH" else 0  # requester already wants them
    else:  # OFFER
        if u["role"]=="COACH" and current_team!=u.get("team_id"):
            return jsonify(success=False,error="Offer: you can only offer players from your own team"),403
        from_team=current_team
        to_team=other
        if from_team==to_team: return jsonify(success=False,error="Same team"),400
        fee_payer=to_team  # receiving team pays
        from_agreed, to_agreed = 1 if u["role"]=="COACH" else 0, 0
    if from_team==to_team: return jsonify(success=False,error="Same team"),400
    wtype=get_setting("transfer_window_type","MID_SEASON")
    if wtype=="MID_SEASON":
        cnt=q1("SELECT COUNT(*) c FROM transfer_history WHERE to_team_id=? AND window_type='MID_SEASON' AND approved_by_admin=1",(to_team,))
        if cnt and cnt["c"]>=2: return jsonify(success=False,error="Max 2 mid-season incoming for destination"),403
    conn=get_conn()
    cur=conn.execute("""INSERT INTO transfer_history(player_id,from_team_id,to_team_id,window_type,fee_paid_status,
        from_coach_agreed,to_coach_agreed,approved_by_admin,status,request_type,fee_payer_team_id,requested_by,notes)
        VALUES(?,?,?,?,'PENDING',?,?,0,'PENDING',?,?,?,?)""",
        (player["player_id"],from_team,to_team,wtype,from_agreed,to_agreed,rtype,fee_payer,u["user_id"],d.get("notes")))
    conn.commit(); tid=cur.lastrowid; conn.close()
    fee=fees()["transfer"]
    if rtype=="PULL":
        msg=f"Pull request: {player['legal_name']} → your team. Releasing coach must agree; you pay E{fee}."
        notify(coach_user_for_team(from_team), "Pull request on your player",
               f"{player['legal_name']} ({license_no}) — agree to release?", "transfer", tid)
    else:
        msg=f"Offer: {player['legal_name']} offered. Receiving coach must accept and pay E{fee}."
        notify(coach_user_for_team(to_team), "Player offered to your team",
               f"{player['legal_name']} ({license_no}) — accept and pay E{fee}?", "transfer", tid)
    for adm in q("SELECT user_id FROM users WHERE role='ADMIN' AND is_active=1"):
        notify(adm["user_id"], f"Transfer {rtype}", f"{player['legal_name']} ({license_no})", "transfer", tid)
    tr=q1("SELECT * FROM transfer_history WHERE transfer_id=?",(tid,))
    return jsonify(success=True, transfer=tr, message=msg),201

@app.route("/api/transfers/<int:tid>/agree", methods=["POST"])
@role_required("ADMIN","COACH")
def transfer_agree(tid):
    tr=q1("SELECT * FROM transfer_history WHERE transfer_id=?",(tid,))
    if not tr: return jsonify(success=False,error="Not found"),404
    if tr["status"]=="APPROVED": return jsonify(success=False,error="Already approved"),400
    u=current_user(); side=(request.json or {}).get("side")
    if u["role"]=="ADMIN":
        # Admin can force-agree either side for testing
        if side=="from": q("UPDATE transfer_history SET from_coach_agreed=1 WHERE transfer_id=?",(tid,),fetch=False)
        elif side=="to": q("UPDATE transfer_history SET to_coach_agreed=1 WHERE transfer_id=?",(tid,),fetch=False)
        else:
            q("UPDATE transfer_history SET from_coach_agreed=1,to_coach_agreed=1 WHERE transfer_id=?",(tid,),fetch=False)
    else:
        if side=="from" and u.get("team_id")==tr["from_team_id"]:
            q("UPDATE transfer_history SET from_coach_agreed=1 WHERE transfer_id=?",(tid,),fetch=False)
        elif side=="to" and u.get("team_id")==tr["to_team_id"]:
            q("UPDATE transfer_history SET to_coach_agreed=1 WHERE transfer_id=?",(tid,),fetch=False)
        else:
            return jsonify(success=False,error="Not your side of this transfer"),403
    tr=q1("SELECT * FROM transfer_history WHERE transfer_id=?",(tid,))
    if tr["from_coach_agreed"] and tr["to_coach_agreed"]:
        q("UPDATE transfer_history SET status='AWAITING_PAYMENT' WHERE transfer_id=?",(tid,),fetch=False)
        for uid in filter(None,[coach_user_for_team(tr["from_team_id"]),coach_user_for_team(tr["to_team_id"])]):
            notify(uid,"Both coaches agreed",f"Transfer #{tid}: pay fee E{fees()['transfer']} to proceed.","transfer",tid)
    return jsonify(success=True, message="Agreement recorded", transfer=q1("SELECT * FROM transfer_history WHERE transfer_id=?",(tid,)))

@app.route("/api/transfers/<int:tid>/pay", methods=["POST"])
@login_required
def transfer_pay(tid):
    tr=q1("SELECT * FROM transfer_history WHERE transfer_id=?",(tid,))
    if not tr: return jsonify(success=False,error="Not found"),404
    if not (tr["from_coach_agreed"] and tr["to_coach_agreed"]):
        return jsonify(success=False,error="Both coaches must agree first"),403
    u=current_user()
    payer=tr.get("fee_payer_team_id")
    if u["role"]=="COACH" and u.get("team_id")!=payer:
        return jsonify(success=False,error="Only the fee-paying team coach can pay this transfer fee"),403
    phone=(request.json or {}).get("phone") or "000"
    fee=fees()["transfer"]
    ref=gen_ref()
    q("""INSERT INTO financial_ledger(reference_number,amount,payment_type,payment_status,payer_phone_number,related_player_id,related_transfer_id)
        VALUES(?,?,'TRANSFER_FEE','SUCCESSFUL',?,?,?)""",(ref,fee,phone,tr["player_id"],tid),fetch=False)
    q("UPDATE transfer_history SET fee_paid_status='PAID',status='AWAITING_ADMIN' WHERE transfer_id=?",(tid,),fetch=False)
    for adm in q("SELECT user_id FROM users WHERE role='ADMIN' AND is_active=1"):
        notify(adm["user_id"],"Transfer fee paid",f"Transfer #{tid} ready for admin approval.","transfer",tid)
    for uid in filter(None,[coach_user_for_team(tr["from_team_id"]),coach_user_for_team(tr["to_team_id"])]):
        notify(uid,"Transfer fee paid",f"Transfer #{tid}: awaiting Admin final approval.","transfer",tid)
    return jsonify(success=True, message=f"Fee E{fee} recorded. Awaiting Admin approval.", systemReference=ref)

@app.route("/api/transfers/<int:tid>/approve", methods=["POST"])
@role_required("ADMIN")
def transfer_approve(tid):
    tr=q1("SELECT * FROM transfer_history WHERE transfer_id=?",(tid,))
    if not tr: return jsonify(success=False,error="Not found"),404
    if tr["approved_by_admin"]: return jsonify(success=False,error="Already approved"),400
    if not (tr["from_coach_agreed"] and tr["to_coach_agreed"]):
        return jsonify(success=False,error="Both coaches must agree"),403
    if tr["fee_paid_status"]!="PAID":
        return jsonify(success=False,error="Transfer fee not paid"),402
    conn=get_conn()
    conn.execute("UPDATE annual_registrations SET is_active_roster=0 WHERE player_id=? AND team_id=? AND season_year=?",
                 (tr["player_id"],tr["from_team_id"],SEASON))
    ex=q1("SELECT registration_id FROM annual_registrations WHERE player_id=? AND season_year=?",(tr["player_id"],SEASON))
    if ex:
        conn.execute("UPDATE annual_registrations SET team_id=?,is_active_roster=1,player_fee_status='PAID' WHERE registration_id=?",
                     (tr["to_team_id"],ex["registration_id"]))
    else:
        conn.execute("INSERT INTO annual_registrations(player_id,team_id,season_year,is_active_roster,player_fee_status) VALUES(?,?,?,1,'PAID')",
                     (tr["player_id"],tr["to_team_id"],SEASON))
    conn.execute("UPDATE transfer_history SET approved_by_admin=1,status='APPROVED' WHERE transfer_id=?",(tid,))
    conn.commit(); conn.close()
    for uid in filter(None,[coach_user_for_team(tr["from_team_id"]),coach_user_for_team(tr["to_team_id"])]):
        notify(uid,"Transfer approved",f"Transfer #{tid} completed. Player moved.","transfer",tid)
    return jsonify(success=True, message="Transfer approved. Player moved.")

try:
    init_db()  # WSGI / PythonAnywhere: create tables on load
except Exception as _e:
    print("init_db warning:", _e)

if __name__=="__main__":
    init_db()
    ips=[]
    try:
        s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.connect(("8.8.8.8",80)); ips.append(s.getsockname()[0]); s.close()
    except Exception: pass
    print("="*60)
    print("  ESWATINI HANDBALL ASSOCIATION – Registration v1.3")
    print("  http://localhost:3000")
    for ip in ips: print(f"  LAN http://{ip}:3000")
    print("  Sign in with your organisation accounts (no credentials shown in UI)")
    print("="*60)
    app.run(host="0.0.0.0", port=3000, debug=False)
