"""养生馆网页系统 — Flask + SQLite 单文件后端

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

SLOTS = [f"{h:02d}:00" for h in range(10, 21)]  # 可约时段 10:00-20:00
STATUS_CN = {"pending": "待确认", "confirmed": "已确认",
             "completed": "已完成", "cancelled": "已取消"}
PHONE_RE = re.compile(r"^1\d{10}$")
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, sort INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS services (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, category_id INTEGER,
    price REAL, duration INTEGER, desc TEXT DEFAULT '', active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS technicians (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, avatar TEXT DEFAULT '',
    intro TEXT DEFAULT '', specialties TEXT DEFAULT '', active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS members (
    id INTEGER PRIMARY KEY AUTOINCREMENT, phone TEXT UNIQUE, name TEXT DEFAULT '',
    gender TEXT DEFAULT '', birthday TEXT DEFAULT '', created_at TEXT);
CREATE TABLE IF NOT EXISTS sms_codes (
    phone TEXT, code TEXT, expires_at REAL);
CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, phone TEXT,
    service_id INTEGER, technician_id INTEGER, bdate TEXT, slot TEXT,
    status TEXT DEFAULT 'pending', remark TEXT DEFAULT '', created_at TEXT);
CREATE TABLE IF NOT EXISTS admins (
    id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE, pass TEXT);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY, value TEXT);
"""


# ---------- 数据库 ----------

def init_db(path=DB_PATH):
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.close()


def seed(db):
    """首次运行时写入示例数据（已存在则跳过）"""
    if db.execute("SELECT COUNT(*) FROM admins").fetchone()[0]:
        return
    cats = [("推拿按摩", 1), ("艾灸养生", 2), ("足浴足疗", 3), ("刮痧拔罐", 4)]
    db.executemany("INSERT INTO categories (name, sort) VALUES (?, ?)", cats)
    db.executemany("INSERT INTO services (name, category_id, price, duration, desc) VALUES (?,?,?,?,?)", [
        ("全身经络推拿", 1, 128, 60, "十二经络整体疏通，缓解疲劳，促进气血运行"),
        ("肩颈舒缓按摩", 1, 88, 45, "针对久坐办公人群，松解肩颈僵硬酸痛"),
        ("背部精油开背", 1, 158, 60, "植物精油配合专业手法，深层放松背部肌肉"),
        ("艾灸温阳理疗", 2, 98, 50, "古法艾灸温补阳气，改善手脚冰凉、体寒"),
        ("督脉铺灸", 2, 168, 70, "督脉长蛇灸，温通全身，调理亚健康"),
        ("中药足浴", 3, 68, 40, "二十余味中药熬制泡足，驱寒解乏助睡眠"),
        ("足底反射按摩", 3, 98, 60, "足底反射区精准刺激，调理脏腑机能"),
        ("刮痧排毒", 4, 58, 30, "传统刮痧疏通经络，祛除体内湿毒"),
        ("拔罐祛湿", 4, 68, 30, "火罐走罐，祛风散寒除湿"),
    ])
    db.executemany("INSERT INTO technicians (name, specialties, intro) VALUES (?,?,?)", [
        ("王师傅", "肩颈调理、腰椎推拿", "从业 15 年，擅长颈肩腰腿痛调理，手法沉稳有力"),
        ("李师傅", "艾灸、督脉灸", "中医灸疗世家传人，擅长体质调理与亚健康改善"),
        ("陈师傅", "足疗、足底反射", "10 年足疗经验，手法精准，广受好评"),
        ("周师傅", "经络推拿、拔罐", "科班出身，结合解剖学与中医经络理论"),
    ])
    admin_pw = "".join(random.choices("abcdefghjkmnpqrstuvwxyz23456789", k=10))
    salt = os.urandom(16).hex()
    db.execute("INSERT INTO admins (username, pass) VALUES (?, ?)",
               ("admin", salt + "$" + hashlib.sha256((salt + admin_pw).encode()).hexdigest()))
    print(f"[初始管理员] 账号 admin  密码 {admin_pw}  （仅首次启动显示，请立即登录后台并妥善保管）")
    db.executemany("INSERT INTO settings (key, value) VALUES (?, ?)", [
        ("site_name", "康怡养生馆"),
        ("slogan", "舒缓身心 · 颐养之道"),
        ("intro", "专注中医养生调理十余年，提供推拿、艾灸、足浴、刮痧等传统养生服务"),
        ("phone", "138-0000-0000"),
        ("wechat", "kangyi888"),
        ("address", "示例市幸福路 88 号"),
        ("hours", "每日 10:00 - 22:00"),
        ("map_search", "康怡养生馆"),
    ])
    db.commit()
    return admin_pw


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


def get_settings():
    """门店配置字典，模板里通过 cfg.site_name 访问"""
    if "cfg" not in g:
        g.cfg = dict(get_db().execute("SELECT key, value FROM settings"))
    return g.cfg


def set_setting(key, value):
    get_db().execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def hash_pass(pw):
    salt = os.urandom(16).hex()
    return salt + "$" + hashlib.sha256((salt + pw).encode()).hexdigest()


def check_pass(stored, pw):
    salt, h = stored.split("$", 1)
    return hashlib.sha256((salt + pw).encode()).hexdigest() == h


@app.context_processor
def inject_cfg():
    return {"cfg": get_settings(), "now": datetime.now()}


app.jinja_env.filters["yuan"] = lambda v: f"¥{v:g}"


# ---------- 短信 ----------

def send_sms(phone, code):
    """发送验证码。开发模式打印到控制台；上线时接入阿里云短信替换此函数即可。
    ponytail: 真实短信待接服务商（登录/查询两处验证码都走这里）"""
    print(f"[短信] 发给 {phone}: 验证码 {code}")


@app.post("/api/send_code")
def send_code():
    phone = (request.get_json() or {}).get("phone", "").strip()
    if not PHONE_RE.match(phone):
        return jsonify(ok=False, msg="手机号格式不正确")
    db = get_db()
    row = db.execute("SELECT code, expires_at FROM sms_codes WHERE phone=? "
                     "ORDER BY expires_at DESC LIMIT 1", (phone,)).fetchone()
    if row and row["expires_at"] - time.time() > 240:  # 60 秒内不重发
        code = row["code"]
    else:
        code = f"{random.randint(0, 999999):06d}"
        db.execute("DELETE FROM sms_codes WHERE phone=?", (phone,))
        db.execute("INSERT INTO sms_codes (phone, code, expires_at) VALUES (?,?,?)",
                   (phone, code, time.time() + 300))
        db.commit()
        send_sms(phone, code)
    return jsonify(ok=True, dev_code=code)  # dev_code 仅开发模式返回，接入真短信后删掉


# ---------- 登录装饰器 ----------

def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if not session.get("mid"):
            return redirect(url_for("login"))
        return f(*a, **k)
    return w


def admin_required(f):
    @wraps(f)
    def w(*a, **k):
        if not session.get("aid"):
            return redirect(url_for("admin_login"))
        return f(*a, **k)
    return w


# ---------- 客户端页面 ----------

@app.get("/")
def index():
    db = get_db()
    services = db.execute(
        "SELECT s.*, c.name cname FROM services s LEFT JOIN categories c "
        "ON s.category_id=c.id WHERE s.active=1 ORDER BY c.sort, s.id").fetchall()
    technicians = db.execute("SELECT * FROM technicians WHERE active=1").fetchall()
    cfg = get_settings()
    carousel = [cfg.get(f"carousel_{i}") for i in (1, 2, 3)]
    carousel = [u for u in carousel if u]
    return render_template("index.html", services=services,
                           technicians=technicians, carousel=carousel)


@app.get("/services")
def services():
    db = get_db()
    cat = request.args.get("cat", type=int)
    rows = db.execute(
        "SELECT s.*, c.name cname FROM services s LEFT JOIN categories c "
        "ON s.category_id=c.id WHERE s.active=1 AND (? IS NULL OR s.category_id=?) "
        "ORDER BY c.sort, s.id", (cat, cat)).fetchall()
    categories = db.execute(
        "SELECT c.*, COUNT(s.id) cnt FROM categories c LEFT JOIN services s "
        "ON s.category_id=c.id AND s.active=1 GROUP BY c.id ORDER BY c.sort").fetchall()
    return render_template("services.html", services=rows, categories=categories, cat=cat)


@app.route("/booking", methods=["GET", "POST"])
def booking():
    db = get_db()
    if request.method == "POST":
        f = request.form
        err = None
        if not f.get("name", "").strip():
            err = "请填写姓名"
        elif not PHONE_RE.match(f.get("phone", "")):
            err = "手机号格式不正确"
        elif not db.execute("SELECT 1 FROM services WHERE id=? AND active=1",
                            (f.get("service_id", -1),)).fetchone():
            err = "请选择服务项目"
        elif f.get("technician_id") and not db.execute(
                "SELECT 1 FROM technicians WHERE id=?", (f["technician_id"],)).fetchone():
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
                "SELECT 1 FROM bookings WHERE technician_id=? AND bdate=? AND slot=? "
                "AND status!='cancelled'",
                (f.get("technician_id") or None, f["bdate"], f["slot"])).fetchone():
            err = "该技师该时段已被预约，请换时段或换技师"
        if err:
            flash(err)
            return redirect(url_for("booking"))
        db.execute("INSERT INTO bookings (name, phone, service_id, technician_id, "
                   "bdate, slot, remark, created_at) VALUES (?,?,?,?,?,?,?,?)",
                   (f["name"].strip(), f["phone"], f["service_id"],
                    f.get("technician_id") or None, f["bdate"], f["slot"],
                    f.get("remark", "").strip(),
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        db.commit()
        flash("预约提交成功，请等待门店确认")
        return redirect(url_for("me") if session.get("mid") else url_for("query"))

    cat_services = [
        (c["name"], db.execute("SELECT * FROM services WHERE category_id=? AND active=1",
                               (c["id"],)).fetchall())
        for c in db.execute("SELECT * FROM categories ORDER BY sort")]
    technicians = db.execute("SELECT * FROM technicians WHERE active=1").fetchall()
    member = None
    if session.get("mid"):
        member = db.execute("SELECT * FROM members WHERE id=?", (session["mid"],)).fetchone()
    return render_template("booking.html", cat_services=cat_services,
                           technicians=technicians, slots=SLOTS, member=member,
                           pre_service=request.args.get("service", type=int))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        phone, code = request.form.get("phone", "").strip(), request.form.get("code", "").strip()
        db = get_db()
        row = db.execute("SELECT code, expires_at FROM sms_codes WHERE phone=? "
                         "ORDER BY expires_at DESC LIMIT 1", (phone,)).fetchone()
        if not (PHONE_RE.match(phone) and row and row["code"] == code
                and row["expires_at"] > time.time()):
            flash("验证码错误或已过期")
            return redirect(url_for("login"))
        db.execute("DELETE FROM sms_codes WHERE phone=?", (phone,))
        if not db.execute("SELECT id FROM members WHERE phone=?", (phone,)).fetchone():
            db.execute("INSERT INTO members (phone, created_at) VALUES (?,?)",
                       (phone, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            db.commit()
        session["mid"] = db.execute("SELECT id FROM members WHERE phone=?",
                                    (phone,)).fetchone()["id"]
        db.commit()
        flash("登录成功" + ("，已为您自动注册会员" if not row else ""))
        return redirect(url_for("me"))
    return render_template("login.html")


@app.get("/logout")
def logout():
    session.pop("mid", None)
    return redirect("/")


@app.route("/query", methods=["GET", "POST"])
def query():
    bookings = None
    phone = ""
    if request.method == "POST":
        phone = request.form.get("phone", "").strip()
        code = request.form.get("code", "").strip()
        db = get_db()
        row = db.execute("SELECT code, expires_at FROM sms_codes WHERE phone=? "
                         "ORDER BY expires_at DESC LIMIT 1", (phone,)).fetchone()
        if not (row and row["code"] == code and row["expires_at"] > time.time()):
            flash("验证码错误或已过期")
        else:
            bookings = db.execute(
                "SELECT b.*, s.name sname, s.price, s.duration, t.name tname "
                "FROM bookings b LEFT JOIN services s ON b.service_id=s.id "
                "LEFT JOIN technicians t ON b.technician_id=t.id "
                "WHERE b.phone=? ORDER BY b.bdate DESC, b.slot DESC", (phone,)).fetchall()
    return render_template("query.html", bookings=bookings, phone=phone)


@app.route("/me", methods=["GET", "POST"])
@login_required
def me():
    db = get_db()
    m = db.execute("SELECT * FROM members WHERE id=?", (session["mid"],)).fetchone()
    if request.method == "POST":
        db.execute("UPDATE members SET name=?, gender=?, birthday=? WHERE id=?",
                   (request.form.get("name", "").strip(),
                    request.form.get("gender", ""),
                    request.form.get("birthday", ""), session["mid"]))
        db.commit()
        flash("个人信息已保存")
        return redirect(url_for("me"))
    rows = db.execute(
        "SELECT b.*, s.name sname, s.price, s.duration, t.name tname "
        "FROM bookings b LEFT JOIN services s ON b.service_id=s.id "
        "LEFT JOIN technicians t ON b.technician_id=t.id "
        "WHERE b.phone=? ORDER BY b.bdate DESC, b.slot DESC", (m["phone"],)).fetchall()
    bookings = [r for r in rows if r["status"] != "completed"]
    consumptions = [r for r in rows if r["status"] == "completed"]
    return render_template("me.html", m=m, bookings=bookings,
                           consumptions=consumptions)


# ---------- 管理后台 ----------

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        u = request.form.get("username", "")
        pw = request.form.get("password", "")
        row = get_db().execute("SELECT * FROM admins WHERE username=?", (u,)).fetchone()
        if row and check_pass(row["pass"], pw):
            session["aid"] = row["id"]
            return redirect("/admin/")
        flash("账号或密码错误")
    return render_template("admin/login.html")


@app.get("/admin/logout")
def admin_logout():
    session.pop("aid", None)
    return redirect("/admin/login")


@app.get("/admin/")
@admin_required
def admin_index():
    db = get_db()
    today = datetime.now().strftime("%Y-%m-%d")
    stats = {
        "今日预约": db.execute("SELECT COUNT(*) c FROM bookings WHERE bdate=? AND status!='cancelled'",
                            (today,)).fetchone()["c"],
        "待确认": db.execute("SELECT COUNT(*) c FROM bookings WHERE status='pending'").fetchone()["c"],
        "会员数": db.execute("SELECT COUNT(*) c FROM members").fetchone()["c"],
        "在售项目": db.execute("SELECT COUNT(*) c FROM services WHERE active=1").fetchone()["c"],
    }
    recent = db.execute(
        "SELECT b.*, s.name sname, t.name tname FROM bookings b "
        "LEFT JOIN services s ON b.service_id=s.id LEFT JOIN technicians t ON b.technician_id=t.id "
        "ORDER BY b.created_at DESC LIMIT 8").fetchall()
    return render_template("admin/index.html", stats=stats, recent=recent)


@app.get("/admin/bookings")
@admin_required
def admin_bookings():
    st = request.args.get("status", "all")
    db = get_db()
    rows = db.execute(
        "SELECT b.*, s.name sname, s.price, t.name tname FROM bookings b "
        "LEFT JOIN services s ON b.service_id=s.id LEFT JOIN technicians t ON b.technician_id=t.id "
        "WHERE ?='all' OR b.status=? ORDER BY b.bdate DESC, b.slot DESC",
        (st, st)).fetchall()
    return render_template("admin/bookings.html", bookings=rows,
                           status_cn=STATUS_CN, st=st)


@app.post("/admin/bookings/action")
@admin_required
def admin_booking_action():
    bid = request.form.get("id")
    status = request.form.get("status")
    if status in STATUS_CN:
        db = get_db()
        db.execute("UPDATE bookings SET status=? WHERE id=?", (status, bid))
        db.commit()
        flash(f"预约已改为「{STATUS_CN[status]}」")
    return redirect(url_for("admin_bookings", status=request.args.get("status", "all")))


@app.get("/admin/services")
@admin_required
def admin_services():
    db = get_db()
    edit = None
    if eid := request.args.get("edit", type=int):
        edit = db.execute("SELECT * FROM services WHERE id=?", (eid,)).fetchone()
    services = db.execute(
        "SELECT s.*, c.name cname FROM services s LEFT JOIN categories c "
        "ON s.category_id=c.id ORDER BY c.sort, s.id").fetchall()
    categories = db.execute(
        "SELECT c.*, COUNT(s.id) cnt FROM categories c LEFT JOIN services s "
        "ON s.category_id=c.id GROUP BY c.id ORDER BY c.sort").fetchall()
    return render_template("admin/services.html", services=services,
                           categories=categories, edit=edit)


@app.post("/admin/services/save")
@admin_required
def admin_service_save():
    f = request.form
    if not (f.get("name", "").strip() and f.get("price") and f.get("duration")):
        flash("请填写完整的项目信息")
        return redirect(url_for("admin_services"))
    fields = (f["name"].strip(), f.get("category_id", type=int), float(f["price"]),
              int(f["duration"]), f.get("desc", "").strip(), 1 if f.get("active") else 0)
    db = get_db()
    if sid := f.get("id"):
        db.execute("UPDATE services SET name=?, category_id=?, price=?, duration=?, "
                   "desc=?, active=? WHERE id=?", fields + (sid,))
    else:
        db.execute("INSERT INTO services (name, category_id, price, duration, desc, active) "
                   "VALUES (?,?,?,?,?,?)", fields)
    db.commit()
    flash("项目已保存")
    return redirect(url_for("admin_services"))


@app.post("/admin/services/delete")
@admin_required
def admin_service_delete():
    get_db().execute("DELETE FROM services WHERE id=?", (request.form.get("id"),))
    get_db().commit()
    flash("项目已删除")
    return redirect(url_for("admin_services"))


@app.post("/admin/categories/add")
@admin_required
def admin_category_add():
    name = request.form.get("name", "").strip()
    if name:
        get_db().execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (name,))
        get_db().commit()
    return redirect(url_for("admin_services"))


@app.post("/admin/categories/delete")
@admin_required
def admin_category_delete():
    cid = request.form.get("id")
    db = get_db()
    db.execute("UPDATE services SET category_id=NULL WHERE category_id=?", (cid,))
    db.execute("DELETE FROM categories WHERE id=?", (cid,))
    db.commit()
    flash("分类已删除，其下项目移到「未分类」")
    return redirect(url_for("admin_services"))


@app.get("/admin/technicians")
@admin_required
def admin_technicians():
    db = get_db()
    edit = None
    if eid := request.args.get("edit", type=int):
        edit = db.execute("SELECT * FROM technicians WHERE id=?", (eid,)).fetchone()
    technicians = db.execute("SELECT * FROM technicians ORDER BY id").fetchall()
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


@app.post("/admin/technicians/save")
@admin_required
def admin_technician_save():
    f = request.form
    if not f.get("name", "").strip():
        flash("请填写技师姓名")
        return redirect(url_for("admin_technicians"))
    avatar = f.get("avatar_url", "").strip()
    if up := save_upload(request.files.get("avatar"), "tech"):
        avatar = up
    fields = (f["name"].strip(), avatar, f.get("intro", "").strip(),
              f.get("specialties", "").strip(), 1 if f.get("active") else 0)
    db = get_db()
    if tid := f.get("id"):
        db.execute("UPDATE technicians SET name=?, avatar=?, intro=?, specialties=?, "
                   "active=? WHERE id=?", fields + (tid,))
    else:
        db.execute("INSERT INTO technicians (name, avatar, intro, specialties, active) "
                   "VALUES (?,?,?,?,?)", fields)
    db.commit()
    flash("技师已保存")
    return redirect(url_for("admin_technicians"))


@app.post("/admin/technicians/delete")
@admin_required
def admin_technician_delete():
    get_db().execute("DELETE FROM technicians WHERE id=?", (request.form.get("id"),))
    get_db().commit()
    flash("技师已删除")
    return redirect(url_for("admin_technicians"))


@app.get("/admin/members")
@admin_required
def admin_members():
    rows = get_db().execute(
        "SELECT m.*, (SELECT COUNT(*) FROM bookings b WHERE b.phone=m.phone) bcnt "
        "FROM members m ORDER BY m.created_at DESC").fetchall()
    return render_template("admin/members.html", members=rows)


@app.route("/admin/config", methods=["GET", "POST"])
@admin_required
def admin_config():
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
        return redirect(url_for("admin_config"))
    return render_template("admin/config.html")


# ---------- 启动 ----------

# 模块加载即初始化数据库（不依赖启动方式），重复执行安全
init_db(DB_PATH)
_db = sqlite3.connect(DB_PATH)
seed(_db)
_db.close()


def selftest():
    """核心流程自检：页面渲染 + 预约 + 审核 + 登录"""
    global DB_PATH
    DB_PATH = Path(tempfile.gettempdir()) / "ysg_selftest.db"
    if DB_PATH.exists():
        DB_PATH.unlink()
    init_db(DB_PATH)
    db = sqlite3.connect(DB_PATH)
    admin_pw = seed(db)
    db.close()

    c = app.test_client()
    for url in ("/", "/services", "/booking", "/login", "/query", "/admin/login"):
        assert c.get(url).status_code == 200, f"页面 {url} 打不开"
    r = c.post("/api/send_code", json={"phone": "13800000000"}).get_json()
    assert r["ok"], r
    assert c.post("/login", data={"phone": "13800000000",
                                  "code": r["dev_code"]}).status_code == 302
    assert c.post("/booking", data={"name": "测试客户", "phone": "13800000000",
                                    "service_id": "1", "technician_id": "1",
                                    "bdate": "2099-01-01", "slot": "10:00"}).status_code == 302
    assert c.post("/booking", data={"name": "测试客户2", "phone": "13800000000",
                                    "service_id": "1", "technician_id": "1",
                                    "bdate": "2099-01-01", "slot": "10:00"}).status_code == 302
    html = c.get("/me").get_data(as_text=True)
    assert "待确认" in html and "全身经络推拿" in html
    assert c.post("/admin/login", data={"username": "admin",
                                        "password": admin_pw}).status_code == 302
    html = c.get("/admin/bookings").get_data(as_text=True)
    assert "测试客户" in html
    db = sqlite3.connect(DB_PATH)
    n, = db.execute("SELECT COUNT(*) FROM bookings WHERE slot='10:00' "
                    "AND technician_id=1 AND status!='cancelled'").fetchone()
    assert n == 1, f"同时段重复预约没拦住: {n}"
    bid = db.execute("SELECT id FROM bookings LIMIT 1").fetchone()[0]
    db.close()
    c.post("/admin/bookings/action", data={"id": bid, "status": "confirmed"})
    c.post("/admin/bookings/action", data={"id": bid, "status": "completed"})
    html = c.get("/me").get_data(as_text=True)
    assert "已完成" in html and "消费记录" in html
    DB_PATH.unlink()
    print("selftest OK: 页面渲染、登录、预约、防重复、审核流转全部通过")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
        sys.exit()
    print("康怡养生馆已启动: http://127.0.0.1:5000  (后台 http://127.0.0.1:5000/admin/login, admin/admin123)")
    app.run(host="127.0.0.1", port=5000, debug=True)
