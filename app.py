"""养生馆网页系统 — Flask + SQLite 多店版（SaaS 雏形）

每家店独立网址: /s/<店id>/       后台: /s/<店id>/admin/login
运行: python app.py          (浏览器打开 http://127.0.0.1:5000)
自检: python app.py --selftest
"""
import hashlib
import json
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


PHONE_RE = re.compile(r"^1\d{10}$")
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS shops (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT, shop_id INTEGER, name TEXT, sort INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS services (
    id INTEGER PRIMARY KEY AUTOINCREMENT, shop_id INTEGER, name TEXT, category_id INTEGER,
    price REAL, duration INTEGER, desc TEXT DEFAULT '', active INTEGER DEFAULT 1,
    deduct_times INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS technicians (
    id INTEGER PRIMARY KEY AUTOINCREMENT, shop_id INTEGER, name TEXT, avatar TEXT DEFAULT '',
    intro TEXT DEFAULT '', specialties TEXT DEFAULT '', active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS members (
    id INTEGER PRIMARY KEY AUTOINCREMENT, shop_id INTEGER, phone TEXT, name TEXT DEFAULT '',
    gender TEXT DEFAULT '', birthday TEXT DEFAULT '', created_at TEXT);
CREATE TABLE IF NOT EXISTS sms_codes (
    shop_id INTEGER, phone TEXT, code TEXT, expires_at REAL);
CREATE TABLE IF NOT EXISTS admins (
    id INTEGER PRIMARY KEY AUTOINCREMENT, shop_id INTEGER, username TEXT, pass TEXT,
    role TEXT DEFAULT 'super', phone TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT, shop_id INTEGER, order_no TEXT, member_id INTEGER,
    member_card_id INTEGER, items_json TEXT, total REAL, discount REAL DEFAULT 0,
    pay_method TEXT, status TEXT DEFAULT 'paid', operator_id INTEGER, created_at TEXT);
CREATE TABLE IF NOT EXISTS op_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, shop_id INTEGER, admin_id INTEGER,
    action TEXT, detail TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS settings (
    shop_id INTEGER, key TEXT, value TEXT, PRIMARY KEY (shop_id, key));
CREATE TABLE IF NOT EXISTS cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT, shop_id INTEGER, name TEXT,
    price REAL, times INTEGER, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS member_cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT, shop_id INTEGER, member_id INTEGER,
    card_id INTEGER, times_left INTEGER, status TEXT DEFAULT 'pending',
    created_at TEXT);
CREATE TABLE IF NOT EXISTS card_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, shop_id INTEGER, member_card_id INTEGER,
    service_id INTEGER, technician_id INTEGER, created_at TEXT);
"""

# 新店预置项目（名称, 核销扣次）；价格/时长/分类由店长开店后自行设置
SEED_SERVICES = [
    ("婚前护理", 1), ("产后恢复", 1), ("儿童游泳", 1), ("普拉提瑜伽", 1), ("全身精致减肥", 1),
    ("皮肤管理", 1), ("细胞乳清器保养子宫", 1), ("盐房", 1), ("韩式汗蒸房", 1), ("全身脱毛", 15),
    ("私密护理", 5), ("全身按摩", 2), ("半身按摩", 1), ("全身排毒", 2), ("泡脚按摩", 1),
    ("乳保护理", 1), ("暖宫护理", 1), ("肾护理", 1), ("提臀", 1), ("护肩", 2),
    ("泡浴", 2), ("美人鱼汗蒸", 2), ("药液洗头", 2), ("头部蛋白护理", 1), ("脸部排毒护理", 2),
    ("颈部护理抗衰", 2), ("手部美白补水护理", 1), ("能量姜子提", 1), ("紫曼提", 5), ("艾草除湿", 1),
    ("采耳护理", 1), ("全身美白护理", 5), ("催乳", 5), ("回奶", 5), ("全身调理", 2),
    ("通背", 2), ("腿部按摩机", 3),
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
    cols = [r[1] for r in db.execute("PRAGMA table_info(shops)")]
    if "active" not in cols:
        db.execute("ALTER TABLE shops ADD COLUMN active INTEGER DEFAULT 1")
    if "technician_id" not in [r[1] for r in db.execute("PRAGMA table_info(card_logs)")]:
        db.execute("ALTER TABLE card_logs ADD COLUMN technician_id INTEGER")
    if "deduct_times" not in [r[1] for r in db.execute("PRAGMA table_info(services)")]:
        db.execute("ALTER TABLE services ADD COLUMN deduct_times INTEGER DEFAULT 1")
    admin_cols = [r[1] for r in db.execute("PRAGMA table_info(admins)")]
    if "role" not in admin_cols:
        db.execute("ALTER TABLE admins ADD COLUMN role TEXT DEFAULT 'super'")
    if "phone" not in admin_cols:
        db.execute("ALTER TABLE admins ADD COLUMN phone TEXT DEFAULT ''")
    if "active" not in admin_cols:
        db.execute("ALTER TABLE admins ADD COLUMN active INTEGER DEFAULT 1")
    order_cols = [r[1] for r in db.execute("PRAGMA table_info(orders)")]
    if "member_card_id" not in order_cols:
        db.execute("ALTER TABLE orders ADD COLUMN member_card_id INTEGER")
    db.commit()
    db.close()


def seed_shop(db, name):
    """开一家新店：初始化分类/项目/技师/配置/店管理员，返回店 id 和管理员密码"""
    sid = db.execute("INSERT INTO shops (name) VALUES (?)", (name,)).lastrowid
    db.executemany(
        "INSERT INTO services (shop_id, name, category_id, price, duration, desc, active, deduct_times) "
        "VALUES (?,?,NULL,0,60,'',1,?)",
        [(sid, n, t) for n, t in SEED_SERVICES])
    db.executemany("INSERT INTO technicians (shop_id, name, specialties, intro) VALUES (?,?,?,?)",
                   [(sid, n, sp, it) for n, sp, it in SEED_TECHS])
    pw = "".join(random.choices("abcdefghjkmnpqrstuvwxyz23456789", k=10))
    salt = os.urandom(16).hex()
    db.execute("INSERT INTO admins (shop_id, username, pass) VALUES (?,?,?)",
               (sid, "admin", salt + "$" + hashlib.sha256((salt + pw).encode()).hexdigest()))
    # 预置三档会员次卡示例（店长可改价格/次数；所有卡均可用店内全部项目）
    db.executemany("INSERT INTO cards (shop_id, name, price, times) VALUES (?,?,?,?)",
                   [(sid, "基础卡", 980, 10), (sid, "进阶卡", 1980, 20),
                    (sid, "尊享卡", 2880, 30)])
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


def get_admin():
    """当前登录的店管理员（模板菜单按角色渲染用）"""
    if "adm" not in g:
        g.adm = None
        if session.get("aid"):
            g.adm = get_db().execute("SELECT * FROM admins WHERE id=?",
                                     (session["aid"],)).fetchone()
    return g.adm


@app.context_processor
def inject_shop():
    shop = get_shop() if g.get("shop_id") else None
    return {"shop": shop, "cfg": get_settings() if shop else {},
            "now": datetime.now(), "csrf": _csrf_token(), "adm": get_admin()}


app.jinja_env.filters["yuan"] = lambda v: f"¥{v:g}"
app.jinja_env.filters["fromjson"] = json.loads


# ---------- 短信 ----------

def send_sms(phone, code):
    """发送验证码。开发模式打印到控制台；上线时接入阿里云短信替换此函数即可。
    ponytail: 真实短信待接服务商（登录/查询两处验证码都走这里）"""
    print(f"[短信] 发给 {phone}: 验证码 {code}")


# ---------- 登录装饰器 ----------

def admin_required(f):
    @wraps(f)
    def w(shop_id, *a, **k):
        if not session.get("aid") or session.get("ashop") != shop_id:
            return redirect(url_for("admin_login", shop_id=shop_id))
        return f(shop_id, *a, **k)
    return w



def super_required(f):
    """一级管理员专属功能"""
    @wraps(f)
    def w(shop_id, *a, **k):
        adm = get_admin()
        if not adm or adm["role"] != "super":
            flash("该操作仅限一级管理员")
            return redirect(url_for("admin_index", shop_id=shop_id))
        return f(shop_id, *a, **k)
    return w


def log_op(shop_id, action, detail=""):
    """操作留痕"""
    get_db().execute("INSERT INTO op_logs (shop_id, admin_id, action, detail, created_at) "
                     "VALUES (?,?,?,?,?)",
                     (shop_id, session.get("aid"), action, detail,
                      datetime.now().strftime("%Y-%m-%d %H:%M:%S")))


def check_vcode(shop_id, vcode):
    """二级账号敏感操作验证码校验（一级直接放行）"""
    adm = get_admin()
    if adm and adm["role"] == "super":
        return True
    row = get_db().execute(
        "SELECT code, expires_at FROM sms_codes WHERE shop_id=? AND phone="
        "(SELECT phone FROM admins WHERE shop_id=? AND role='super' AND phone!='' LIMIT 1) "
        "ORDER BY expires_at DESC LIMIT 1", (shop_id, shop_id)).fetchone()
    return bool(row and row["code"] == vcode and row["expires_at"] > time.time())


# ---------- 根路径 ----------

@app.get("/")
def root():
    """内部管理后台：无对外网页"""
    return redirect(url_for("platform_login"))


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
    if db.execute("SELECT COUNT(*) FROM shops").fetchone()[0] >= 5:
        flash("最多开 5 家店，已达上限")
        return redirect(url_for("platform_shops"))
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


@app.post("/platform/shops/delete")
@platform_required
def platform_shop_delete():
    """删除门店及全部数据，释放 5 家名额"""
    db = get_db()
    s = db.execute("SELECT * FROM shops WHERE id=?", (request.form.get("id"),)).fetchone()
    if s:
        for t in ("settings", "admins", "categories", "services", "technicians",
                  "members", "sms_codes", "orders", "member_cards", "card_logs",
                  "cards", "op_logs"):
            db.execute(f"DELETE FROM {t} WHERE shop_id=?", (s["id"],))
        db.execute("DELETE FROM shops WHERE id=?", (s["id"],))
        db.commit()
        flash(f"「{s['name']}」已删除（名额已释放，可再开新店）")
    return redirect(url_for("platform_shops"))


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
        row = get_db().execute("SELECT * FROM admins WHERE shop_id=? AND username=? AND active=1",
                               (shop_id, u)).fetchone()
        if row and check_pass(row["pass"], pw):
            session["aid"] = row["id"]
            session["ashop"] = shop_id
            log_op(shop_id, "登录", u)
            get_db().commit()
            return redirect(url_for("admin_index", shop_id=shop_id))
        flash("账号或密码错误")
        return redirect(url_for("admin_login", shop_id=shop_id))
    return render_template("admin/login.html")


@app.get("/s/<int:shop_id>/admin/logout")
def admin_logout(shop_id):
    session.pop("aid", None)
    session.pop("ashop", None)
    return redirect(url_for("admin_login", shop_id=shop_id))


@app.get("/s/<int:shop_id>/admin/api/member_search")
@admin_required
def admin_member_search(shop_id):
    """收银台会员搜索（手机号/姓名模糊）"""
    q = request.args.get("q", "").strip()
    rows = get_db().execute(
        "SELECT id, phone, name FROM members WHERE shop_id=? AND (phone LIKE ? OR name LIKE ?) "
        "LIMIT 5", (shop_id, f"%{q}%", f"%{q}%")).fetchall()
    return jsonify(members=[dict(r) for r in rows])


@app.get("/s/<int:shop_id>/admin/api/member_cards")
@admin_required
def admin_member_cards_api(shop_id):
    """收银台划卡：该会员生效的会员卡"""
    rows = get_db().execute(
        "SELECT mc.id, mc.times_left, c.name cname FROM member_cards mc "
        "JOIN cards c ON c.id=mc.card_id WHERE mc.shop_id=? AND mc.member_id=? "
        "AND mc.status='active' AND mc.times_left>0",
        (shop_id, request.args.get("member_id"))).fetchall()
    return jsonify(cards=[dict(r) for r in rows])


@app.get("/s/<int:shop_id>/admin/")
@admin_required
def admin_index(shop_id):
    db = get_db()
    today = datetime.now().strftime("%Y-%m-%d") + "%"
    stats = {
        "今日营收": db.execute("SELECT COALESCE(SUM(total-discount), 0) s FROM orders "
                            "WHERE shop_id=? AND status='paid' AND created_at LIKE ?",
                            (shop_id, today)).fetchone()["s"],
        "今日订单": db.execute("SELECT COUNT(*) c FROM orders WHERE shop_id=? AND created_at LIKE ?",
                            (shop_id, today)).fetchone()["c"],
        "会员数": db.execute("SELECT COUNT(*) c FROM members WHERE shop_id=?", (shop_id,)).fetchone()["c"],
        "在售项目": db.execute("SELECT COUNT(*) c FROM services WHERE shop_id=? AND active=1",
                            (shop_id,)).fetchone()["c"],
    }
    recent = db.execute(
        "SELECT o.*, a.username opname FROM orders o LEFT JOIN admins a ON a.id=o.operator_id "
        "WHERE o.shop_id=? AND o.created_at LIKE ? ORDER BY o.id DESC LIMIT 8",
        (shop_id, today)).fetchall()
    return render_template("admin/index.html", stats=stats, recent=recent)


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
@super_required
def admin_service_save(shop_id):
    f = request.form
    if not (f.get("name", "").strip() and f.get("price") and f.get("duration")):
        flash("请填写完整的项目信息")
        return redirect(url_for("admin_services", shop_id=shop_id))
    fields = (f["name"].strip(), f.get("category_id", type=int), float(f["price"]),
              int(f["duration"]), f.get("desc", "").strip(), 1 if f.get("active") else 0,
              max(1, f.get("deduct_times", type=int) or 1))
    db = get_db()
    if sid := f.get("id"):
        db.execute("UPDATE services SET name=?, category_id=?, price=?, duration=?, "
                   "desc=?, active=?, deduct_times=? WHERE id=? AND shop_id=?", fields + (sid, shop_id))
    else:
        db.execute("INSERT INTO services (shop_id, name, category_id, price, duration, "
                   "desc, active, deduct_times) VALUES (?,?,?,?,?,?,?,?)", (shop_id,) + fields)
    db.commit()
    flash("项目已保存")
    return redirect(url_for("admin_services", shop_id=shop_id))


@app.post("/s/<int:shop_id>/admin/services/delete")
@admin_required
@super_required
def admin_service_delete(shop_id):
    db = get_db()
    db.execute("DELETE FROM services WHERE id=? AND shop_id=?", (request.form.get("id"), shop_id))
    log_op(shop_id, "删除项目", request.form.get("id"))
    db.commit()
    flash("项目已删除")
    return redirect(url_for("admin_services", shop_id=shop_id))


@app.post("/s/<int:shop_id>/admin/categories/add")
@admin_required
@super_required
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
@super_required
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
@super_required
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
@super_required
def admin_technician_delete(shop_id):
    db = get_db()
    db.execute("DELETE FROM technicians WHERE id=? AND shop_id=?", (request.form.get("id"), shop_id))
    log_op(shop_id, "删除技师", request.form.get("id"))
    db.commit()
    flash("技师已删除")
    return redirect(url_for("admin_technicians", shop_id=shop_id))


# ---------- 店后台：会员卡 ----------

@app.route("/s/<int:shop_id>/admin/cards", methods=["GET", "POST"])
@admin_required
@super_required
def admin_cards(shop_id):
    """卡档管理：名称/价格/次数/可用项目/上架"""
    db = get_db()
    if request.method == "POST":
        f = request.form
        if not (f.get("name", "").strip() and f.get("price") and f.get("times")):
            flash("请填写完整的卡信息")
        else:
            fields = (f["name"].strip(), float(f["price"]), int(f["times"]),
                      1 if f.get("active") else 0)
            if cid := f.get("id"):
                db.execute("UPDATE cards SET name=?, price=?, times=?, active=? "
                           "WHERE id=? AND shop_id=?", fields + (cid, shop_id))
            else:
                db.execute("INSERT INTO cards (shop_id, name, price, times, active) "
                           "VALUES (?,?,?,?,?)", (shop_id,) + fields)
            db.commit()
            flash("卡已保存")
        return redirect(url_for("admin_cards", shop_id=shop_id))
    edit = None
    if eid := request.args.get("edit", type=int):
        edit = db.execute("SELECT * FROM cards WHERE id=? AND shop_id=?", (eid, shop_id)).fetchone()
    cards = db.execute(
        "SELECT c.*, (SELECT COUNT(*) FROM member_cards mc WHERE mc.card_id=c.id "
        "AND mc.status='active') sold, (SELECT COUNT(*) FROM member_cards mc "
        "WHERE mc.card_id=c.id AND mc.status='pending') pending "
        "FROM cards c WHERE c.shop_id=? ORDER BY c.id", (shop_id,)).fetchall()
    return render_template("admin/cards.html", cards=cards, edit=edit)


@app.get("/s/<int:shop_id>/admin/card_buyers")
@admin_required
def admin_card_buyers(shop_id):
    """点击卡名查看该卡全部购买人"""
    cid = request.args.get("card", type=int)
    db = get_db()
    card = db.execute("SELECT * FROM cards WHERE id=? AND shop_id=?", (cid, shop_id)).fetchone()
    if not card:
        return redirect(url_for("admin_cards", shop_id=shop_id))
    st = request.args.get("status", "all")
    q = request.args.get("q", "").strip()
    rows = db.execute(
        "SELECT mc.*, m.phone, m.name mname FROM member_cards mc "
        "LEFT JOIN members m ON m.id=mc.member_id "
        "WHERE mc.shop_id=? AND mc.card_id=? AND (?='all' OR mc.status=?) "
        "AND (?='' OR m.phone LIKE '%'||?||'%') "
        "ORDER BY mc.id DESC", (shop_id, cid, st, st, q, q)).fetchall()
    services = db.execute("SELECT * FROM services WHERE shop_id=? ORDER BY id",
                          (shop_id,)).fetchall()
    technicians = db.execute("SELECT * FROM technicians WHERE shop_id=? AND active=1 ORDER BY id",
                             (shop_id,)).fetchall()
    logs = {}
    if rows:
        q = ",".join("?" * len(rows))
        for r in db.execute(
                f"SELECT l.*, s.name sname, t.name tname FROM card_logs l "
                f"LEFT JOIN services s ON s.id=l.service_id "
                f"LEFT JOIN technicians t ON t.id=l.technician_id "
                f"WHERE l.member_card_id IN ({q}) ORDER BY l.id DESC",
                [r["id"] for r in rows]):
            logs.setdefault(r["member_card_id"], []).append(
                {"created_at": r["created_at"], "sname": r["sname"],
                 "tname": r["tname"]})
    return render_template("admin/card_buyers.html", card=card, buyers=rows,
                           services=services, technicians=technicians, logs=logs,
                           st=st, q=q)


@app.post("/s/<int:shop_id>/admin/card_buyers/delete")
@admin_required
def admin_card_buyer_delete(shop_id):
    """删除购买记录（二级需一级验证码）"""
    if get_admin()["role"] == "staff" and not check_vcode(shop_id, request.form.get("vcode")):
        flash("删除购卡记录需一级管理员验证码")
        return redirect(url_for("admin_card_buyers", shop_id=shop_id, card=request.form.get("card")))
    db = get_db()
    db.execute("DELETE FROM member_cards WHERE id=? AND shop_id=?",
               (request.form.get("id"), shop_id))
    log_op(shop_id, "删除购卡记录", request.form.get("id"))
    db.commit()
    flash("购买记录已删除")
    return redirect(url_for("admin_card_buyers", shop_id=shop_id,
                            card=request.form.get("card")))


@app.get("/s/<int:shop_id>/admin/card_deduct")
@admin_required
def admin_card_deduct(shop_id):
    """独立核销页：勾选项目 + 技师 + 确认"""
    db = get_db()
    mc = db.execute("SELECT mc.*, m.phone, m.name mname, c.name cname, c.times FROM member_cards mc "
                    "JOIN members m ON m.id=mc.member_id JOIN cards c ON c.id=mc.card_id "
                    "WHERE mc.id=? AND mc.shop_id=? AND mc.status='active'",
                    (request.args.get("mc"), shop_id)).fetchone()
    if not mc:
        return redirect(url_for("admin_member_cards", shop_id=shop_id))
    services = db.execute("SELECT * FROM services WHERE shop_id=? AND active=1 ORDER BY id",
                          (shop_id,)).fetchall()
    technicians = db.execute("SELECT * FROM technicians WHERE shop_id=? AND active=1 ORDER BY id",
                             (shop_id,)).fetchall()
    return render_template("admin/card_deduct.html", mc=mc, services=services,
                           technicians=technicians)


@app.post("/s/<int:shop_id>/admin/card_buyers/deduct")
@admin_required
def admin_card_buyer_deduct(shop_id):
    """弹窗核销：勾选多个项目 + 选择技师，一次核销多项并逐项留流水"""
    db = get_db()
    o = db.execute("SELECT * FROM member_cards WHERE id=? AND shop_id=? AND status='active'",
                   (request.form.get("id"), shop_id)).fetchone()
    svcs = [int(s) for s in request.form.getlist("services")
            if db.execute("SELECT 1 FROM services WHERE id=? AND shop_id=?",
                          (s, shop_id)).fetchone()]
    cost = sum(db.execute("SELECT deduct_times FROM services WHERE id=?", (s,)).fetchone()[0]
               for s in svcs)
    tid = request.form.get("technician_id") or None
    if o and svcs and o["times_left"] >= cost:
        db.execute("UPDATE member_cards SET times_left=times_left-? WHERE id=?",
                   (cost, o["id"]))
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        db.executemany("INSERT INTO card_logs (shop_id, member_card_id, service_id, "
                       "technician_id, created_at) VALUES (?,?,?,?,?)",
                       [(shop_id, o["id"], s, tid, now) for s in svcs])
        db.commit()
        flash(f"已核销 {len(svcs)} 个项目共 {cost} 次，剩余 {o['times_left'] - cost} 次")
    elif o:
        flash(f"剩余次数不足（剩 {o['times_left']} 次）" if svcs else "请先勾选项目")
    if request.form.get("back") == "deduct":
        return redirect(url_for("admin_card_deduct", shop_id=shop_id,
                                mc=request.form.get("id")))
    return redirect(url_for("admin_card_buyers", shop_id=shop_id,
                            card=request.form.get("card")))


@app.get("/s/<int:shop_id>/admin/card_logs")
@admin_required
def admin_card_logs(shop_id):
    """某会员卡的核销流水管理页"""
    db = get_db()
    mc = db.execute("SELECT mc.*, m.phone, c.name cname FROM member_cards mc "
                    "JOIN members m ON m.id=mc.member_id JOIN cards c ON c.id=mc.card_id "
                    "WHERE mc.id=? AND mc.shop_id=?", (request.args.get("mc"), shop_id)).fetchone()
    if not mc:
        return redirect(url_for("admin_cards", shop_id=shop_id))
    logs = db.execute(
        "SELECT l.*, s.name sname, t.name tname FROM card_logs l "
        "LEFT JOIN services s ON s.id=l.service_id LEFT JOIN technicians t ON t.id=l.technician_id "
        "WHERE l.member_card_id=? ORDER BY l.id DESC", (mc["id"],)).fetchall()
    services = db.execute("SELECT * FROM services WHERE shop_id=? ORDER BY id", (shop_id,)).fetchall()
    technicians = db.execute("SELECT * FROM technicians WHERE shop_id=? AND active=1 ORDER BY id",
                             (shop_id,)).fetchall()
    return render_template("admin/card_logs.html", mc=mc, logs=logs,
                           services=services, technicians=technicians)


@app.post("/s/<int:shop_id>/admin/card_logs/update")
@admin_required
def admin_card_log_update(shop_id):
    """修改流水：项目 / 技师"""
    db = get_db()
    l = db.execute("SELECT * FROM card_logs WHERE id=? AND shop_id=?",
                   (request.form.get("id"), shop_id)).fetchone()
    if l:
        db.execute("UPDATE card_logs SET service_id=?, technician_id=? WHERE id=?",
                   (request.form.get("service_id") or l["service_id"],
                    request.form.get("technician_id") or None, l["id"]))
        db.commit()
        flash("流水已修改")
    return redirect(url_for("admin_card_logs", shop_id=shop_id,
                            mc=request.form.get("mc")))


@app.post("/s/<int:shop_id>/admin/card_logs/delete")
@admin_required
def admin_card_log_delete(shop_id):
    """删除流水并回补 1 次（二级需一级验证码）"""
    if get_admin()["role"] == "staff" and not check_vcode(shop_id, request.form.get("vcode")):
        flash("删除流水需一级管理员验证码")
        return redirect(url_for("admin_card_logs", shop_id=shop_id, mc=request.form.get("mc")))
    db = get_db()
    l = db.execute("SELECT * FROM card_logs WHERE id=? AND shop_id=?",
                   (request.form.get("id"), shop_id)).fetchone()
    if l:
        db.execute("DELETE FROM card_logs WHERE id=?", (l["id"],))
        db.execute("UPDATE member_cards SET times_left=times_left+1 WHERE id=?",
                   (l["member_card_id"],))
        log_op(shop_id, "删除核销流水", request.form.get("id"))
        db.commit()
        flash("流水已删除，次数已回补 1 次")
    return redirect(url_for("admin_card_logs", shop_id=shop_id,
                            mc=request.form.get("mc")))


@app.post("/s/<int:shop_id>/admin/card_buyers/update")
@admin_required
def admin_card_buyer_update(shop_id):
    """修改购买记录：剩余次数 / 状态（二级需一级验证码）"""
    if get_admin()["role"] == "staff" and not check_vcode(shop_id, request.form.get("vcode")):
        flash("调整会员卡次数需一级管理员验证码")
        return redirect(url_for("admin_card_buyers", shop_id=shop_id, card=request.form.get("card")))
    db = get_db()
    o = db.execute("SELECT * FROM member_cards WHERE id=? AND shop_id=?",
                   (request.form.get("id"), shop_id)).fetchone()
    if o:
        status = request.form.get("status")
        if status not in ("pending", "active", "cancelled"):
            status = o["status"]
        db.execute("UPDATE member_cards SET times_left=?, status=? WHERE id=?",
                   (max(0, request.form.get("times_left", type=int) or 0), status, o["id"]))
        log_op(shop_id, "调整会员卡次数", f"卡记录{o['id']}")
        db.commit()
        flash("购买记录已修改")
    return redirect(url_for("admin_card_buyers", shop_id=shop_id,
                            card=request.form.get("card")))


@app.post("/s/<int:shop_id>/admin/cards/delete")
@admin_required
@super_required
def admin_card_delete(shop_id):
    db = get_db()
    db.execute("DELETE FROM cards WHERE id=? AND shop_id=?", (request.form.get("id"), shop_id))
    log_op(shop_id, "删除卡档", request.form.get("id"))
    db.commit()
    flash("卡已删除（已购会员卡不受影响）")
    return redirect(url_for("admin_cards", shop_id=shop_id))


@app.get("/s/<int:shop_id>/admin/member_cards")
@admin_required
def admin_member_cards(shop_id):
    """生效会员卡：核销扣次"""
    q = request.args.get("q", "").strip()
    rows = get_db().execute(
        "SELECT mc.*, m.phone, c.name cname, mc.created_at AS issued_at FROM member_cards mc "
        "JOIN members m ON m.id=mc.member_id JOIN cards c ON c.id=mc.card_id "
        "WHERE mc.shop_id=? AND mc.status='active' AND (?='' OR m.phone LIKE '%'||?||'%') "
        "ORDER BY mc.id DESC", (shop_id, q, q)).fetchall()
    return render_template("admin/member_cards.html", cards=rows, q=q)


@app.get("/s/<int:shop_id>/admin/members")
@admin_required
def admin_members(shop_id):
    db = get_db()
    card = request.args.get("card", type=int)
    q = request.args.get("q", "").strip()
    rows = db.execute(
        "SELECT m.* FROM members m WHERE m.shop_id=? "
        "AND (? IS NULL OR m.id IN (SELECT member_id FROM member_cards WHERE card_id=? "
        "AND status='active') OR (?=-1 AND m.id NOT IN "
        "(SELECT member_id FROM member_cards WHERE status='active'))) "
        "AND (?='' OR m.phone LIKE '%'||?||'%') ORDER BY m.id",
        (shop_id, card, card, card, q, q)).fetchall()
    # 每个会员买的卡（卡名+剩余次数），按类别组装
    cards_info = {}
    for r in db.execute(
            "SELECT mc.member_id, c.name, mc.times_left, mc.status FROM member_cards mc "
            "JOIN cards c ON c.id=mc.card_id WHERE mc.shop_id=?", (shop_id,)):
        cards_info.setdefault(r["member_id"], []).append(
            f"{r['name']}(剩{r['times_left']}次{'·待确认' if r['status'] == 'pending' else ''})")
    cards = db.execute("SELECT * FROM cards WHERE shop_id=? ORDER BY id", (shop_id,)).fetchall()
    return render_template("admin/members.html", members=rows,
                           cards_info=cards_info, cards=cards, card=card, q=q)


@app.post("/s/<int:shop_id>/admin/members/add")
@admin_required
def admin_member_add(shop_id):
    """后台新增会员档案"""
    f = request.form
    phone, name = f.get("phone", "").strip(), f.get("name", "").strip()
    if not PHONE_RE.match(phone):
        flash("手机号格式不正确")
    elif get_db().execute("SELECT 1 FROM members WHERE shop_id=? AND phone=?",
                          (shop_id, phone)).fetchone():
        flash("该手机号已是会员")
    else:
        db = get_db()
        db.execute("INSERT INTO members (shop_id, phone, name, gender, birthday, created_at) "
                   "VALUES (?,?,?,?,?,?)",
                   (shop_id, phone, name, f.get("gender", ""), f.get("birthday", ""),
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        log_op(shop_id, "新增会员", f"{name} {phone}")
        db.commit()
        flash("会员已新增")
    return redirect(url_for("admin_members", shop_id=shop_id))


@app.post("/s/<int:shop_id>/admin/members/update")
@admin_required
def admin_member_update(shop_id):
    db = get_db()
    m = db.execute("SELECT * FROM members WHERE id=? AND shop_id=?",
                   (request.form.get("id"), shop_id)).fetchone()
    if m:
        db.execute("UPDATE members SET name=?, gender=?, birthday=? WHERE id=?",
                   (request.form.get("name", "").strip(),
                    request.form.get("gender", ""),
                    request.form.get("birthday", ""), m["id"]))
        db.commit()
        flash("会员信息已修改")
    return redirect(url_for("admin_members", shop_id=shop_id))


@app.post("/s/<int:shop_id>/admin/members/delete")
@admin_required
def admin_member_delete(shop_id):
    """删除会员及其预约、会员卡、核销流水（二级需一级验证码）"""
    if get_admin()["role"] == "staff" and not check_vcode(shop_id, request.form.get("vcode")):
        flash("删除会员需一级管理员验证码")
        return redirect(url_for("admin_members", shop_id=shop_id))
    db = get_db()
    m = db.execute("SELECT * FROM members WHERE id=? AND shop_id=?",
                   (request.form.get("id"), shop_id)).fetchone()
    if m:
        db.execute("DELETE FROM card_logs WHERE member_card_id IN "
                   "(SELECT id FROM member_cards WHERE member_id=?)", (m["id"],))
        db.execute("DELETE FROM member_cards WHERE member_id=?", (m["id"],))
        db.execute("DELETE FROM members WHERE id=?", (m["id"],))
        log_op(shop_id, "删除会员", m["phone"])
        db.commit()
        flash(f"会员 {m['phone']} 及其会员卡数据已删除")
    return redirect(url_for("admin_members", shop_id=shop_id))


@app.route("/s/<int:shop_id>/admin/config", methods=["GET", "POST"])
@admin_required
@super_required
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


@app.post("/s/<int:shop_id>/admin/api/send_vcode")
@admin_required
def admin_send_vcode(shop_id):
    """发送敏感操作验证码到一级管理员手机（开发模式返回 dev_code）"""
    adm = get_admin()
    if adm and adm["role"] == "super":
        return jsonify(ok=True, msg="一级操作无需验证码")
    phone = get_db().execute("SELECT phone FROM admins WHERE shop_id=? AND role='super' "
                             "AND phone!='' LIMIT 1", (shop_id,)).fetchone()
    if not phone:
        return jsonify(ok=False, msg="本店一级管理员未绑定手机号，请联系一级管理员")
    phone = phone[0]
    db = get_db()
    row = db.execute("SELECT code, expires_at FROM sms_codes WHERE shop_id=? AND phone=? "
                     "ORDER BY expires_at DESC LIMIT 1", (shop_id, phone)).fetchone()
    if row and row["expires_at"] - time.time() > 240:
        code = row["code"]
    else:
        code = f"{random.randint(0, 999999):06d}"
        db.execute("DELETE FROM sms_codes WHERE shop_id=? AND phone=?", (shop_id, phone))
        db.execute("INSERT INTO sms_codes (shop_id, phone, code, expires_at) VALUES (?,?,?,?)",
                   (shop_id, phone, code, time.time() + 300))
        db.commit()
        send_sms(phone, code)
    return jsonify(ok=True, dev_code=code)


# ---- 账号管理（一级） ----

@app.route("/s/<int:shop_id>/admin/accounts", methods=["GET", "POST"])
@admin_required
@super_required
def admin_accounts(shop_id):
    db = get_db()
    if request.method == "POST":
        f = request.form
        if f.get("action") == "add":
            username, pw = f.get("username", "").strip(), f.get("password", "").strip()
            if len(username) < 2 or len(pw) < 6:
                flash("账号至少 2 位、密码至少 6 位")
            elif db.execute("SELECT 1 FROM admins WHERE shop_id=? AND username=?",
                            (shop_id, username)).fetchone():
                flash("账号已存在")
            else:
                db.execute("INSERT INTO admins (shop_id, username, pass, role) VALUES (?,?,?,?)",
                           (shop_id, username, hash_pass(pw), "staff"))
                log_op(shop_id, "创建二级账号", username)
                db.commit()
                flash(f"二级账号 {username} 已创建，密码 {pw}")
        elif f.get("action") == "toggle":
            a = db.execute("SELECT * FROM admins WHERE id=? AND shop_id=?", (f.get("id"), shop_id)).fetchone()
            if a and a["role"] == "staff":
                db.execute("UPDATE admins SET active=? WHERE id=?", (0 if a["active"] else 1, a["id"]))
                log_op(shop_id, "启用/禁用二级账号", a["username"])
                db.commit()
                flash(f"账号 {a['username']} 已{'禁用' if a['active'] else '启用'}")
        elif f.get("action") == "delete":
            a = db.execute("SELECT * FROM admins WHERE id=? AND shop_id=?", (f.get("id"), shop_id)).fetchone()
            if a and a["role"] == "staff":
                db.execute("DELETE FROM admins WHERE id=?", (a["id"],))
                log_op(shop_id, "删除二级账号", a["username"])
                db.commit()
                flash(f"账号 {a['username']} 已删除")
        elif f.get("action") == "bind_phone":
            phone = f.get("phone", "").strip()
            if PHONE_RE.match(phone):
                db.execute("UPDATE admins SET phone=? WHERE shop_id=? AND role='super'",
                           (phone, shop_id))
                log_op(shop_id, "绑定验证码手机", phone)
                db.commit()
                flash("敏感操作验证码接收手机已绑定")
            else:
                flash("手机号格式不正确")
        return redirect(url_for("admin_accounts", shop_id=shop_id))
    accounts = db.execute("SELECT * FROM admins WHERE shop_id=? ORDER BY role DESC, id",
                          (shop_id,)).fetchall()
    return render_template("admin/accounts.html", accounts=accounts)


@app.route("/s/<int:shop_id>/admin/change_pw", methods=["GET", "POST"])
@admin_required
def admin_change_pw(shop_id):
    if request.method == "GET":
        return render_template("admin/change_pw.html")
    adm = get_admin()
    old, new = request.form.get("old", ""), request.form.get("new", "")
    if not check_pass(adm["pass"], old):
        flash("原密码错误")
    elif len(new) < 6:
        flash("新密码至少 6 位")
    else:
        db = get_db()
        db.execute("UPDATE admins SET pass=? WHERE id=?", (hash_pass(new), adm["id"]))
        log_op(shop_id, "修改密码", adm["username"])
        db.commit()
        flash("密码已修改")
    return redirect(url_for("admin_accounts", shop_id=shop_id)
                    if adm["role"] == "super" else url_for("admin_index", shop_id=shop_id))


# ---- 收银下单 ----

@app.route("/s/<int:shop_id>/admin/pos", methods=["GET", "POST"])
@admin_required
def admin_pos(shop_id):
    db = get_db()
    if request.method == "POST":
        f = request.form
        member_id = f.get("member_id", type=int) or None
        svc_ids = [int(s) for s in f.getlist("services")
                   if db.execute("SELECT 1 FROM services WHERE id=? AND shop_id=?",
                                 (s, shop_id)).fetchone()]
        if not svc_ids:
            flash("请至少勾选一个项目")
            return redirect(url_for("admin_pos", shop_id=shop_id))
        total = sum(db.execute("SELECT price FROM services WHERE id=?",
                               (s,)).fetchone()[0] for s in svc_ids)
        discount = min(float(f.get("discount") or 0), total)
        pay_method = f.get("pay_method", "现金")
        member_card = f.get("member_card", type=int) or None
        adm = get_admin()
        if adm["role"] == "staff" and discount > float(get_settings().get("discount_limit") or 0) \
                and not check_vcode(shop_id, f.get("vcode")):
            flash("减免金额超过阈值，需一级管理员验证码")
            return redirect(url_for("admin_pos", shop_id=shop_id))
        if pay_method == "划卡" and not member_card:
            flash("划卡支付需选择会员卡")
            return redirect(url_for("admin_pos", shop_id=shop_id))
        items = []
        for s in svc_ids:
            row = db.execute("SELECT name, price, deduct_times FROM services WHERE id=?", (s,)).fetchone()
            tech = db.execute("SELECT name FROM technicians WHERE id=? AND shop_id=?",
                              (f.get(f"tech_{s}") or None, shop_id)).fetchone()
            items.append({"name": row["name"], "price": row["price"],
                          "tech": tech["name"] if tech else "不指定",
                          "deduct_times": row["deduct_times"]})
        oid = db.execute("INSERT INTO orders (shop_id, order_no, member_id, member_card_id, "
                         "items_json, total, discount, pay_method, status, operator_id, created_at) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (shop_id, datetime.now().strftime("%Y%m%d%H%M%S") + str(random.randint(10, 99)),
                          member_id, member_card, json.dumps(items, ensure_ascii=False),
                          total, discount, pay_method, "unpaid", session["aid"],
                          datetime.now().strftime("%Y-%m-%d %H:%M:%S"))).lastrowid
        db.commit()
        return redirect(url_for("admin_pay", shop_id=shop_id, oid=oid))
    services = db.execute("SELECT * FROM services WHERE shop_id=? AND active=1 ORDER BY id",
                          (shop_id,)).fetchall()
    technicians = db.execute("SELECT * FROM technicians WHERE shop_id=? AND active=1 ORDER BY id",
                             (shop_id,)).fetchall()
    return render_template("admin/pos.html", services=services, technicians=technicians)


@app.get("/s/<int:shop_id>/admin/pay/<int:oid>")
@admin_required
def admin_pay(shop_id, oid):
    """付款展示页：大字金额出示给客户"""
    o = get_db().execute("SELECT * FROM orders WHERE id=? AND shop_id=?",
                         (oid, shop_id)).fetchone()
    if not o or o["status"] != "unpaid":
        return redirect(url_for("admin_orders", shop_id=shop_id))
    member = None
    if o["member_id"]:
        member = get_db().execute("SELECT * FROM members WHERE id=?", (o["member_id"],)).fetchone()
    return render_template("admin/pay.html", o=o,
                           items=json.loads(o["items_json"]), member=member)


@app.post("/s/<int:shop_id>/admin/pay/confirm")
@admin_required
def admin_pay_confirm(shop_id):
    db = get_db()
    o = db.execute("SELECT * FROM orders WHERE id=? AND shop_id=? AND status='unpaid'",
                   (request.form.get("oid"), shop_id)).fetchone()
    if o:
        db.execute("UPDATE orders SET status='paid' WHERE id=?", (o["id"],))
        if o["pay_method"] == "划卡" and o["member_card_id"]:
            mc = db.execute("SELECT * FROM member_cards WHERE id=? AND shop_id=? AND status='active'",
                            (o["member_card_id"], shop_id)).fetchone()
            cost = sum(it.get("deduct_times", 1) for it in json.loads(o["items_json"]))
            if mc and mc["times_left"] >= cost:
                db.execute("UPDATE member_cards SET times_left=times_left-? WHERE id=?", (cost, mc["id"]))
                now = datetime.now().strftime("%Y-%m-%d %H:%M")
                db.executemany("INSERT INTO card_logs (shop_id, member_card_id, service_id, "
                               "technician_id, created_at) VALUES (?,?,?,?,?)",
                               [(shop_id, mc["id"], None, None, now)] * cost)
        log_op(shop_id, "收款完成", f"{o['order_no']} ¥{o['total'] - o['discount']:g}")
        db.commit()
        flash("收款完成")
    return redirect(url_for("admin_orders", shop_id=shop_id))


@app.get("/s/<int:shop_id>/admin/orders")
@admin_required
def admin_orders(shop_id):
    st = request.args.get("status", "today")
    db = get_db()
    q = "SELECT o.*, m.phone, a.username opname FROM orders o " \
        "LEFT JOIN members m ON m.id=o.member_id LEFT JOIN admins a ON a.id=o.operator_id " \
        "WHERE o.shop_id=?"
    args = [shop_id]
    if st == "today":
        q += " AND o.created_at LIKE ?"
        args.append(datetime.now().strftime("%Y-%m-%d") + "%")
    elif st != "all":
        q += " AND o.status=?"
        args.append(st)
    rows = db.execute(q + " ORDER BY o.id DESC", args).fetchall()
    return render_template("admin/orders.html", orders=rows, st=st)


@app.post("/s/<int:shop_id>/admin/orders/refund")
@admin_required
def admin_order_refund(shop_id):
    adm = get_admin()
    if adm["role"] == "staff" and not check_vcode(shop_id, request.form.get("vcode")):
        flash("退款需一级管理员验证码")
        return redirect(url_for("admin_orders", shop_id=shop_id))
    db = get_db()
    o = db.execute("SELECT * FROM orders WHERE id=? AND shop_id=? AND status='paid'",
                   (request.form.get("id"), shop_id)).fetchone()
    if o:
        db.execute("UPDATE orders SET status='refunded' WHERE id=?", (o["id"],))
        if o["member_card_id"]:  # 划卡订单退款回补次数
            db.execute("UPDATE member_cards SET times_left=times_left+? WHERE id=?",
                       (len(json.loads(o["items_json"])), o["member_card_id"]))
        log_op(shop_id, "退款", f"{o['order_no']} ¥{o['total'] - o['discount']:g}")
        db.commit()
        flash("订单已退款")
    return redirect(url_for("admin_orders", shop_id=shop_id))


@app.post("/s/<int:shop_id>/admin/orders/void")
@admin_required
@super_required
def admin_order_void(shop_id):
    db = get_db()
    o = db.execute("SELECT * FROM orders WHERE id=? AND shop_id=? AND status='unpaid'",
                   (request.form.get("id"), shop_id)).fetchone()
    if o:
        db.execute("UPDATE orders SET status='void' WHERE id=?", (o["id"],))
        log_op(shop_id, "作废订单", o["order_no"])
        db.commit()
        flash("订单已作废")
    return redirect(url_for("admin_orders", shop_id=shop_id))


@app.get("/s/<int:shop_id>/admin/shift")
@admin_required
def admin_shift(shop_id):
    """交接班：今日营收汇总"""
    db = get_db()
    today = datetime.now().strftime("%Y-%m-%d") + "%"
    rows = db.execute("SELECT pay_method, COUNT(*) cnt, SUM(total-discount) amt FROM orders "
                      "WHERE shop_id=? AND status='paid' AND created_at LIKE ? "
                      "GROUP BY pay_method", (shop_id, today)).fetchall()
    total = sum(r["amt"] for r in rows)
    cnt = sum(r["cnt"] for r in rows)
    return render_template("admin/shift.html", rows=rows, total=total, cnt=cnt)


# ---- 技师业绩（提成参考） ----

@app.get("/s/<int:shop_id>/admin/tech_stats")
@admin_required
def admin_tech_stats(shop_id):
    """技师开了几单：订单中每个技师的单数与业绩金额"""
    days = request.args.get("days", "today")
    db = get_db()
    q = "SELECT items_json FROM orders WHERE shop_id=? AND status='paid'"
    args = [shop_id]
    if days == "today":
        q += " AND created_at LIKE ?"
        args.append(datetime.now().strftime("%Y-%m-%d") + "%")
    stats = {}  # 技师名 -> {"cnt": 单数, "amt": 业绩}
    for (ij,) in db.execute(q, args):
        for it in json.loads(ij):
            t = it.get("tech", "不指定")
            stats.setdefault(t, {"cnt": 0, "amt": 0.0})
            stats[t]["cnt"] += 1
            stats[t]["amt"] += it["price"]
    rows = sorted(stats.items(), key=lambda kv: -kv[1]["amt"])
    return render_template("admin/tech_stats.html", rows=rows, days=days)


# ---- 后台售卡开卡 ----

@app.route("/s/<int:shop_id>/admin/card_sell", methods=["GET", "POST"])
@admin_required
def admin_card_sell(shop_id):
    db = get_db()
    if request.method == "POST":
        member_id = request.form.get("member_id", type=int)
        card_id = request.form.get("card_id", type=int)
        c = db.execute("SELECT * FROM cards WHERE id=? AND shop_id=?", (card_id, shop_id)).fetchone()
        if not member_id and request.form.get("phone"):
            # 新客户：直接建档再开卡
            phone, name = request.form.get("phone", "").strip(), request.form.get("name", "").strip()
            if not PHONE_RE.match(phone):
                flash("手机号格式不正确")
                return redirect(url_for("admin_card_sell", shop_id=shop_id))
            if db.execute("SELECT 1 FROM members WHERE shop_id=? AND phone=?", (shop_id, phone)).fetchone():
                flash("该手机号已是会员，请在搜索框选择")
                return redirect(url_for("admin_card_sell", shop_id=shop_id))
            member_id = db.execute("INSERT INTO members (shop_id, phone, name, created_at) "
                                   "VALUES (?,?,?,?)",
                                   (shop_id, phone, name,
                                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"))).lastrowid
            log_op(shop_id, "新增会员", f"{name} {phone}")
        m = db.execute("SELECT * FROM members WHERE id=? AND shop_id=?", (member_id, shop_id)).fetchone()
        if m and c:
            db.execute("INSERT INTO member_cards (shop_id, member_id, card_id, times_left, "
                       "status, created_at) VALUES (?,?,?,?, 'active', ?)",
                       (shop_id, member_id, card_id, c["times"],
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            log_op(shop_id, "售卡开卡", f"{m['phone']} {c['name']} ¥{c['price']:g}")
            db.commit()
            flash(f"已为 {m['phone']} 开卡「{c['name']}」（{c['times']} 次，售价 ¥{c['price']:g}）")
        else:
            flash("会员或卡不存在")
        return redirect(url_for("admin_card_sell", shop_id=shop_id))
    cards = db.execute("SELECT * FROM cards WHERE shop_id=? AND active=1 ORDER BY id",
                       (shop_id,)).fetchall()
    return render_template("admin/card_sell.html", cards=cards)


# ---- 操作日志（一级） ----

@app.get("/s/<int:shop_id>/admin/oplogs")
@admin_required
@super_required
def admin_oplogs(shop_id):
    rows = get_db().execute(
        "SELECT l.*, a.username FROM op_logs l LEFT JOIN admins a ON a.id=l.admin_id "
        "WHERE l.shop_id=? ORDER BY l.id DESC LIMIT 200", (shop_id,)).fetchall()
    return render_template("admin/oplogs.html", logs=rows)


# ---------- 启动 ----------

# 模块加载即初始化数据库（不依赖启动方式），重复执行安全
init_db(DB_PATH)
_db = sqlite3.connect(DB_PATH)
_platform_pw = seed(_db)
_db.close()
if _platform_pw:
    print(f"[平台总后台初始账号] platform / {_platform_pw}  （仅首次启动显示，请妥善保管）")


def selftest():
    """核心流程自检：开店、两级权限、收银下单、敏感操作验证码、日志留痕"""
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
        s["csrf"] = "selftest"

    # 根路径重定向到平台登录
    assert c.get("/").headers["Location"].endswith("/platform/login")
    # 平台开店，拿店 1 一级密码
    assert c.post("/platform/login", data={"username": "platform", "password": plat_pw,
                                           "_csrf": "selftest"}).status_code == 302
    html = c.post("/platform/shops/add", data={"name": "自检店", "_csrf": "selftest"},
                  follow_redirects=True).get_data(as_text=True)
    pw1 = re.search(r"密码 (\w+)", html).group(1)
    # 一级登录、绑定验证码手机、创建二级账号
    assert c.post("/s/1/admin/login", data={"username": "admin", "password": pw1,
                                            "_csrf": "selftest"}).status_code == 302
    c.post("/s/1/admin/accounts", data={"action": "bind_phone", "phone": "13811112222",
                                        "_csrf": "selftest"})
    c.post("/s/1/admin/accounts", data={"action": "add", "username": "staff1",
                                        "password": "staff123", "_csrf": "selftest"})
    db = sqlite3.connect(DB_PATH)
    assert db.execute("SELECT role FROM admins WHERE username='staff1'").fetchone()[0] == "staff"
    db.close()
    c.get("/s/1/admin/logout")
    # 新增会员 + 售卡开卡
    assert c.post("/s/1/admin/login", data={"username": "admin", "password": pw1,
                                            "_csrf": "selftest"}).status_code == 302
    c.post("/s/1/admin/members/add", data={"name": "测试会员", "phone": "13900001111",
                                           "gender": "男", "_csrf": "selftest"})
    db = sqlite3.connect(DB_PATH)
    mid = db.execute("SELECT id FROM members WHERE phone='13900001111'").fetchone()[0]
    db.close()
    c.post("/s/1/admin/card_sell", data={"member_id": str(mid), "card_id": "1",
                                         "_csrf": "selftest"})
    db = sqlite3.connect(DB_PATH)
    mcid = db.execute("SELECT id FROM member_cards WHERE member_id=?", (mid,)).fetchone()[0]
    assert db.execute("SELECT times_left FROM member_cards WHERE id=?", (mcid,)).fetchone()[0] == 10
    db.close()
    # 二级登录
    c.get("/s/1/admin/logout")
    assert c.post("/s/1/admin/login", data={"username": "staff1", "password": "staff123",
                                            "_csrf": "selftest"}).status_code == 302
    # 二级访问一级页面被拒（卡档管理 302 回首页）
    r = c.get("/s/1/admin/cards", follow_redirects=True)
    assert "仅限一级管理员" in r.get_data(as_text=True)
    # 收银下单（现金，2 个项目）
    from werkzeug.datastructures import MultiDict
    db = sqlite3.connect(DB_PATH)
    svcs = [str(r[0]) for r in db.execute("SELECT id FROM services WHERE shop_id=1 LIMIT 2")]
    # 新店项目默认 0 元，自检里先定价再走收银
    db.execute("UPDATE services SET price=128 WHERE id=?", (svcs[0],))
    db.execute("UPDATE services SET price=88 WHERE id=?", (svcs[1],))
    db.commit()
    db.close()
    c.post("/s/1/admin/pos", data=MultiDict([
        ("_csrf", "selftest"), ("member_id", str(mid)), ("services", svcs[0]),
        ("services", svcs[1]), (f"tech_{svcs[0]}", "1"), ("discount", "0"),
        ("pay_method", "现金")]))
    db = sqlite3.connect(DB_PATH)
    oid = db.execute("SELECT id FROM orders WHERE status='unpaid'").fetchone()[0]
    assert db.execute("SELECT total FROM orders WHERE id=?", (oid,)).fetchone()[0] == 216
    db.close()
    # 付款展示页 + 确认收款
    assert "请客户付款" in c.get(f"/s/1/admin/pay/{oid}").get_data(as_text=True)
    c.post("/s/1/admin/pay/confirm", data={"oid": str(oid), "_csrf": "selftest"})
    db = sqlite3.connect(DB_PATH)
    assert db.execute("SELECT status FROM orders WHERE id=?", (oid,)).fetchone()[0] == "paid"
    db.close()
    # 划卡下单：扣会员卡次数并留流水
    c.post("/s/1/admin/pos", data=MultiDict([
        ("_csrf", "selftest"), ("member_id", str(mid)), ("services", svcs[0]),
        ("discount", "0"), ("pay_method", "划卡"), ("member_card", str(mcid))]))
    db = sqlite3.connect(DB_PATH)
    oid2 = db.execute("SELECT id FROM orders WHERE pay_method='划卡' AND status='unpaid'").fetchone()[0]
    db.close()
    c.post("/s/1/admin/pay/confirm", data={"oid": str(oid2), "_csrf": "selftest"})
    db = sqlite3.connect(DB_PATH)
    assert db.execute("SELECT times_left FROM member_cards WHERE id=?", (mcid,)).fetchone()[0] == 9
    db.close()
    # 二级删除会员：无验证码被拒，有验证码通过
    assert "验证码" in c.post("/s/1/admin/members/delete", data={"id": str(mid), "_csrf": "selftest"},
                              follow_redirects=True).get_data(as_text=True)
    r = c.post("/s/1/admin/api/send_vcode", data={"_csrf": "selftest"}).get_json()
    assert r["ok"] and r["dev_code"]
    c.post("/s/1/admin/members/delete", data={"id": str(mid), "vcode": r["dev_code"],
                                              "_csrf": "selftest"})
    db = sqlite3.connect(DB_PATH)
    assert db.execute("SELECT COUNT(*) FROM members WHERE id=?", (mid,)).fetchone()[0] == 0
    db.close()
    # 二级改价超阈值被拒（阈值 50，减 100）
    db = sqlite3.connect(DB_PATH)
    db.execute("INSERT INTO settings (shop_id, key, value) VALUES (1, 'discount_limit', '50')")
    db.commit()
    db.close()
    assert "需一级管理员验证码" in c.post("/s/1/admin/pos", data=MultiDict([
        ("_csrf", "selftest"), ("services", svcs[0]), ("discount", "100"),
        ("pay_method", "现金")]), follow_redirects=True).get_data(as_text=True)
    # 二级退款：无验证码被拒
    db = sqlite3.connect(DB_PATH)
    db.execute("UPDATE orders SET status='paid' WHERE id=?", (oid,))
    db.commit()
    db.close()
    assert "退款需一级管理员验证码" in c.post("/s/1/admin/orders/refund",
        data={"id": str(oid), "_csrf": "selftest"}, follow_redirects=True).get_data(as_text=True)
    # 一级直接退款成功
    c.get("/s/1/admin/logout")
    c.post("/s/1/admin/login", data={"username": "admin", "password": pw1, "_csrf": "selftest"})
    c.post("/s/1/admin/orders/refund", data={"id": str(oid), "_csrf": "selftest"})
    db = sqlite3.connect(DB_PATH)
    assert db.execute("SELECT status FROM orders WHERE id=?", (oid,)).fetchone()[0] == "refunded"
    # 操作日志留痕
    actions = {r[0] for r in db.execute("SELECT action FROM op_logs WHERE shop_id=1")}
    assert {"登录", "创建二级账号", "售卡开卡", "收款完成", "退款", "删除会员"} <= actions, actions
    # 交接班有今日营收
    assert "总营收" in c.get("/s/1/admin/shift").get_data(as_text=True)
    db.close()
    DB_PATH.unlink()
    print("selftest OK: 开店、两级权限、收银下单、划卡扣次、敏感操作验证码、退款、日志留痕全部通过")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
        sys.exit()
    print("康怡养生馆多店系统已启动: http://127.0.0.1:5000  (各店后台 /s/<店id>/admin/login)")
    app.run(host="0.0.0.0", port=5000, debug=True)  # 0.0.0.0: 局域网内其他设备可访问
