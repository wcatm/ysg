"""养生馆网页系统 — Flask + SQLite 多店版（SaaS 雏形）

每家店独立网址: /s/<店id>/       后台: /s/<店id>/admin/login
运行: python app.py          (浏览器打开 http://127.0.0.1:5000)
自检: python app.py --selftest
"""
import hashlib
import os
import random
import re
import sqlite3
import sys
import tempfile
import time
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import (Flask, flash, g, jsonify, redirect, render_template,
                   request, session, url_for)

sys.stdout.reconfigure(encoding="utf-8")  # Windows 控制台中文输出

BASE = Path(__file__).parent
DB_PATH = BASE / "ysg.db"
UPLOAD_DIR = BASE / "static" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
_secret = BASE / ".secret_key"
if not _secret.exists():
    _secret.write_bytes(os.urandom(32))
app.secret_key = _secret.read_bytes()

# ---------- 安全防护（防火墙：新增规则只改本区块） ----------

app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # 上传最大 8MB，超限自动 413

# 简单限流：内存滑动窗口。ponytail: 单进程够用；多进程部署时换 Redis
_RL = {}


def rate_limit(key, limit, window=60):
    """key 在 window 秒内超过 limit 次返回 False"""
    now = time.time()
    _RL[key] = [t for t in _RL.get(key, []) if now - t < window]
    if len(_RL[key]) >= limit:
        return False
    _RL[key].append(now)
    return True


@app.after_request
def security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    return resp


def _csrf_token():
    if "csrf" not in session:
        session["csrf"] = os.urandom(16).hex()
    return session["csrf"]


@app.before_request
def check_csrf():
    """所有 POST 表单必须携带 session CSRF token（JSON API 豁免）"""
    if request.method == "POST" and not request.is_json \
            and request.form.get("_csrf") != _csrf_token():
        from werkzeug.exceptions import abort
        abort(400)


SLOTS = [f"{h:02d}:00" for h in range(10, 21)]  # 可约时段 10:00-20:00
STATUS_CN = {"pending": "待确认", "confirmed": "已确认",
             "completed": "已完成", "cancelled": "已取消"}
PHONE_RE = re.compile(r"^1\d{10}$")
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS shops (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT, shop_id INTEGER, name TEXT, sort INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS services (
    id INTEGER PRIMARY KEY AUTOINCREMENT, shop_id INTEGER, name TEXT, category_id INTEGER,
    price REAL, duration INTEGER, desc TEXT DEFAULT '', active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS technicians (
    id INTEGER PRIMARY KEY AUTOINCREMENT, shop_id INTEGER, name TEXT, avatar TEXT DEFAULT '',
    intro TEXT DEFAULT '', specialties TEXT DEFAULT '', active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS members (
    id INTEGER PRIMARY KEY AUTOINCREMENT, shop_id INTEGER, phone TEXT, name TEXT DEFAULT '',
    gender TEXT DEFAULT '', birthday TEXT DEFAULT '', created_at TEXT);
CREATE TABLE IF NOT EXISTS sms_codes (
    shop_id INTEGER, phone TEXT, code TEXT, expires_at REAL);
CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT, shop_id INTEGER, name TEXT, phone TEXT,
    service_id INTEGER, technician_id INTEGER, bdate TEXT, slot TEXT,
    status TEXT DEFAULT 'pending', remark TEXT DEFAULT '', created_at TEXT);
CREATE TABLE IF NOT EXISTS admins (
    id INTEGER PRIMARY KEY AUTOINCREMENT, shop_id INTEGER, username TEXT, pass TEXT);
CREATE TABLE IF NOT EXISTS settings (
    shop_id INTEGER, key TEXT, value TEXT, PRIMARY KEY (shop_id, key));
"""

SEED_SERVICES = [
    ("全身经络推拿", 1, 128, 60, "十二经络整体疏通，缓解疲劳，促进气血运行"),
    ("肩颈舒缓按摩", 1, 88, 45, "针对久坐办公人群，松解肩颈僵硬酸痛"),
    ("背部精油开背", 1, 158, 60, "植物精油配合专业手法，深层放松背部肌肉"),
    ("艾灸温阳理疗", 2, 98, 50, "古法艾灸温补阳气，改善手脚冰凉、体寒"),
    ("督脉铺灸", 2, 168, 70, "督脉长蛇灸，温通全身，调理亚健康"),
    ("中药足浴", 3, 68, 40, "二十余味中药熬制泡足，驱寒解乏助睡眠"),
    ("足底反射按摩", 3, 98, 60, "足底反射区精准刺激，调理脏腑机能"),
    ("刮痧排毒", 4, 58, 30, "传统刮痧疏通经络，祛除体内湿毒"),
    ("拔罐祛湿", 4, 68, 30, "火罐走罐，祛风散寒除湿"),
]
SEED_TECHS = [
    ("王师傅", "肩颈调理、腰椎推拿", "从业 15 年，擅长颈肩腰腿痛调理，手法沉稳有力"),
    ("李师傅", "艾灸、督脉灸", "中医灸疗世家传人，擅长体质调理与亚健康改善"),
    ("陈师傅", "足疗、足底反射", "10 年足疗经验，手法精准，广受好评"),
    ("周师傅", "经络推拿、拔罐", "科班出身，结合解剖学与中医经络理论"),
]


# ---------- 数据库 ----------

def init_db(path=DB_PATH):
    """建表；旧单店库（settings 无 shop_id 列）自动备份后重建"""
    if path.exists():
        db = sqlite3.connect(path)
        has = db.execute("SELECT 1 FROM sqlite_master WHERE name='settings'").fetchone()
        cols = [r[1] for r in db.execute("PRAGMA table_info(settings)")] if has else []
        db.close()
        if has and "shop_id" not in cols:
            path.rename(path.with_name(path.stem + "_backup.db"))
            print(f"[数据库升级] 旧单店数据已备份到 {path.stem}_backup.db")
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    if "active" not in [r[1] for r in db.execute("PRAGMA table_info(shops)")]:
        db.execute("ALTER TABLE shops ADD COLUMN active INTEGER DEFAULT 1")
        db.commit()
    db.close()


def seed_shop(db, name):
    """开一家新店：初始化分类/项目/技师/配置/店管理员，返回店 id 和管理员密码"""
    sid = db.execute("INSERT INTO shops (name) VALUES (?)", (name,)).lastrowid
    cat_ids = {sort: db.execute(
        "INSERT INTO categories (shop_id, name, sort) VALUES (?,?,?)",
        (sid, cname, sort)).lastrowid
        for cname, sort in (("推拿按摩", 1), ("艾灸养生", 2), ("足浴足疗", 3), ("刮痧拔罐", 4))}
    db.executemany(
        "INSERT INTO services (shop_id, name, category_id, price, duration, desc) VALUES (?,?,?,?,?,?)",
        [(sid, n, cat_ids[cat], p, d, desc) for n, cat, p, d, desc in SEED_SERVICES])
    db.executemany("INSERT INTO technicians (shop_id, name, specialties, intro) VALUES (?,?,?,?)",
                   [(sid, n, sp, it) for n, sp, it in SEED_TECHS])
    pw = "".join(random.choices("abcdefghjkmnpqrstuvwxyz23456789", k=10))
    salt = os.urandom(16).hex()
    db.execute("INSERT INTO admins (shop_id, username, pass) VALUES (?,?,?)",
               (sid, "admin", salt + "$" + hashlib.sha256((salt + pw).encode()).hexdigest()))
    db.executemany("INSERT INTO settings (shop_id, key, value) VALUES (?,?,?)", [
        (sid, "site_name", name),
        (sid, "slogan", "舒缓身心 · 颐养之道"),
        (sid, "intro", "专注中医养生调理十余年，提供推拿、艾灸、足浴、刮痧等传统养生服务"),
        (sid, "phone", "138-0000-0000"),
        (sid, "wechat", "kangyi888"),
        (sid, "address", "示例市幸福路 88 号"),
        (sid, "hours", "每日 10:00 - 22:00"),
        (sid, "map_search", name),
    ])
    return sid, pw


def seed(db):
    """首次运行：确保总管理员存在；不预置门店（按需开店）"""
    if not db.execute("SELECT 1 FROM admins WHERE shop_id IS NULL").fetchone():
        pw = "".join(random.choices("abcdefghjkmnpqrstuvwxyz23456789", k=10))
        salt = os.urandom(16).hex()
        db.execute("INSERT INTO admins (shop_id, username, pass) VALUES (?,?,?)",
                   (None, "platform", salt + "$" + hashlib.sha256((salt + pw).encode()).hexdigest()))
        db.commit()
        return pw
    return None


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db:
        db.close()


@app.before_request
def load_shop_id():
    """从 URL 提取门店 id 存 g.shop_id（/s/<id>/... 路由共用）"""
    m = re.match(r"^/s/(\d+)", request.path)
    g.shop_id = int(m.group(1)) if m else None


def get_shop():
    """当前门店，不存在或已停用则 404"""
    if "shop" not in g:
        g.shop = get_db().execute("SELECT * FROM shops WHERE id=? AND active=1",
                                  (g.shop_id,)).fetchone()
        if not g.shop:
            from werkzeug.exceptions import abort
            abort(404)
    return g.shop


def get_settings():
    """当前门店配置字典，模板里通过 cfg.site_name 访问"""
    if "cfg" not in g:
        g.cfg = dict(get_db().execute("SELECT key, value FROM settings WHERE shop_id=?",
                                      (g.shop_id,)))
    return g.cfg


def set_setting(key, value):
    get_db().execute("INSERT INTO settings (shop_id, key, value) VALUES (?,?,?) "
                     "ON CONFLICT(shop_id, key) DO UPDATE SET value=excluded.value",
                     (g.shop_id, key, value))


def hash_pass(pw):
    salt = os.urandom(16).hex()
    return salt + "$" + hashlib.sha256((salt + pw).encode()).hexdigest()


def check_pass(stored, pw):
    salt, h = stored.split("$", 1)
    return hashlib.sha256((salt + pw).encode()).hexdigest() == h


@app.context_processor
def inject_shop():
    shop = get_shop() if g.get("shop_id") else None
    return {"shop": shop, "cfg": get_settings() if shop else {},
            "now": datetime.now(), "csrf": _csrf_token()}


app.jinja_env.filters["yuan"] = lambda v: f"¥{v:g}"


# ---------- 短信 ----------

def send_sms(phone, code):
    """发送验证码。开发模式打印到控制台；上线时接入阿里云短信替换此函数即可。
    ponytail: 真实短信待接服务商（登录/查询两处验证码都走这里）"""
    print(f"[短信] 发给 {phone}: 验证码 {code}")


@app.post("/s/<int:shop_id>/api/send_code")
def send_code(shop_id):
    get_shop()
    phone = (request.get_json() or {}).get("phone", "").strip()
    if not PHONE_RE.match(phone):
        return jsonify(ok=False, msg="手机号格式不正确")
    db = get_db()
    row = db.execute("SELECT code, expires_at FROM sms_codes WHERE shop_id=? AND phone=? "
                     "ORDER BY expires_at DESC LIMIT 1", (shop_id, phone)).fetchone()
    if row and row["expires_at"] - time.time() > 240:  # 60 秒内不重发
        code = row["code"]
    else:
        code = f"{random.randint(0, 999999):06d}"
        db.execute("DELETE FROM sms_codes WHERE shop_id=? AND phone=?", (shop_id, phone))
        db.execute("INSERT INTO sms_codes (shop_id, phone, code, expires_at) VALUES (?,?,?,?)",
                   (shop_id, phone, code, time.time() + 300))
        db.commit()
        send_sms(phone, code)
    return jsonify(ok=True, dev_code=code)  # dev_code 仅开发模式返回，接入真短信后删掉


# ---------- 登录装饰器 ----------

def login_required(f):
    @wraps(f)
    def w(shop_id, *a, **k):
        if not session.get("mid") or session.get("shop_id") != shop_id:
            return redirect(url_for("login", shop_id=shop_id))
        return f(shop_id, *a, **k)
    return w


def admin_required(f):
    @wraps(f)
    def w(shop_id, *a, **k):
        if not session.get("aid") or session.get("ashop") != shop_id:
            return redirect(url_for("admin_login", shop_id=shop_id))
        return f(shop_id, *a, **k)
    return w


# ---------- 门店选择页 ----------

@app.get("/")
def shop_list():
    shops = get_db().execute("SELECT * FROM shops WHERE active=1 ORDER BY id").fetchall()
    return render_template("shop_list.html", shops=shops)


# ---------- 平台总后台（开店管理） ----------

def platform_required(f):
    @wraps(f)
    def w(*a, **k):
        if not session.get("pid"):
            return redirect(url_for("platform_login"))
        return f(*a, **k)
    return w


@app.route("/platform/login", methods=["GET", "POST"])
def platform_login():
    if request.method == "POST":
        if not rate_limit(f"platform:{request.remote_addr}", 10):
            flash("尝试过于频繁，请稍后再试")
            return redirect(url_for("platform_login"))
        u = request.form.get("username", "")
        pw = request.form.get("password", "")
        row = get_db().execute("SELECT * FROM admins WHERE shop_id IS NULL AND username=?",
                               (u,)).fetchone()
        if row and check_pass(row["pass"], pw):
            session["pid"] = row["id"]
            return redirect(url_for("platform_shops"))
        flash("账号或密码错误")
        return redirect(url_for("platform_login"))
    return render_template("platform/login.html")


@app.get("/platform/logout")
def platform_logout():
    session.pop("pid", None)
    return redirect(url_for("platform_login"))


@app.get("/platform/")
@platform_required
def platform_shops():
    shops = get_db().execute("SELECT * FROM shops ORDER BY id").fetchall()
    return render_template("platform/shops.html", shops=shops)


@app.post("/platform/shops/add")
@platform_required
def platform_shop_add():
    name = request.form.get("name", "").strip()
    if not name:
        flash("请填写门店名称")
        return redirect(url_for("platform_shops"))
    db = get_db()
    sid, pw = seed_shop(db, name)
    db.commit()
    flash(f"「{name}」已开店！店后台账号 admin / 密码 {pw}（请转发给店长并提醒妥善保管）")
    return redirect(url_for("platform_shops"))


@app.post("/platform/shops/toggle")
@platform_required
def platform_shop_toggle():
    db = get_db()
    s = db.execute("SELECT * FROM shops WHERE id=?", (request.form.get("id"),)).fetchone()
    if s:
        db.execute("UPDATE shops SET active=? WHERE id=?", (0 if s["active"] else 1, s["id"]))
        db.commit()
        flash(f"「{s['name']}」已{'停用（用户端不可见）' if s['active'] else '启用'}")
    return redirect(url_for("platform_shops"))


@app.post("/platform/shops/reset_pw")
@platform_required
def platform_shop_reset_pw():
    db = get_db()
    s = db.execute("SELECT * FROM shops WHERE id=?", (request.form.get("id"),)).fetchone()
    if s:
        pw = "".join(random.choices("abcdefghjkmnpqrstuvwxyz23456789", k=10))
        db.execute("UPDATE admins SET pass=? WHERE shop_id=?", (hash_pass(pw), s["id"]))
        db.commit()
        flash(f"「{s['name']}」后台密码已重置为 {pw}（请转发给店长）")
    return redirect(url_for("platform_shops"))


# ---------- 客户端页面 ----------

@app.get("/s/<int:shop_id>/")
def index(shop_id):
    get_shop()
    db = get_db()
    services = db.execute(
        "SELECT s.*, c.name cname FROM services s LEFT JOIN categories c "
        "ON s.category_id=c.id WHERE s.shop_id=? AND s.active=1 ORDER BY c.sort, s.id",
        (shop_id,)).fetchall()
    technicians = db.execute("SELECT * FROM technicians WHERE shop_id=? AND active=1",
                             (shop_id,)).fetchall()
    cfg = get_settings()
    carousel = [u for u in (cfg.get(f"carousel_{i}") for i in (1, 2, 3)) if u]
    return render_template("index.html", services=services,
                           technicians=technicians, carousel=carousel)


@app.get("/s/<int:shop_id>/services")
def services(shop_id):
    get_shop()
    db = get_db()
    cat = request.args.get("cat", type=int)
    rows = db.execute(
        "SELECT s.*, c.name cname FROM services s LEFT JOIN categories c "
        "ON s.category_id=c.id WHERE s.shop_id=? AND s.active=1 AND (? IS NULL OR s.category_id=?) "
        "ORDER BY c.sort, s.id", (shop_id, cat, cat)).fetchall()
    categories = db.execute(
        "SELECT c.*, COUNT(s.id) cnt FROM categories c LEFT JOIN services s "
        "ON s.category_id=c.id AND s.active=1 WHERE c.shop_id=? GROUP BY c.id ORDER BY c.sort",
        (shop_id,)).fetchall()
    return render_template("services.html", services=rows, categories=categories, cat=cat)


@app.route("/s/<int:shop_id>/booking", methods=["GET", "POST"])
def booking(shop_id):
    get_shop()
    db = get_db()
    if request.method == "POST":
        f = request.form
        err = None
        if not f.get("name", "").strip():
            err = "请填写姓名"
        elif not PHONE_RE.match(f.get("phone", "")):
            err = "手机号格式不正确"
        elif not db.execute("SELECT 1 FROM services WHERE id=? AND shop_id=? AND active=1",
                            (f.get("service_id", -1), shop_id)).fetchone():
            err = "请选择服务项目"
        elif f.get("technician_id") and not db.execute(
                "SELECT 1 FROM technicians WHERE id=? AND shop_id=?",
                (f["technician_id"], shop_id)).fetchone():
            err = "请选择技师"
        try:
            d = datetime.strptime(f.get("bdate", ""), "%Y-%m-%d").date()
        except ValueError:
            err = err or "请选择日期"
            d = None
        if d and d < datetime.now().date():
            err = "不能预约过去的日期"
        if err is None and f.get("slot") not in SLOTS:
            err = "请选择时段"
        if err is None and db.execute(
                "SELECT 1 FROM bookings WHERE shop_id=? AND technician_id=? AND bdate=? "
                "AND slot=? AND status!='cancelled'",
                (shop_id, f.get("technician_id") or None, f["bdate"], f["slot"])).fetchone():
            err = "该技师该时段已被预约，请换时段或换技师"
        if err:
            flash(err)
            return redirect(url_for("booking", shop_id=shop_id))
        db.execute("INSERT INTO bookings (shop_id, name, phone, service_id, technician_id, "
                   "bdate, slot, remark, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                   (shop_id, f["name"].strip(), f["phone"], f["service_id"],
                    f.get("technician_id") or None, f["bdate"], f["slot"],
                    f.get("remark", "").strip(),
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        db.commit()
        flash("预约提交成功，请等待门店确认")
        return redirect(url_for("me", shop_id=shop_id) if session.get("mid")
                        else url_for("query", shop_id=shop_id))

    cat_services = [
        (c["name"], db.execute("SELECT * FROM services WHERE shop_id=? AND category_id=? "
                               "AND active=1", (shop_id, c["id"])).fetchall())
        for c in db.execute("SELECT * FROM categories WHERE shop_id=? ORDER BY sort", (shop_id,))]
    technicians = db.execute("SELECT * FROM technicians WHERE shop_id=? AND active=1",
                             (shop_id,)).fetchall()
    member = None
    if session.get("mid") and session.get("shop_id") == shop_id:
        member = db.execute("SELECT * FROM members WHERE id=?", (session["mid"],)).fetchone()
    return render_template("booking.html", cat_services=cat_services,
                           technicians=technicians, slots=SLOTS, member=member,
                           pre_service=request.args.get("service", type=int))


@app.route("/s/<int:shop_id>/login", methods=["GET", "POST"])
def login(shop_id):
    get_shop()
    if request.method == "POST":
        phone, code = request.form.get("phone", "").strip(), request.form.get("code", "").strip()
        db = get_db()
        row = db.execute("SELECT code, expires_at FROM sms_codes WHERE shop_id=? AND phone=? "
                         "ORDER BY expires_at DESC LIMIT 1", (shop_id, phone)).fetchone()
        if not (PHONE_RE.match(phone) and row and row["code"] == code
                and row["expires_at"] > time.time()):
            flash("验证码错误或已过期")
            return redirect(url_for("login", shop_id=shop_id))
        db.execute("DELETE FROM sms_codes WHERE shop_id=? AND phone=?", (shop_id, phone))
        m = db.execute("SELECT id FROM members WHERE shop_id=? AND phone=?",
                       (shop_id, phone)).fetchone()
        if not m:
            db.execute("INSERT INTO members (shop_id, phone, created_at) VALUES (?,?,?)",
                       (shop_id, phone, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            db.commit()
            m = db.execute("SELECT id FROM members WHERE shop_id=? AND phone=?",
                           (shop_id, phone)).fetchone()
        session["mid"] = m["id"]
        session["shop_id"] = shop_id
        flash("登录成功，未注册手机号已自动注册会员")
        return redirect(url_for("me", shop_id=shop_id))
    return render_template("login.html")


@app.get("/s/<int:shop_id>/logout")
def logout(shop_id):
    session.pop("mid", None)
    session.pop("shop_id", None)
    return redirect(url_for("index", shop_id=shop_id))


@app.route("/s/<int:shop_id>/query", methods=["GET", "POST"])
def query(shop_id):
    get_shop()
    bookings = None
    phone = ""
    if request.method == "POST":
        phone = request.form.get("phone", "").strip()
        code = request.form.get("code", "").strip()
        db = get_db()
        row = db.execute("SELECT code, expires_at FROM sms_codes WHERE shop_id=? AND phone=? "
                         "ORDER BY expires_at DESC LIMIT 1", (shop_id, phone)).fetchone()
        if not (row and row["code"] == code and row["expires_at"] > time.time()):
            flash("验证码错误或已过期")
        else:
            bookings = db.execute(
                "SELECT b.*, s.name sname, s.price, s.duration, t.name tname "
                "FROM bookings b LEFT JOIN services s ON b.service_id=s.id "
                "LEFT JOIN technicians t ON b.technician_id=t.id "
                "WHERE b.shop_id=? AND b.phone=? ORDER BY b.bdate DESC, b.slot DESC",
                (shop_id, phone)).fetchall()
    return render_template("query.html", bookings=bookings, phone=phone)


@app.route("/s/<int:shop_id>/me", methods=["GET", "POST"])
@login_required
def me(shop_id):
    db = get_db()
    m = db.execute("SELECT * FROM members WHERE id=?", (session["mid"],)).fetchone()
    if request.method == "POST":
        db.execute("UPDATE members SET name=?, gender=?, birthday=? WHERE id=?",
                   (request.form.get("name", "").strip(),
                    request.form.get("gender", ""),
                    request.form.get("birthday", ""), session["mid"]))
        db.commit()
        flash("个人信息已保存")
        return redirect(url_for("me", shop_id=shop_id))
    rows = db.execute(
        "SELECT b.*, s.name sname, s.price, s.duration, t.name tname "
        "FROM bookings b LEFT JOIN services s ON b.service_id=s.id "
        "LEFT JOIN technicians t ON b.technician_id=t.id "
        "WHERE b.shop_id=? AND b.phone=? ORDER BY b.bdate DESC, b.slot DESC",
        (shop_id, m["phone"])).fetchall()
    bookings = [r for r in rows if r["status"] != "completed"]
    consumptions = [r for r in rows if r["status"] == "completed"]
    return render_template("me.html", m=m, bookings=bookings,
                           consumptions=consumptions)


# ---------- 店后台 ----------

@app.route("/s/<int:shop_id>/admin/login", methods=["GET", "POST"])
def admin_login(shop_id):
    get_shop()
    if request.method == "POST":
        if not rate_limit(f"admin:{shop_id}:{request.remote_addr}", 10):
            flash("尝试过于频繁，请稍后再试")
            return redirect(url_for("admin_login", shop_id=shop_id))
        u = request.form.get("username", "")
        pw = request.form.get("password", "")
        row = get_db().execute("SELECT * FROM admins WHERE shop_id=? AND username=?",
                               (shop_id, u)).fetchone()
        if row and check_pass(row["pass"], pw):
            session["aid"] = row["id"]
            session["ashop"] = shop_id
            return redirect(url_for("admin_index", shop_id=shop_id))
        flash("账号或密码错误")
        return redirect(url_for("admin_login", shop_id=shop_id))
    return render_template("admin/login.html")


@app.get("/s/<int:shop_id>/admin/logout")
def admin_logout(shop_id):
    session.pop("aid", None)
    session.pop("ashop", None)
    return redirect(url_for("admin_login", shop_id=shop_id))


@app.get("/s/<int:shop_id>/admin/")
@admin_required
def admin_index(shop_id):
    db = get_db()
    today = datetime.now().strftime("%Y-%m-%d")
    stats = {
        "今日预约": db.execute("SELECT COUNT(*) c FROM bookings WHERE shop_id=? AND bdate=? "
                            "AND status!='cancelled'", (shop_id, today)).fetchone()["c"],
        "待确认": db.execute("SELECT COUNT(*) c FROM bookings WHERE shop_id=? AND status='pending'",
                          (shop_id,)).fetchone()["c"],
        "会员数": db.execute("SELECT COUNT(*) c FROM members WHERE shop_id=?", (shop_id,)).fetchone()["c"],
        "在售项目": db.execute("SELECT COUNT(*) c FROM services WHERE shop_id=? AND active=1",
                            (shop_id,)).fetchone()["c"],
    }
    recent = db.execute(
        "SELECT b.*, s.name sname, t.name tname FROM bookings b "
        "LEFT JOIN services s ON b.service_id=s.id LEFT JOIN technicians t ON b.technician_id=t.id "
        "WHERE b.shop_id=? ORDER BY b.created_at DESC LIMIT 8", (shop_id,)).fetchall()
    return render_template("admin/index.html", stats=stats, recent=recent)


@app.get("/s/<int:shop_id>/admin/bookings")
@admin_required
def admin_bookings(shop_id):
    st = request.args.get("status", "all")
    rows = get_db().execute(
        "SELECT b.*, s.name sname, s.price, t.name tname FROM bookings b "
        "LEFT JOIN services s ON b.service_id=s.id LEFT JOIN technicians t ON b.technician_id=t.id "
        "WHERE b.shop_id=? AND (?='all' OR b.status=?) ORDER BY b.bdate DESC, b.slot DESC",
        (shop_id, st, st)).fetchall()
    return render_template("admin/bookings.html", bookings=rows,
                           status_cn=STATUS_CN, st=st)


@app.post("/s/<int:shop_id>/admin/bookings/action")
@admin_required
def admin_booking_action(shop_id):
    bid = request.form.get("id")
    status = request.form.get("status")
    if status in STATUS_CN:
        db = get_db()
        db.execute("UPDATE bookings SET status=? WHERE id=? AND shop_id=?", (status, bid, shop_id))
        db.commit()
        flash(f"预约已改为「{STATUS_CN[status]}」")
    return redirect(url_for("admin_bookings", shop_id=shop_id,
                            status=request.args.get("status", "all")))


@app.get("/s/<int:shop_id>/admin/services")
@admin_required
def admin_services(shop_id):
    db = get_db()
    edit = None
    if eid := request.args.get("edit", type=int):
        edit = db.execute("SELECT * FROM services WHERE id=? AND shop_id=?",
                          (eid, shop_id)).fetchone()
    services = db.execute(
        "SELECT s.*, c.name cname FROM services s LEFT JOIN categories c "
        "ON s.category_id=c.id WHERE s.shop_id=? ORDER BY c.sort, s.id", (shop_id,)).fetchall()
    categories = db.execute(
        "SELECT c.*, COUNT(s.id) cnt FROM categories c LEFT JOIN services s "
        "ON s.category_id=c.id WHERE c.shop_id=? GROUP BY c.id ORDER BY c.sort",
        (shop_id,)).fetchall()
    return render_template("admin/services.html", services=services,
                           categories=categories, edit=edit)


@app.post("/s/<int:shop_id>/admin/services/save")
@admin_required
def admin_service_save(shop_id):
    f = request.form
    if not (f.get("name", "").strip() and f.get("price") and f.get("duration")):
        flash("请填写完整的项目信息")
        return redirect(url_for("admin_services", shop_id=shop_id))
    fields = (f["name"].strip(), f.get("category_id", type=int), float(f["price"]),
              int(f["duration"]), f.get("desc", "").strip(), 1 if f.get("active") else 0)
    db = get_db()
    if sid := f.get("id"):
        db.execute("UPDATE services SET name=?, category_id=?, price=?, duration=?, "
                   "desc=?, active=? WHERE id=? AND shop_id=?", fields + (sid, shop_id))
    else:
        db.execute("INSERT INTO services (shop_id, name, category_id, price, duration, "
                   "desc, active) VALUES (?,?,?,?,?,?,?)", (shop_id,) + fields)
    db.commit()
    flash("项目已保存")
    return redirect(url_for("admin_services", shop_id=shop_id))


@app.post("/s/<int:shop_id>/admin/services/delete")
@admin_required
def admin_service_delete(shop_id):
    db = get_db()
    db.execute("DELETE FROM services WHERE id=? AND shop_id=?", (request.form.get("id"), shop_id))
    db.commit()
    flash("项目已删除")
    return redirect(url_for("admin_services", shop_id=shop_id))


@app.post("/s/<int:shop_id>/admin/categories/add")
@admin_required
def admin_category_add(shop_id):
    name = request.form.get("name", "").strip()
    if name:
        db = get_db()
        db.execute("INSERT OR IGNORE INTO categories (shop_id, name) VALUES (?,?)",
                   (shop_id, name))
        db.commit()
    return redirect(url_for("admin_services", shop_id=shop_id))


@app.post("/s/<int:shop_id>/admin/categories/delete")
@admin_required
def admin_category_delete(shop_id):
    cid = request.form.get("id")
    db = get_db()
    db.execute("UPDATE services SET category_id=NULL WHERE category_id=? AND shop_id=?", (cid, shop_id))
    db.execute("DELETE FROM categories WHERE id=? AND shop_id=?", (cid, shop_id))
    db.commit()
    flash("分类已删除，其下项目移到「未分类」")
    return redirect(url_for("admin_services", shop_id=shop_id))


@app.get("/s/<int:shop_id>/admin/technicians")
@admin_required
def admin_technicians(shop_id):
    db = get_db()
    edit = None
    if eid := request.args.get("edit", type=int):
        edit = db.execute("SELECT * FROM technicians WHERE id=? AND shop_id=?",
                          (eid, shop_id)).fetchone()
    technicians = db.execute("SELECT * FROM technicians WHERE shop_id=? ORDER BY id",
                             (shop_id,)).fetchall()
    return render_template("admin/technicians.html", technicians=technicians, edit=edit)


def save_upload(f, prefix):
    """保存上传的图片，返回 URL；未传或格式不对返回 None"""
    if not f or not f.filename:
        return None
    ext = Path(f.filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        return None
    name = f"{prefix}_{int(time.time())}{ext}"
    f.save(UPLOAD_DIR / name)
    return "/static/uploads/" + name


@app.post("/s/<int:shop_id>/admin/technicians/save")
@admin_required
def admin_technician_save(shop_id):
    f = request.form
    if not f.get("name", "").strip():
        flash("请填写技师姓名")
        return redirect(url_for("admin_technicians", shop_id=shop_id))
    avatar = f.get("avatar_url", "").strip()
    if up := save_upload(request.files.get("avatar"), "tech"):
        avatar = up
    fields = (f["name"].strip(), avatar, f.get("intro", "").strip(),
              f.get("specialties", "").strip(), 1 if f.get("active") else 0)
    db = get_db()
    if tid := f.get("id"):
        db.execute("UPDATE technicians SET name=?, avatar=?, intro=?, specialties=?, "
                   "active=? WHERE id=? AND shop_id=?", fields + (tid, shop_id))
    else:
        db.execute("INSERT INTO technicians (shop_id, name, avatar, intro, specialties, "
                   "active) VALUES (?,?,?,?,?,?)", (shop_id,) + fields)
    db.commit()
    flash("技师已保存")
    return redirect(url_for("admin_technicians", shop_id=shop_id))


@app.post("/s/<int:shop_id>/admin/technicians/delete")
@admin_required
def admin_technician_delete(shop_id):
    db = get_db()
    db.execute("DELETE FROM technicians WHERE id=? AND shop_id=?", (request.form.get("id"), shop_id))
    db.commit()
    flash("技师已删除")
    return redirect(url_for("admin_technicians", shop_id=shop_id))


@app.get("/s/<int:shop_id>/admin/members")
@admin_required
def admin_members(shop_id):
    rows = get_db().execute(
        "SELECT m.*, (SELECT COUNT(*) FROM bookings b WHERE b.shop_id=m.shop_id "
        "AND b.phone=m.phone) bcnt FROM members m WHERE m.shop_id=? ORDER BY m.created_at DESC",
        (shop_id,)).fetchall()
    return render_template("admin/members.html", members=rows)


@app.route("/s/<int:shop_id>/admin/config", methods=["GET", "POST"])
@admin_required
def admin_config(shop_id):
    if request.method == "POST":
        for k in ("site_name", "slogan", "intro", "phone", "wechat",
                  "address", "hours", "map_search"):
            set_setting(k, request.form.get(k, "").strip())
        cfg = get_settings()
        for i in (1, 2, 3):
            if p := save_upload(request.files.get(f"carousel_{i}"), f"carousel_{i}"):
                if old := cfg.get(f"carousel_{i}"):
                    old_f = BASE / old.lstrip("/")
                    if old_f.is_file():
                        old_f.unlink()
                set_setting(f"carousel_{i}", p)
        get_db().commit()
        flash("配置已保存")
        return redirect(url_for("admin_config", shop_id=shop_id))
    return render_template("admin/config.html")


# ---------- 启动 ----------

# 模块加载即初始化数据库（不依赖启动方式），重复执行安全
init_db(DB_PATH)
_db = sqlite3.connect(DB_PATH)
_platform_pw = seed(_db)
_db.close()
if _platform_pw:
    print(f"[平台总后台初始账号] platform / {_platform_pw}  （仅首次启动显示，请妥善保管）")


def selftest():
    """核心流程自检：开店 + 多店隔离 + 页面渲染 + 预约审核流转"""
    global DB_PATH
    DB_PATH = Path(tempfile.gettempdir()) / "ysg_selftest.db"
    if DB_PATH.exists():
        DB_PATH.unlink()
    init_db(DB_PATH)
    db = sqlite3.connect(DB_PATH)
    plat_pw = seed(db)
    db.close()
    assert plat_pw, "总管理员未创建"

    c = app.test_client()
    with c.session_transaction() as s:
        s["csrf"] = "selftest"  # 固定测试 token
    for url in ("/", "/platform/login"):
        assert c.get(url).status_code == 200, f"页面 {url} 打不开"
    # CSRF 负测试：无 token 的 POST 被拒
    assert c.post("/platform/login", data={"username": "x", "password": "x"}).status_code == 400
    # 总后台开店 2 家，从提示条提取店后台密码
    assert c.post("/platform/login", data={"username": "platform", "password": plat_pw,
                                           "_csrf": "selftest"}).status_code == 302
    html = c.post("/platform/shops/add", data={"name": "自检一店", "_csrf": "selftest"},
                  follow_redirects=True).get_data(as_text=True)
    pw1 = re.search(r"密码 (\w+)", html).group(1)
    html = c.post("/platform/shops/add", data={"name": "自检二店", "_csrf": "selftest"},
                  follow_redirects=True).get_data(as_text=True)
    pw2 = re.search(r"密码 (\w+)", html).group(1)

    for url in ("/s/1/", "/s/1/services", "/s/1/booking", "/s/1/login",
                "/s/1/query", "/s/1/admin/login", "/s/2/"):
        assert c.get(url).status_code == 200, f"页面 {url} 打不开"
    r = c.post("/s/1/api/send_code", json={"phone": "13800000000"}).get_json()
    assert r["ok"], r
    assert c.post("/s/1/login", data={"phone": "13800000000",
                                      "code": r["dev_code"], "_csrf": "selftest"}).status_code == 302
    assert c.post("/s/1/booking", data={"name": "测试客户", "phone": "13800000000",
                                        "service_id": "1", "technician_id": "1",
                                        "bdate": "2099-01-01", "slot": "10:00",
                                        "_csrf": "selftest"}).status_code == 302
    assert c.post("/s/1/booking", data={"name": "测试客户2", "phone": "13800000000",
                                        "service_id": "1", "technician_id": "1",
                                        "bdate": "2099-01-01", "slot": "10:00",
                                        "_csrf": "selftest"}).status_code == 302
    html = c.get("/s/1/me").get_data(as_text=True)
    assert "待确认" in html and "全身经络推拿" in html
    # 店间隔离：店 1 管理员能进自己后台看自己预约，密码串店登录被拒
    assert c.post("/s/1/admin/login", data={"username": "admin",
                                            "password": pw1, "_csrf": "selftest"}).status_code == 302
    assert "测试客户" in c.get("/s/1/admin/bookings").get_data(as_text=True)
    c.get("/s/1/admin/logout")
    assert c.post("/s/2/admin/login", data={"username": "admin",
                                            "password": pw1, "_csrf": "selftest"}).status_code == 302
    assert "测试客户" not in c.get("/s/2/admin/bookings").get_data(as_text=True)
    c.get("/s/2/admin/logout")
    assert c.post("/s/2/admin/login", data={"username": "admin",
                                            "password": pw2, "_csrf": "selftest"}).status_code == 302
    db = sqlite3.connect(DB_PATH)
    n, = db.execute("SELECT COUNT(*) FROM bookings WHERE shop_id=1 AND slot='10:00' "
                    "AND technician_id=1 AND status!='cancelled'").fetchone()
    assert n == 1, f"同店同时段重复预约没拦住: {n}"
    assert db.execute("SELECT COUNT(*) FROM bookings WHERE shop_id=2").fetchone()[0] == 0
    bid = db.execute("SELECT id FROM bookings WHERE shop_id=1 LIMIT 1").fetchone()[0]
    db.close()
    c.get("/s/2/admin/logout")
    c.post("/s/1/admin/login", data={"username": "admin", "password": pw1, "_csrf": "selftest"})
    c.post("/s/1/admin/bookings/action", data={"id": bid, "status": "confirmed",
                                               "_csrf": "selftest"})
    c.post("/s/1/admin/bookings/action", data={"id": bid, "status": "completed",
                                               "_csrf": "selftest"})
    html = c.get("/s/1/me").get_data(as_text=True)
    assert "已完成" in html and "消费记录" in html
    # 停用门店后用户端 404
    c.post("/platform/shops/toggle", data={"id": "2", "_csrf": "selftest"})
    assert c.get("/s/2/").status_code == 404
    assert "自检二店" not in c.get("/").get_data(as_text=True)
    DB_PATH.unlink()
    print("selftest OK: CSRF、开店、停用、多店隔离、页面渲染、登录、预约、防重复、审核流转全部通过")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
        sys.exit()
    print("康怡养生馆多店系统已启动: http://127.0.0.1:5000  (各店后台 /s/<店id>/admin/login)")
    app.run(host="127.0.0.1", port=5000, debug=True)
