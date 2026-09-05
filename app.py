"""养生馆内部收银管理后台 — Flask + SQLite 单店版

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
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, sort INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS services (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, category_id INTEGER,
    price REAL, duration INTEGER, desc TEXT DEFAULT '', active INTEGER DEFAULT 1,
    deduct_times INTEGER DEFAULT 1, commission REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS technicians (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, avatar TEXT DEFAULT '',
    intro TEXT DEFAULT '', specialties TEXT DEFAULT '', active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS members (
    id INTEGER PRIMARY KEY AUTOINCREMENT, phone TEXT, name TEXT DEFAULT '',
    gender TEXT DEFAULT '', birthday TEXT DEFAULT '', created_at TEXT);
CREATE TABLE IF NOT EXISTS sms_codes (
    phone TEXT, code TEXT, expires_at REAL);
CREATE TABLE IF NOT EXISTS admins (
    id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT, pass TEXT,
    role TEXT DEFAULT 'super', phone TEXT DEFAULT '', active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT, order_no TEXT, member_id INTEGER,
    member_card_id INTEGER, items_json TEXT, total REAL, discount REAL DEFAULT 0,
    pay_method TEXT, status TEXT DEFAULT 'paid', operator_id INTEGER, created_at TEXT);
CREATE TABLE IF NOT EXISTS op_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER,
    action TEXT, detail TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT,
    price REAL, times INTEGER, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS member_cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT, member_id INTEGER,
    card_id INTEGER, times_left INTEGER, status TEXT DEFAULT 'pending',
    paid REAL DEFAULT 0, created_at TEXT);
CREATE TABLE IF NOT EXISTS card_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, member_card_id INTEGER,
    service_id INTEGER, technician_id INTEGER, created_at TEXT);
"""

# 首次初始化预置项目（名称, 核销扣次）；价格/时长/分类由店长自行设置
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

def _migrate_multi_to_single(old_path, new_path):
    """旧多店库 → 单店库：保留第一家 active 店的全部数据，去掉 shop_id 列"""
    old = sqlite3.connect(old_path)
    sid = old.execute("SELECT id FROM shops WHERE active=1 ORDER BY id LIMIT 1").fetchone()
    new = sqlite3.connect(new_path)
    new.executescript(SCHEMA)
    if sid:
        sid = sid[0]
        for t in ("categories", "services", "technicians", "members", "admins",
                  "orders", "op_logs", "cards", "member_cards", "card_logs"):
            cols = [r[1] for r in old.execute(f"PRAGMA table_info({t})")]
            cols = [c for c in cols if c != "shop_id"]
            rows = old.execute(f"SELECT {','.join(cols)} FROM {t} WHERE shop_id=?", (sid,)).fetchall()
            new.executemany(f"INSERT INTO {t} ({','.join(cols)}) "
                            f"VALUES ({','.join('?' * len(cols))})", rows)
        for k, v in old.execute("SELECT key, value FROM settings WHERE shop_id=?", (sid,)):
            new.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (k, v))
        for p, code, e in old.execute("SELECT phone, code, expires_at FROM sms_codes "
                                      "WHERE shop_id=?", (sid,)):
            new.execute("INSERT INTO sms_codes (phone, code, expires_at) VALUES (?,?,?)",
                        (p, code, e))
    new.commit()
    new.close()
    old.close()


def init_db(path=DB_PATH):
    """建表；旧多店库（有 shops 表）自动备份后迁移为单店库"""
    if path.exists():
        db = sqlite3.connect(path)
        has_shops = db.execute("SELECT 1 FROM sqlite_master WHERE name='shops'").fetchone()
        cols = [r[1] for r in db.execute("PRAGMA table_info(settings)")] \
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='settings'").fetchone() else []
        db.close()
        if has_shops and "shop_id" in cols:
            bak = path.with_name(f"{path.stem}_backup_{datetime.now().strftime('%Y%m%d%H%M%S')}.db")
            path.rename(bak)
            _migrate_multi_to_single(bak, path)
            print(f"[数据库升级] 旧多店数据已备份到 {bak.name}，本店数据已迁移为单店库")
        elif not has_shops and cols and "shop_id" not in cols:
            # 最早期单店库（settings 无 shop_id）结构兼容，直接沿用
            pass
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.commit()
    db.close()


def seed(db):
    """首次运行：预置项目/技师/三档卡/管理员，返回管理员密码"""
    if db.execute("SELECT 1 FROM admins").fetchone():
        return None
    db.executemany(
        "INSERT INTO services (name, category_id, price, duration, desc, active, deduct_times, commission) "
        "VALUES (?,NULL,0,60,'',1,?,0)", [(n, t) for n, t in SEED_SERVICES])
    db.executemany("INSERT INTO technicians (name, specialties, intro) VALUES (?,?,?)",
                   [(n, sp, it) for n, sp, it in SEED_TECHS])
    # 预置三档会员次卡（店长可改价格/次数）
    db.executemany("INSERT INTO cards (name, price, times) VALUES (?,?,?)",
                   [("基础卡", 980, 10), ("进阶卡", 1980, 20), ("尊享卡", 2880, 30)])
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('site_name', '养生馆')")
    pw = "".join(random.choices("abcdefghjkmnpqrstuvwxyz23456789", k=10))
    db.execute("INSERT INTO admins (username, pass) VALUES (?,?)", ("admin", hash_pass(pw)))
    db.commit()
    return pw


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
    get_db().execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, value))


def hash_pass(pw):
    salt = os.urandom(16).hex()
    return salt + "$" + hashlib.sha256((salt + pw).encode()).hexdigest()


def check_pass(stored, pw):
    salt, h = stored.split("$", 1)
    return hashlib.sha256((salt + pw).encode()).hexdigest() == h


def get_admin():
    """当前登录的后台管理员（模板菜单按角色渲染用）"""
    if "adm" not in g:
        g.adm = None
        if session.get("aid"):
            g.adm = get_db().execute("SELECT * FROM admins WHERE id=?",
                                     (session["aid"],)).fetchone()
    return g.adm


@app.context_processor
def inject_shop():
    cfg = get_settings()
    return {"cfg": cfg, "shop": {"name": cfg.get("site_name", "养生馆")},
            "now": datetime.now(), "csrf": _csrf_token(), "adm": get_admin()}


app.jinja_env.filters["yuan"] = lambda v: f"¥{v:g}"
app.jinja_env.filters["fromjson"] = json.loads


# ---------- 短信 ----------

def send_sms(phone, code):
    """发送验证码。开发模式打印到控制台；上线时接入阿里云短信替换此函数即可。
    ponytail: 真实短信待接服务商"""
    print(f"[短信] 发给 {phone}: 验证码 {code}")


# ---------- 登录装饰器 ----------

def admin_required(f):
    @wraps(f)
    def w(*a, **k):
        if not session.get("aid"):
            return redirect(url_for("admin_login"))
        return f(*a, **k)
    return w


def super_required(f):
    """一级管理员专属功能"""
    @wraps(f)
    def w(*a, **k):
        adm = get_admin()
        if not adm or adm["role"] != "super":
            flash("该操作仅限一级管理员")
            return redirect(url_for("admin_index"))
        return f(*a, **k)
    return w


def log_op(action, detail=""):
    """操作留痕"""
    get_db().execute("INSERT INTO op_logs (admin_id, action, detail, created_at) "
                     "VALUES (?,?,?,?)",
                     (session.get("aid"), action, detail,
                      datetime.now().strftime("%Y-%m-%d %H:%M:%S")))


def check_vcode(vcode):
    """二级账号敏感操作验证码校验（一级直接放行）"""
    adm = get_admin()
    if adm and adm["role"] == "super":
        return True
    row = get_db().execute(
        "SELECT code, expires_at FROM sms_codes WHERE phone="
        "(SELECT phone FROM admins WHERE role='super' AND phone!='' LIMIT 1) "
        "ORDER BY expires_at DESC LIMIT 1").fetchone()
    return bool(row and row["code"] == vcode and row["expires_at"] > time.time())


# ---------- 根路径 ----------

@app.get("/")
def root():
    """内部管理后台：无对外网页"""
    return redirect(url_for("admin_login"))


# ---------- 登录 ----------

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        if not rate_limit(f"admin:{request.remote_addr}", 10):
            flash("尝试过于频繁，请稍后再试")
            return redirect(url_for("admin_login"))
        u = request.form.get("username", "")
        pw = request.form.get("password", "")
        row = get_db().execute("SELECT * FROM admins WHERE username=? AND active=1",
                               (u,)).fetchone()
        if row and check_pass(row["pass"], pw):
            session["aid"] = row["id"]
            log_op("登录", u)
            get_db().commit()
            return redirect(url_for("admin_index"))
        flash("账号或密码错误")
        return redirect(url_for("admin_login"))
    return render_template("admin/login.html")


@app.get("/admin/logout")
def admin_logout():
    session.pop("aid", None)
    return redirect(url_for("admin_login"))


@app.get("/admin/api/member_search")
@admin_required
def admin_member_search():
    """收银台会员搜索（手机号/姓名模糊）"""
    q = request.args.get("q", "").strip()
    rows = get_db().execute(
        "SELECT id, phone, name FROM members WHERE phone LIKE ? OR name LIKE ? "
        "LIMIT 5", (f"%{q}%", f"%{q}%")).fetchall()
    return jsonify(members=[dict(r) for r in rows])


@app.get("/admin/api/member_cards")
@admin_required
def admin_member_cards_api():
    """收银台划卡：该会员生效的会员卡"""
    rows = get_db().execute(
        "SELECT mc.id, mc.times_left, c.name cname FROM member_cards mc "
        "JOIN cards c ON c.id=mc.card_id WHERE mc.member_id=? "
        "AND mc.status='active' AND mc.times_left>0",
        (request.args.get("member_id"),)).fetchall()
    return jsonify(cards=[dict(r) for r in rows])


@app.get("/admin/")
@admin_required
def admin_index():
    db = get_db()
    today = datetime.now().strftime("%Y-%m-%d") + "%"
    stats = {
        "今日营收": db.execute("SELECT COALESCE(SUM(total-discount), 0) s FROM orders "
                            "WHERE status='paid' AND created_at LIKE ?", (today,)).fetchone()["s"],
        "今日订单": db.execute("SELECT COUNT(*) c FROM orders WHERE created_at LIKE ?",
                            (today,)).fetchone()["c"],
        "会员数": db.execute("SELECT COUNT(*) c FROM members").fetchone()["c"],
        "在售项目": db.execute("SELECT COUNT(*) c FROM services WHERE active=1").fetchone()["c"],
    }
    recent = db.execute(
        "SELECT o.*, a.username opname FROM orders o LEFT JOIN admins a ON a.id=o.operator_id "
        "WHERE o.created_at LIKE ? ORDER BY o.id DESC LIMIT 8", (today,)).fetchall()
    return render_template("admin/index.html", stats=stats, recent=recent)


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
@super_required
def admin_service_save():
    f = request.form
    if not (f.get("name", "").strip() and f.get("price") and f.get("duration")):
        flash("请填写完整的项目信息")
        return redirect(url_for("admin_services"))
    fields = (f["name"].strip(), f.get("category_id", type=int), float(f["price"]),
              int(f["duration"]), f.get("desc", "").strip(), 1 if f.get("active") else 0,
              max(1, f.get("deduct_times", type=int) or 1),
              max(0.0, f.get("commission", type=float) or 0))
    db = get_db()
    if sid := f.get("id"):
        db.execute("UPDATE services SET name=?, category_id=?, price=?, duration=?, "
                   "desc=?, active=?, deduct_times=?, commission=? WHERE id=?",
                   fields + (sid,))
    else:
        db.execute("INSERT INTO services (name, category_id, price, duration, "
                   "desc, active, deduct_times, commission) VALUES (?,?,?,?,?,?,?,?)", fields)
    db.commit()
    flash("项目已保存")
    return redirect(url_for("admin_services"))


@app.post("/admin/services/delete")
@admin_required
@super_required
def admin_service_delete():
    db = get_db()
    db.execute("DELETE FROM services WHERE id=?", (request.form.get("id"),))
    log_op("删除项目", request.form.get("id"))
    db.commit()
    flash("项目已删除")
    return redirect(url_for("admin_services"))


@app.post("/admin/categories/add")
@admin_required
@super_required
def admin_category_add():
    name = request.form.get("name", "").strip()
    if name:
        db = get_db()
        db.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (name,))
        db.commit()
    return redirect(url_for("admin_services"))


@app.post("/admin/categories/delete")
@admin_required
@super_required
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
@super_required
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
        db.execute("INSERT INTO technicians (name, avatar, intro, specialties, "
                   "active) VALUES (?,?,?,?,?)", fields)
    db.commit()
    flash("技师已保存")
    return redirect(url_for("admin_technicians"))


@app.post("/admin/technicians/delete")
@admin_required
@super_required
def admin_technician_delete():
    db = get_db()
    db.execute("DELETE FROM technicians WHERE id=?", (request.form.get("id"),))
    log_op("删除技师", request.form.get("id"))
    db.commit()
    flash("技师已删除")
    return redirect(url_for("admin_technicians"))


# ---------- 会员卡 ----------

@app.route("/admin/cards", methods=["GET", "POST"])
@admin_required
@super_required
def admin_cards():
    """卡档管理：名称/价格/次数/上架"""
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
                           "WHERE id=?", fields + (cid,))
            else:
                db.execute("INSERT INTO cards (name, price, times, active) "
                           "VALUES (?,?,?,?)", fields)
            db.commit()
            flash("卡已保存")
        return redirect(url_for("admin_cards"))
    edit = None
    if eid := request.args.get("edit", type=int):
        edit = db.execute("SELECT * FROM cards WHERE id=?", (eid,)).fetchone()
    cards = db.execute(
        "SELECT c.*, (SELECT COUNT(*) FROM member_cards mc WHERE mc.card_id=c.id "
        "AND mc.status='active') sold, (SELECT COUNT(*) FROM member_cards mc "
        "WHERE mc.card_id=c.id AND mc.status='pending') pending "
        "FROM cards c ORDER BY c.id").fetchall()
    return render_template("admin/cards.html", cards=cards, edit=edit)


@app.get("/admin/card_buyers")
@admin_required
def admin_card_buyers():
    """点击卡名查看该卡全部购买人"""
    cid = request.args.get("card", type=int)
    db = get_db()
    card = db.execute("SELECT * FROM cards WHERE id=?", (cid,)).fetchone()
    if not card:
        return redirect(url_for("admin_cards"))
    st = request.args.get("status", "all")
    q = request.args.get("q", "").strip()
    rows = db.execute(
        "SELECT mc.*, m.phone, m.name mname FROM member_cards mc "
        "LEFT JOIN members m ON m.id=mc.member_id "
        "WHERE mc.card_id=? AND (?='all' OR mc.status=?) "
        "AND (?='' OR m.phone LIKE '%'||?||'%') "
        "ORDER BY mc.id DESC", (cid, st, st, q, q)).fetchall()
    services = db.execute("SELECT * FROM services ORDER BY id").fetchall()
    technicians = db.execute("SELECT * FROM technicians WHERE active=1 ORDER BY id").fetchall()
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


@app.post("/admin/card_buyers/delete")
@admin_required
def admin_card_buyer_delete():
    """删除购买记录（二级需一级验证码）"""
    if get_admin()["role"] == "staff" and not check_vcode(request.form.get("vcode")):
        flash("删除购卡记录需一级管理员验证码")
        return redirect(url_for("admin_card_buyers", card=request.form.get("card")))
    db = get_db()
    db.execute("DELETE FROM member_cards WHERE id=?", (request.form.get("id"),))
    log_op("删除购卡记录", request.form.get("id"))
    db.commit()
    flash("购买记录已删除")
    return redirect(url_for("admin_card_buyers", card=request.form.get("card")))


@app.get("/admin/card_deduct")
@admin_required
def admin_card_deduct():
    """独立核销页：勾选项目 + 技师 + 确认"""
    db = get_db()
    mc = db.execute("SELECT mc.*, m.phone, m.name mname, c.name cname, c.times FROM member_cards mc "
                    "JOIN members m ON m.id=mc.member_id JOIN cards c ON c.id=mc.card_id "
                    "WHERE mc.id=? AND mc.status='active'",
                    (request.args.get("mc"),)).fetchone()
    if not mc:
        return redirect(url_for("admin_member_cards"))
    services = db.execute("SELECT * FROM services WHERE active=1 ORDER BY id").fetchall()
    technicians = db.execute("SELECT * FROM technicians WHERE active=1 ORDER BY id").fetchall()
    return render_template("admin/card_deduct.html", mc=mc, services=services,
                           technicians=technicians)


@app.post("/admin/card_buyers/deduct")
@admin_required
def admin_card_buyer_deduct():
    """弹窗核销：勾选多个项目 + 选择技师，一次核销多项并逐项留流水"""
    db = get_db()
    o = db.execute("SELECT * FROM member_cards WHERE id=? AND status='active'",
                   (request.form.get("id"),)).fetchone()
    svcs = [int(s) for s in request.form.getlist("services")
            if db.execute("SELECT 1 FROM services WHERE id=?", (s,)).fetchone()]
    cost = sum(db.execute("SELECT deduct_times FROM services WHERE id=?", (s,)).fetchone()[0]
               for s in svcs)
    tid = request.form.get("technician_id") or None
    if o and svcs and o["times_left"] >= cost:
        db.execute("UPDATE member_cards SET times_left=times_left-? WHERE id=?",
                   (cost, o["id"]))
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        db.executemany("INSERT INTO card_logs (member_card_id, service_id, "
                       "technician_id, created_at) VALUES (?,?,?,?)",
                       [(o["id"], s, tid, now) for s in svcs])
        db.commit()
        flash(f"已核销 {len(svcs)} 个项目共 {cost} 次，剩余 {o['times_left'] - cost} 次")
    elif o:
        flash(f"剩余次数不足（剩 {o['times_left']} 次）" if svcs else "请先勾选项目")
    if request.form.get("back") == "deduct":
        return redirect(url_for("admin_card_deduct", mc=request.form.get("id")))
    return redirect(url_for("admin_card_buyers", card=request.form.get("card")))


@app.get("/admin/card_logs")
@admin_required
def admin_card_logs():
    """某会员卡的核销流水管理页"""
    db = get_db()
    mc = db.execute("SELECT mc.*, m.phone, c.name cname FROM member_cards mc "
                    "JOIN members m ON m.id=mc.member_id JOIN cards c ON c.id=mc.card_id "
                    "WHERE mc.id=?", (request.args.get("mc"),)).fetchone()
    if not mc:
        return redirect(url_for("admin_cards"))
    logs = db.execute(
        "SELECT l.*, s.name sname, t.name tname FROM card_logs l "
        "LEFT JOIN services s ON s.id=l.service_id LEFT JOIN technicians t ON t.id=l.technician_id "
        "WHERE l.member_card_id=? ORDER BY l.id DESC", (mc["id"],)).fetchall()
    services = db.execute("SELECT * FROM services ORDER BY id").fetchall()
    technicians = db.execute("SELECT * FROM technicians WHERE active=1 ORDER BY id").fetchall()
    return render_template("admin/card_logs.html", mc=mc, logs=logs,
                           services=services, technicians=technicians)


@app.post("/admin/card_logs/update")
@admin_required
def admin_card_log_update():
    """修改流水：项目 / 技师"""
    db = get_db()
    l = db.execute("SELECT * FROM card_logs WHERE id=?", (request.form.get("id"),)).fetchone()
    if l:
        db.execute("UPDATE card_logs SET service_id=?, technician_id=? WHERE id=?",
                   (request.form.get("service_id") or l["service_id"],
                    request.form.get("technician_id") or None, l["id"]))
        db.commit()
        flash("流水已修改")
    return redirect(url_for("admin_card_logs", mc=request.form.get("mc")))


@app.post("/admin/card_logs/delete")
@admin_required
def admin_card_log_delete():
    """删除流水并回补 1 次（二级需一级验证码）"""
    if get_admin()["role"] == "staff" and not check_vcode(request.form.get("vcode")):
        flash("删除流水需一级管理员验证码")
        return redirect(url_for("admin_card_logs", mc=request.form.get("mc")))
    db = get_db()
    l = db.execute("SELECT * FROM card_logs WHERE id=?", (request.form.get("id"),)).fetchone()
    if l:
        db.execute("DELETE FROM card_logs WHERE id=?", (l["id"],))
        db.execute("UPDATE member_cards SET times_left=times_left+1 WHERE id=?",
                   (l["member_card_id"],))
        log_op("删除核销流水", request.form.get("id"))
        db.commit()
        flash("流水已删除，次数已回补 1 次")
    return redirect(url_for("admin_card_logs", mc=request.form.get("mc")))


@app.post("/admin/card_buyers/update")
@admin_required
def admin_card_buyer_update():
    """修改购买记录：剩余次数 / 状态（二级需一级验证码）"""
    if get_admin()["role"] == "staff" and not check_vcode(request.form.get("vcode")):
        flash("调整会员卡次数需一级管理员验证码")
        return redirect(url_for("admin_card_buyers", card=request.form.get("card")))
    db = get_db()
    o = db.execute("SELECT * FROM member_cards WHERE id=?", (request.form.get("id"),)).fetchone()
    if o:
        status = request.form.get("status")
        if status not in ("pending", "active", "cancelled"):
            status = o["status"]
        db.execute("UPDATE member_cards SET times_left=?, status=? WHERE id=?",
                   (max(0, request.form.get("times_left", type=int) or 0), status, o["id"]))
        log_op("调整会员卡次数", f"卡记录{o['id']}")
        db.commit()
        flash("购买记录已修改")
    return redirect(url_for("admin_card_buyers", card=request.form.get("card")))


@app.post("/admin/card_buyers/pay")
@admin_required
def admin_card_buyer_pay():
    """补收会员卡欠款（多次支付场景）"""
    db = get_db()
    o = db.execute("SELECT mc.*, c.price FROM member_cards mc JOIN cards c ON c.id=mc.card_id "
                   "WHERE mc.id=?", (request.form.get("id"),)).fetchone()
    if o:
        amt = min(max(0.01, request.form.get("amount", type=float) or 0), o["price"] - o["paid"])
        if amt > 0:
            db.execute("UPDATE member_cards SET paid=paid+? WHERE id=?", (amt, o["id"]))
            log_op("补收卡款",
                   f"卡记录{o['id']} +¥{amt:g}（{request.form.get('pay_method', '现金')}）")
            db.commit()
            flash(f"已补收 ¥{amt:g}")
        else:
            flash("补款金额无效或已付清")
    return redirect(url_for("admin_card_buyers", card=request.form.get("card")))


@app.post("/admin/cards/delete")
@admin_required
@super_required
def admin_card_delete():
    db = get_db()
    db.execute("DELETE FROM cards WHERE id=?", (request.form.get("id"),))
    log_op("删除卡档", request.form.get("id"))
    db.commit()
    flash("卡已删除（已购会员卡不受影响）")
    return redirect(url_for("admin_cards"))


@app.get("/admin/member_cards")
@admin_required
def admin_member_cards():
    """生效会员卡：核销扣次"""
    q = request.args.get("q", "").strip()
    rows = get_db().execute(
        "SELECT mc.*, m.phone, c.name cname, mc.created_at AS issued_at FROM member_cards mc "
        "JOIN members m ON m.id=mc.member_id JOIN cards c ON c.id=mc.card_id "
        "WHERE mc.status='active' AND (?='' OR m.phone LIKE '%'||?||'%') "
        "ORDER BY mc.id DESC", (q, q)).fetchall()
    return render_template("admin/member_cards.html", cards=rows, q=q)


@app.get("/admin/members")
@admin_required
def admin_members():
    db = get_db()
    card = request.args.get("card", type=int)
    q = request.args.get("q", "").strip()
    rows = db.execute(
        "SELECT m.* FROM members m WHERE "
        "(? IS NULL OR m.id IN (SELECT member_id FROM member_cards WHERE card_id=? "
        "AND status='active') OR (?=-1 AND m.id NOT IN "
        "(SELECT member_id FROM member_cards WHERE status='active'))) "
        "AND (?='' OR m.phone LIKE '%'||?||'%') ORDER BY m.id",
        (card, card, card, q, q)).fetchall()
    # 每个会员买的卡（卡名+剩余次数），按类别组装
    cards_info = {}
    for r in db.execute(
            "SELECT mc.member_id, c.name, mc.times_left, mc.status FROM member_cards mc "
            "JOIN cards c ON c.id=mc.card_id"):
        cards_info.setdefault(r["member_id"], []).append(
            f"{r['name']}(剩{r['times_left']}次{'·待确认' if r['status'] == 'pending' else ''})")
    cards = db.execute("SELECT * FROM cards ORDER BY id").fetchall()
    return render_template("admin/members.html", members=rows,
                           cards_info=cards_info, cards=cards, card=card, q=q)


@app.post("/admin/members/add")
@admin_required
def admin_member_add():
    """后台新增会员档案"""
    f = request.form
    phone, name = f.get("phone", "").strip(), f.get("name", "").strip()
    if not PHONE_RE.match(phone):
        flash("手机号格式不正确")
    elif get_db().execute("SELECT 1 FROM members WHERE phone=?", (phone,)).fetchone():
        flash("该手机号已是会员")
    else:
        db = get_db()
        db.execute("INSERT INTO members (phone, name, gender, birthday, created_at) "
                   "VALUES (?,?,?,?,?)",
                   (phone, name, f.get("gender", ""), f.get("birthday", ""),
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        log_op("新增会员", f"{name} {phone}")
        db.commit()
        flash("会员已新增")
    return redirect(url_for("admin_members"))


@app.post("/admin/members/update")
@admin_required
def admin_member_update():
    db = get_db()
    m = db.execute("SELECT * FROM members WHERE id=?", (request.form.get("id"),)).fetchone()
    if m:
        db.execute("UPDATE members SET name=?, gender=?, birthday=? WHERE id=?",
                   (request.form.get("name", "").strip(),
                    request.form.get("gender", ""),
                    request.form.get("birthday", ""), m["id"]))
        db.commit()
        flash("会员信息已修改")
    return redirect(url_for("admin_members"))


@app.post("/admin/members/delete")
@admin_required
def admin_member_delete():
    """删除会员及其会员卡、核销流水（二级需一级验证码）"""
    if get_admin()["role"] == "staff" and not check_vcode(request.form.get("vcode")):
        flash("删除会员需一级管理员验证码")
        return redirect(url_for("admin_members"))
    db = get_db()
    m = db.execute("SELECT * FROM members WHERE id=?", (request.form.get("id"),)).fetchone()
    if m:
        db.execute("DELETE FROM card_logs WHERE member_card_id IN "
                   "(SELECT id FROM member_cards WHERE member_id=?)", (m["id"],))
        db.execute("DELETE FROM member_cards WHERE member_id=?", (m["id"],))
        db.execute("DELETE FROM members WHERE id=?", (m["id"],))
        log_op("删除会员", m["phone"])
        db.commit()
        flash(f"会员 {m['phone']} 及其会员卡数据已删除")
    return redirect(url_for("admin_members"))


@app.route("/admin/config", methods=["GET", "POST"])
@admin_required
@super_required
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


@app.post("/admin/api/send_vcode")
@admin_required
def admin_send_vcode():
    """发送敏感操作验证码到一级管理员手机（开发模式返回 dev_code）"""
    adm = get_admin()
    if adm and adm["role"] == "super":
        return jsonify(ok=True, msg="一级操作无需验证码")
    phone = get_db().execute("SELECT phone FROM admins WHERE role='super' "
                             "AND phone!='' LIMIT 1").fetchone()
    if not phone:
        return jsonify(ok=False, msg="一级管理员未绑定手机号，请联系一级管理员")
    phone = phone[0]
    db = get_db()
    row = db.execute("SELECT code, expires_at FROM sms_codes WHERE phone=? "
                     "ORDER BY expires_at DESC LIMIT 1", (phone,)).fetchone()
    if row and row["expires_at"] - time.time() > 240:
        code = row["code"]
    else:
        code = f"{random.randint(0, 999999):06d}"
        db.execute("DELETE FROM sms_codes WHERE phone=?", (phone,))
        db.execute("INSERT INTO sms_codes (phone, code, expires_at) VALUES (?,?,?)",
                   (phone, code, time.time() + 300))
        db.commit()
        send_sms(phone, code)
    return jsonify(ok=True, dev_code=code)


# ---- 账号管理（一级） ----

@app.route("/admin/accounts", methods=["GET", "POST"])
@admin_required
@super_required
def admin_accounts():
    db = get_db()
    if request.method == "POST":
        f = request.form
        if f.get("action") == "add":
            username, pw = f.get("username", "").strip(), f.get("password", "").strip()
            if len(username) < 2 or len(pw) < 6:
                flash("账号至少 2 位、密码至少 6 位")
            elif db.execute("SELECT 1 FROM admins WHERE username=?", (username,)).fetchone():
                flash("账号已存在")
            else:
                db.execute("INSERT INTO admins (username, pass, role) VALUES (?,?,?)",
                           (username, hash_pass(pw), "staff"))
                log_op("创建二级账号", username)
                db.commit()
                flash(f"二级账号 {username} 已创建，密码 {pw}")
        elif f.get("action") == "toggle":
            a = db.execute("SELECT * FROM admins WHERE id=?", (f.get("id"),)).fetchone()
            if a and a["role"] == "staff":
                db.execute("UPDATE admins SET active=? WHERE id=?", (0 if a["active"] else 1, a["id"]))
                log_op("启用/禁用二级账号", a["username"])
                db.commit()
                flash(f"账号 {a['username']} 已{'禁用' if a['active'] else '启用'}")
        elif f.get("action") == "delete":
            a = db.execute("SELECT * FROM admins WHERE id=?", (f.get("id"),)).fetchone()
            if a and a["role"] == "staff":
                db.execute("DELETE FROM admins WHERE id=?", (a["id"],))
                log_op("删除二级账号", a["username"])
                db.commit()
                flash(f"账号 {a['username']} 已删除")
        elif f.get("action") == "bind_phone":
            phone = f.get("phone", "").strip()
            if PHONE_RE.match(phone):
                db.execute("UPDATE admins SET phone=? WHERE role='super'", (phone,))
                log_op("绑定验证码手机", phone)
                db.commit()
                flash("敏感操作验证码接收手机已绑定")
            else:
                flash("手机号格式不正确")
        return redirect(url_for("admin_accounts"))
    accounts = db.execute("SELECT * FROM admins ORDER BY role DESC, id").fetchall()
    return render_template("admin/accounts.html", accounts=accounts)


@app.route("/admin/change_pw", methods=["GET", "POST"])
@admin_required
def admin_change_pw():
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
        log_op("修改密码", adm["username"])
        db.commit()
        flash("密码已修改")
    return redirect(url_for("admin_accounts")
                    if adm["role"] == "super" else url_for("admin_index"))


# ---- 收银下单 ----

@app.route("/admin/pos", methods=["GET", "POST"])
@admin_required
def admin_pos():
    db = get_db()
    if request.method == "POST":
        f = request.form
        member_id = f.get("member_id", type=int) or None
        svc_ids = [int(s) for s in f.getlist("services")
                   if db.execute("SELECT 1 FROM services WHERE id=?", (s,)).fetchone()]
        if not svc_ids:
            flash("请至少勾选一个项目")
            return redirect(url_for("admin_pos"))
        total = sum(db.execute("SELECT price FROM services WHERE id=?",
                               (s,)).fetchone()[0] for s in svc_ids)
        discount = min(float(f.get("discount") or 0), total)
        pay_method = f.get("pay_method", "现金")
        member_card = f.get("member_card", type=int) or None
        adm = get_admin()
        if adm["role"] == "staff" and discount > float(get_settings().get("discount_limit") or 0) \
                and not check_vcode(f.get("vcode")):
            flash("减免金额超过阈值，需一级管理员验证码")
            return redirect(url_for("admin_pos"))
        if pay_method == "划卡" and not member_card:
            flash("划卡支付需选择会员卡")
            return redirect(url_for("admin_pos"))
        items = []
        for s in svc_ids:
            row = db.execute("SELECT name, price, deduct_times, commission FROM services WHERE id=?",
                             (s,)).fetchone()
            tech = db.execute("SELECT name FROM technicians WHERE id=?",
                              (f.get(f"tech_{s}") or None,)).fetchone()
            items.append({"name": row["name"], "price": row["price"],
                          "tech": tech["name"] if tech else "不指定",
                          "deduct_times": row["deduct_times"],
                          "commission": row["commission"] or 0})
        oid = db.execute("INSERT INTO orders (order_no, member_id, member_card_id, "
                         "items_json, total, discount, pay_method, status, operator_id, created_at) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (datetime.now().strftime("%Y%m%d%H%M%S") + str(random.randint(10, 99)),
                          member_id, member_card, json.dumps(items, ensure_ascii=False),
                          total, discount, pay_method, "unpaid", session["aid"],
                          datetime.now().strftime("%Y-%m-%d %H:%M:%S"))).lastrowid
        db.commit()
        return redirect(url_for("admin_pay", oid=oid))
    services = db.execute("SELECT * FROM services WHERE active=1 ORDER BY id").fetchall()
    technicians = db.execute("SELECT * FROM technicians WHERE active=1 ORDER BY id").fetchall()
    return render_template("admin/pos.html", services=services, technicians=technicians)


@app.get("/admin/pay/<int:oid>")
@admin_required
def admin_pay(oid):
    """付款展示页：大字金额出示给客户"""
    o = get_db().execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o or o["status"] != "unpaid":
        return redirect(url_for("admin_orders"))
    member = None
    if o["member_id"]:
        member = get_db().execute("SELECT * FROM members WHERE id=?", (o["member_id"],)).fetchone()
    return render_template("admin/pay.html", o=o,
                           items=json.loads(o["items_json"]), member=member)


@app.post("/admin/pay/confirm")
@admin_required
def admin_pay_confirm():
    db = get_db()
    o = db.execute("SELECT * FROM orders WHERE id=? AND status='unpaid'",
                   (request.form.get("oid"),)).fetchone()
    if o:
        db.execute("UPDATE orders SET status='paid' WHERE id=?", (o["id"],))
        if o["pay_method"] == "划卡" and o["member_card_id"]:
            mc = db.execute("SELECT * FROM member_cards WHERE id=? AND status='active'",
                            (o["member_card_id"],)).fetchone()
            cost = sum(it.get("deduct_times", 1) for it in json.loads(o["items_json"]))
            if mc and mc["times_left"] >= cost:
                db.execute("UPDATE member_cards SET times_left=times_left-? WHERE id=?", (cost, mc["id"]))
                now = datetime.now().strftime("%Y-%m-%d %H:%M")
                db.executemany("INSERT INTO card_logs (member_card_id, service_id, "
                               "technician_id, created_at) VALUES (?,?,?,?)",
                               [(mc["id"], None, None, now)] * cost)
        log_op("收款完成", f"{o['order_no']} ¥{o['total'] - o['discount']:g}")
        db.commit()
        flash("收款完成")
    return redirect(url_for("admin_orders"))


@app.get("/admin/orders")
@admin_required
def admin_orders():
    st = request.args.get("status", "today")
    db = get_db()
    q = "SELECT o.*, m.phone, a.username opname FROM orders o " \
        "LEFT JOIN members m ON m.id=o.member_id LEFT JOIN admins a ON a.id=o.operator_id "
    args = []
    if st == "today":
        q += " WHERE o.created_at LIKE ?"
        args.append(datetime.now().strftime("%Y-%m-%d") + "%")
    elif st != "all":
        q += " WHERE o.status=?"
        args.append(st)
    rows = db.execute(q + " ORDER BY o.id DESC", args).fetchall()
    return render_template("admin/orders.html", orders=rows, st=st)


@app.post("/admin/orders/refund")
@admin_required
def admin_order_refund():
    adm = get_admin()
    if adm["role"] == "staff" and not check_vcode(request.form.get("vcode")):
        flash("退款需一级管理员验证码")
        return redirect(url_for("admin_orders"))
    db = get_db()
    o = db.execute("SELECT * FROM orders WHERE id=? AND status='paid'",
                   (request.form.get("id"),)).fetchone()
    if o:
        db.execute("UPDATE orders SET status='refunded' WHERE id=?", (o["id"],))
        if o["member_card_id"]:  # 划卡订单退款回补次数
            db.execute("UPDATE member_cards SET times_left=times_left+? WHERE id=?",
                       (len(json.loads(o["items_json"])), o["member_card_id"]))
        log_op("退款", f"{o['order_no']} ¥{o['total'] - o['discount']:g}")
        db.commit()
        flash("订单已退款")
    return redirect(url_for("admin_orders"))


@app.post("/admin/orders/void")
@admin_required
@super_required
def admin_order_void():
    db = get_db()
    o = db.execute("SELECT * FROM orders WHERE id=? AND status='unpaid'",
                   (request.form.get("id"),)).fetchone()
    if o:
        db.execute("UPDATE orders SET status='void' WHERE id=?", (o["id"],))
        log_op("作废订单", o["order_no"])
        db.commit()
        flash("订单已作废")
    return redirect(url_for("admin_orders"))


@app.get("/admin/shift")
@admin_required
def admin_shift():
    """交接班：今日营收汇总"""
    db = get_db()
    today = datetime.now().strftime("%Y-%m-%d") + "%"
    rows = db.execute("SELECT pay_method, COUNT(*) cnt, SUM(total-discount) amt FROM orders "
                      "WHERE status='paid' AND created_at LIKE ? "
                      "GROUP BY pay_method", (today,)).fetchall()
    total = sum(r["amt"] for r in rows)
    cnt = sum(r["cnt"] for r in rows)
    return render_template("admin/shift.html", rows=rows, total=total, cnt=cnt)


# ---- 技师业绩（提成参考） ----

@app.get("/admin/tech_stats")
@admin_required
def admin_tech_stats():
    """技师开了几单：订单中每个技师的单数与业绩金额"""
    days = request.args.get("days", "today")
    db = get_db()
    q = "SELECT items_json FROM orders WHERE status='paid'"
    args = []
    if days == "today":
        q += " AND created_at LIKE ?"
        args.append(datetime.now().strftime("%Y-%m-%d") + "%")
    stats = {}  # 技师名 -> {"cnt": 单数, "amt": 业绩, "cm": 提成}
    for (ij,) in db.execute(q, args):
        for it in json.loads(ij):
            t = it.get("tech", "不指定")
            stats.setdefault(t, {"cnt": 0, "amt": 0.0, "cm": 0.0})
            stats[t]["cnt"] += 1
            stats[t]["amt"] += it["price"]
            stats[t]["cm"] += it.get("commission", 0)
    rows = sorted(stats.items(), key=lambda kv: -kv[1]["amt"])
    return render_template("admin/tech_stats.html", rows=rows, days=days)


# ---- 后台售卡开卡 ----

@app.route("/admin/card_sell", methods=["GET", "POST"])
@admin_required
def admin_card_sell():
    db = get_db()
    if request.method == "POST":
        f = request.form
        member_id = f.get("member_id", type=int)
        card_id = f.get("card_id", type=int)
        c = db.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
        if not member_id and f.get("phone"):
            # 新客户：直接建档再开卡
            phone, name = f.get("phone", "").strip(), f.get("name", "").strip()
            if not PHONE_RE.match(phone):
                flash("手机号格式不正确")
                return redirect(url_for("admin_card_sell"))
            if db.execute("SELECT 1 FROM members WHERE phone=?", (phone,)).fetchone():
                flash("该手机号已是会员，请在搜索框选择")
                return redirect(url_for("admin_card_sell"))
            member_id = db.execute("INSERT INTO members (phone, name, created_at) "
                                   "VALUES (?,?,?)",
                                   (phone, name,
                                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"))).lastrowid
            log_op("新增会员", f"{name} {phone}")
        m = db.execute("SELECT * FROM members WHERE id=?", (member_id,)).fetchone()
        if m and c:
            amount = f.get("amount", type=float)
            amount = c["price"] if amount is None else min(max(0.0, amount), c["price"])
            db.execute("INSERT INTO member_cards (member_id, card_id, times_left, "
                       "status, created_at, paid) VALUES (?,?,?, 'active', ?, ?)",
                       (member_id, card_id, c["times"],
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"), amount))
            log_op("售卡开卡",
                   f"{m['phone']} {c['name']} 实收 ¥{amount:g}/{c['price']:g}（{f.get('pay_method', '现金')}）")
            db.commit()
            flash(f"已为 {m['phone']} 开卡「{c['name']}」（{c['times']} 次，售价 ¥{c['price']:g}，"
                  f"本次实收 ¥{amount:g}）")
        else:
            flash("会员或卡不存在")
        return redirect(url_for("admin_card_sell"))
    cards = db.execute("SELECT * FROM cards WHERE active=1 ORDER BY id").fetchall()
    return render_template("admin/card_sell.html", cards=cards)


# ---- 操作日志（一级） ----

@app.get("/admin/oplogs")
@admin_required
@super_required
def admin_oplogs():
    rows = get_db().execute(
        "SELECT l.*, a.username FROM op_logs l LEFT JOIN admins a ON a.id=l.admin_id "
        "ORDER BY l.id DESC LIMIT 200").fetchall()
    return render_template("admin/oplogs.html", logs=rows)


# ---------- 启动 ----------

# 模块加载即初始化数据库（不依赖启动方式），重复执行安全
init_db(DB_PATH)
_db = sqlite3.connect(DB_PATH)
_pw = seed(_db)
_db.close()
if _pw:
    print(f"[初始账号] admin / {_pw}  （仅首次启动显示，请妥善保管）")


def selftest():
    """核心流程自检：两级权限、收银下单、敏感操作验证码、日志留痕"""
    global DB_PATH
    DB_PATH = Path(tempfile.gettempdir()) / "ysg_selftest.db"
    if DB_PATH.exists():
        DB_PATH.unlink()
    init_db(DB_PATH)
    db = sqlite3.connect(DB_PATH)
    pw1 = seed(db)
    db.close()
    assert pw1, "管理员未创建"

    c = app.test_client()
    with c.session_transaction() as s:
        s["csrf"] = "selftest"

    # 根路径重定向到后台登录
    assert c.get("/").headers["Location"].endswith("/admin/login")
    # 一级登录、绑定验证码手机、创建二级账号
    assert c.post("/admin/login", data={"username": "admin", "password": pw1,
                                        "_csrf": "selftest"}).status_code == 302
    c.post("/admin/accounts", data={"action": "bind_phone", "phone": "13811112222",
                                    "_csrf": "selftest"})
    c.post("/admin/accounts", data={"action": "add", "username": "staff1",
                                    "password": "staff123", "_csrf": "selftest"})
    db = sqlite3.connect(DB_PATH)
    assert db.execute("SELECT role FROM admins WHERE username='staff1'").fetchone()[0] == "staff"
    db.close()
    c.get("/admin/logout")
    # 新增会员 + 售卡开卡
    assert c.post("/admin/login", data={"username": "admin", "password": pw1,
                                        "_csrf": "selftest"}).status_code == 302
    c.post("/admin/members/add", data={"name": "测试会员", "phone": "13900001111",
                                       "gender": "男", "_csrf": "selftest"})
    db = sqlite3.connect(DB_PATH)
    mid = db.execute("SELECT id FROM members WHERE phone='13900001111'").fetchone()[0]
    db.close()
    c.post("/admin/card_sell", data={"member_id": str(mid), "card_id": "1",
                                     "amount": "500", "_csrf": "selftest"})
    db = sqlite3.connect(DB_PATH)
    mcid = db.execute("SELECT id FROM member_cards WHERE member_id=?", (mid,)).fetchone()[0]
    assert db.execute("SELECT times_left FROM member_cards WHERE id=?", (mcid,)).fetchone()[0] == 10
    assert db.execute("SELECT paid FROM member_cards WHERE id=?", (mcid,)).fetchone()[0] == 500
    db.close()
    # 补收欠款（多次支付）
    c.post("/admin/card_buyers/pay", data={"id": str(mcid), "amount": "480",
                                           "card": "1", "_csrf": "selftest"})
    db = sqlite3.connect(DB_PATH)
    assert db.execute("SELECT paid FROM member_cards WHERE id=?", (mcid,)).fetchone()[0] == 980
    db.close()
    # 二级登录
    c.get("/admin/logout")
    assert c.post("/admin/login", data={"username": "staff1", "password": "staff123",
                                        "_csrf": "selftest"}).status_code == 302
    # 二级访问一级页面被拒
    r = c.get("/admin/cards", follow_redirects=True)
    assert "仅限一级管理员" in r.get_data(as_text=True)
    # 收银下单（现金，2 个项目）
    from werkzeug.datastructures import MultiDict
    db = sqlite3.connect(DB_PATH)
    svcs = [str(r[0]) for r in db.execute("SELECT id FROM services LIMIT 2")]
    # 预置项目默认 0 元，自检里先定价再走收银
    db.execute("UPDATE services SET price=128, commission=20 WHERE id=?", (svcs[0],))
    db.execute("UPDATE services SET price=88 WHERE id=?", (svcs[1],))
    db.commit()
    db.close()
    c.post("/admin/pos", data=MultiDict([
        ("_csrf", "selftest"), ("member_id", str(mid)), ("services", svcs[0]),
        ("services", svcs[1]), (f"tech_{svcs[0]}", "1"), ("discount", "0"),
        ("pay_method", "现金")]))
    db = sqlite3.connect(DB_PATH)
    oid = db.execute("SELECT id FROM orders WHERE status='unpaid'").fetchone()[0]
    assert db.execute("SELECT total FROM orders WHERE id=?", (oid,)).fetchone()[0] == 216
    db.close()
    # 付款展示页 + 确认收款
    assert "请客户付款" in c.get(f"/admin/pay/{oid}").get_data(as_text=True)
    c.post("/admin/pay/confirm", data={"oid": str(oid), "_csrf": "selftest"})
    db = sqlite3.connect(DB_PATH)
    assert db.execute("SELECT status FROM orders WHERE id=?", (oid,)).fetchone()[0] == "paid"
    db.close()
    # 划卡下单：扣会员卡次数并留流水
    c.post("/admin/pos", data=MultiDict([
        ("_csrf", "selftest"), ("member_id", str(mid)), ("services", svcs[0]),
        ("discount", "0"), ("pay_method", "划卡"), ("member_card", str(mcid))]))
    db = sqlite3.connect(DB_PATH)
    oid2 = db.execute("SELECT id FROM orders WHERE pay_method='划卡' AND status='unpaid'").fetchone()[0]
    db.close()
    c.post("/admin/pay/confirm", data={"oid": str(oid2), "_csrf": "selftest"})
    db = sqlite3.connect(DB_PATH)
    assert db.execute("SELECT times_left FROM member_cards WHERE id=?", (mcid,)).fetchone()[0] == 9
    db.close()
    # 二级删除会员：无验证码被拒，有验证码通过
    assert "验证码" in c.post("/admin/members/delete", data={"id": str(mid), "_csrf": "selftest"},
                              follow_redirects=True).get_data(as_text=True)
    r = c.post("/admin/api/send_vcode", data={"_csrf": "selftest"}).get_json()
    assert r["ok"] and r["dev_code"]
    c.post("/admin/members/delete", data={"id": str(mid), "vcode": r["dev_code"],
                                          "_csrf": "selftest"})
    db = sqlite3.connect(DB_PATH)
    assert db.execute("SELECT COUNT(*) FROM members WHERE id=?", (mid,)).fetchone()[0] == 0
    db.close()
    # 二级改价超阈值被拒（阈值 50，减 100）
    db = sqlite3.connect(DB_PATH)
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('discount_limit', '50')")
    db.commit()
    db.close()
    assert "需一级管理员验证码" in c.post("/admin/pos", data=MultiDict([
        ("_csrf", "selftest"), ("services", svcs[0]), ("discount", "100"),
        ("pay_method", "现金")]), follow_redirects=True).get_data(as_text=True)
    # 二级退款：无验证码被拒
    db = sqlite3.connect(DB_PATH)
    db.execute("UPDATE orders SET status='paid' WHERE id=?", (oid,))
    db.commit()
    db.close()
    assert "退款需一级管理员验证码" in c.post("/admin/orders/refund",
        data={"id": str(oid), "_csrf": "selftest"}, follow_redirects=True).get_data(as_text=True)
    # 一级直接退款成功
    c.get("/admin/logout")
    c.post("/admin/login", data={"username": "admin", "password": pw1, "_csrf": "selftest"})
    c.post("/admin/orders/refund", data={"id": str(oid), "_csrf": "selftest"})
    db = sqlite3.connect(DB_PATH)
    assert db.execute("SELECT status FROM orders WHERE id=?", (oid,)).fetchone()[0] == "refunded"
    # 操作日志留痕
    actions = {r[0] for r in db.execute("SELECT action FROM op_logs")}
    assert {"登录", "创建二级账号", "售卡开卡", "补收卡款", "收款完成", "退款", "删除会员"} <= actions, actions
    # 交接班有今日营收
    assert "总营收" in c.get("/admin/shift").get_data(as_text=True)
    # 技师提成统计（划卡单 svcs[0] 提成 20）
    r = c.get("/admin/tech_stats").get_data(as_text=True)
    assert "¥20" in r
    db.close()
    DB_PATH.unlink()
    print("selftest OK: 两级权限、收银下单、划卡扣次、敏感操作验证码、退款、日志留痕全部通过")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
        sys.exit()
    print("养生馆收银后台已启动: http://127.0.0.1:5000  (后台 /admin/login)")
    app.run(host="0.0.0.0", port=5000, debug=True)  # 0.0.0.0: 局域网内其他设备可访问
