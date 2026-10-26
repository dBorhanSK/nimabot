import os, json, sqlite3, logging, requests
from flask import Flask, request, jsonify

TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "change-this-secret")
OWNER_ID = int(os.getenv("OWNER_ID", "6914909647"))
DB_PATH = os.getenv("DB_PATH", "bot.db")
API = f"https://api.telegram.org/bot{TOKEN}"
logging.basicConfig(level=logging.INFO)
app = Flask(__name__)

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
          tg_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, lang TEXT DEFAULT 'fa',
          phone TEXT, role TEXT DEFAULT 'customer', joined_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS plans(
          id INTEGER PRIMARY KEY AUTOINCREMENT, name_fa TEXT NOT NULL, name_en TEXT NOT NULL,
          volume_gb INTEGER NOT NULL, days INTEGER NOT NULL, price INTEGER NOT NULL, active INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS configs(
          id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER NOT NULL, config TEXT NOT NULL,
          status TEXT DEFAULT 'available', order_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS orders(
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, plan_id INTEGER NOT NULL,
          status TEXT DEFAULT 'awaiting_receipt', receipt_file_id TEXT, config_id INTEGER,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS sellers(tg_id INTEGER PRIMARY KEY, approved INTEGER DEFAULT 0);
        """)
        c.execute("INSERT OR IGNORE INTO users(tg_id, role, lang) VALUES(?, 'owner', 'fa')", (OWNER_ID,))
        c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('card_number','تنظیم نشده')")
        c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('support_id','تنظیم نشده')")
        c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('force_channels','[]')")
        c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('welcome_fa','به فروشگاه کانفیگ V2Ray خوش آمدید.')")
        c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('welcome_en','Welcome to the V2Ray configuration store.')")

def setting(key, default=""):
    with db() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default

def set_setting(key, value):
    with db() as c:
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

def tg(method, payload=None):
    if not TOKEN:
        logging.error("BOT_TOKEN is missing")
        return {}
    try:
        r = requests.post(f"{API}/{method}", json=payload or {}, timeout=20)
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            logging.warning("Telegram API error: %s", data)
        return data
    except Exception:
        logging.exception("Telegram API request failed")
        return {}

def send(chat_id, text, keyboard=None, **kwargs):
    p = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if keyboard: p["reply_markup"] = keyboard
    p.update(kwargs)
    return tg("sendMessage", p)

def inline(rows):
    return {"inline_keyboard": rows}

def is_admin(uid):
    with db() as c:
        row = c.execute("SELECT role FROM users WHERE tg_id=?", (uid,)).fetchone()
    return bool(row and row["role"] in ("owner", "admin"))

def lang_of(uid):
    with db() as c:
        row = c.execute("SELECT lang FROM users WHERE tg_id=?", (uid,)).fetchone()
    return row["lang"] if row else "fa"

def main_menu(uid):
    fa = lang_of(uid) == "fa"
    rows = [
      [{"text": "🛍 خرید سرویس" if fa else "🛍 Buy service", "callback_data": "plans"}],
      [{"text": "👤 حساب کاربری" if fa else "👤 My account", "callback_data": "account"},
       {"text": "🆘 پشتیبانی" if fa else "🆘 Support", "callback_data": "support"}],
      [{"text": "🌐 English" if fa else "🌐 فارسی", "callback_data": "lang_toggle"}]
    ]
    if is_admin(uid):
        rows.append([{"text": "⚙️ پنل مدیریت" if fa else "⚙️ Admin panel", "callback_data": "admin"}])
    return inline(rows)

def upsert_user(user):
    uid = user["id"]
    with db() as c:
        c.execute("""INSERT INTO users(tg_id,username,first_name) VALUES(?,?,?)
        ON CONFLICT(tg_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name""",
        (uid, user.get("username"), user.get("first_name")))

def force_join_ok(uid):
    try:
        channels = json.loads(setting("force_channels", "[]"))
    except Exception:
        channels = []
    for ch in channels:
        result = tg("getChatMember", {"chat_id": ch, "user_id": uid})
        member = (result.get("result") or {}).get("status")
        if not result.get("ok") or member not in ("creator", "administrator", "member"):
            return False
    return True

def prompt_join(chat_id):
    try: channels = json.loads(setting("force_channels", "[]"))
    except Exception: channels = []
    rows = []
    for ch in channels:
        link = ch if str(ch).startswith("https://t.me/") else "https://t.me/" + str(ch).lstrip("@")
        rows.append([{"text": "عضویت در کانال / Join channel", "url": link}])
    rows.append([{"text": "✅ بررسی عضویت / Check membership", "callback_data": "check_join"}])
    send(chat_id, "برای استفاده از ربات ابتدا در کانال(های) زیر عضو شوید.\nJoin the required channel(s) first.", inline(rows))

def start(chat_id, user):
    upsert_user(user)
    if not force_join_ok(user["id"]):
        prompt_join(chat_id); return
    fa = lang_of(user["id"]) == "fa"
    welcome = setting("welcome_fa" if fa else "welcome_en")
    send(chat_id, welcome, main_menu(user["id"]))

def show_plans(chat_id, uid):
    fa = lang_of(uid) == "fa"
    with db() as c:
        plans = c.execute("SELECT * FROM plans WHERE active=1 ORDER BY id").fetchall()
    if not plans:
        send(chat_id, "فعلاً سرویسی ثبت نشده است." if fa else "No services are available yet.", main_menu(uid)); return
    for p in plans:
        name = p["name_fa"] if fa else p["name_en"]
        txt = (f"📦 <b>{name}</b>\nحجم: {p['volume_gb']} GB | مدت: {p['days']} روز\nقیمت: {p['price']:,} تومان"
               if fa else f"📦 <b>{name}</b>\nVolume: {p['volume_gb']} GB | Duration: {p['days']} days\nPrice: {p['price']:,} toman")
        send(chat_id, txt, inline([[{"text": "انتخاب و خرید" if fa else "Select & buy", "callback_data": f"buy:{p['id']}"}]]))

def account(chat_id, uid):
    fa = lang_of(uid) == "fa"
    with db() as c:
        user = c.execute("SELECT * FROM users WHERE tg_id=?", (uid,)).fetchone()
        orders = c.execute("""SELECT o.id,o.status,p.name_fa,p.name_en,p.volume_gb,p.days
          FROM orders o JOIN plans p ON p.id=o.plan_id WHERE o.user_id=? ORDER BY o.id DESC LIMIT 10""", (uid,)).fetchall()
    text = ("👤 <b>حساب کاربری</b>\n" if fa else "👤 <b>My account</b>\n")
    text += f"ID: <code>{uid}</code>\n"
    text += (f"نقش: {user['role']}\n" if fa else f"Role: {user['role']}\n")
    text += ("📄 سفارش‌ها:\n" if fa else "📄 Orders:\n")
    if not orders: text += ("هنوز سفارشی ندارید." if fa else "No orders yet.")
    for o in orders:
        nm = o["name_fa"] if fa else o["name_en"]
        text += f"\n#{o['id']} — {nm} — {o['status']}"
    send(chat_id, text, main_menu(uid))

def admin_menu(chat_id):
    send(chat_id, "⚙️ <b>پنل مدیریت</b>\nفرمان‌ها را در چت ارسال کنید:\n"
      "/setcard شماره_کارت\n/setchannels @channel1,@channel2\n/set support شناسه_پشتیبانی\n"
      "/addplan نام_فارسی|English name|حجم_GB|روز|قیمت\n/addconfig شناسه_پلن سپس متن کانفیگ را در پیام بعدی\n"
      "/stats\n/addadmin user_id\n/approve_seller user_id\n\n"
      "برای تعریف کانفیگ، ابتدا /addconfig PLAN_ID بفرستید و در پیام بعد کانفیگ را ارسال کنید.",
      inline([[{"text":"📊 آمار","callback_data":"stats"}],[{"text":"🏠 منو","callback_data":"home"}]]))

pending_config = {}

def handle_message(m):
    chat_id = m["chat"]["id"]
    user = m.get("from", {})
    uid = user.get("id")
    if not uid: return
    upsert_user(user)
    text = m.get("text", "")
    if text == "/start":
        start(chat_id, user); return
    if not force_join_ok(uid):
        prompt_join(chat_id); return
    if text == "/admin" and is_admin(uid):
        admin_menu(chat_id); return
    if uid in pending_config:
        plan_id = pending_config.pop(uid)
        config_text = text.strip()
        with db() as c:
            plan = c.execute("SELECT id FROM plans WHERE id=?", (plan_id,)).fetchone()
            if plan and config_text:
                c.execute("INSERT INTO configs(plan_id,config) VALUES(?,?)", (plan_id, config_text))
                send(chat_id, f"✅ کانفیگ برای پلن {plan_id} ثبت شد.")
            else:
                send(chat_id, "پلن پیدا نشد یا کانفیگ خالی بود.")
        return
    if text.startswith("/setcard ") and is_admin(uid):
        set_setting("card_number", text.split(" ",1)[1].strip()); send(chat_id, "شماره کارت ذخیره شد."); return
    if text.startswith("/setchannels ") and is_admin(uid):
        vals = [x.strip() for x in text.split(" ",1)[1].split(",") if x.strip()]
        set_setting("force_channels", json.dumps(vals, ensure_ascii=False))
        send(chat_id, "کانال‌های عضویت اجباری ذخیره شدند. ربات باید در کانال‌ها دسترسی لازم برای بررسی عضویت داشته باشد."); return
    if text.startswith("/set support ") and is_admin(uid):
        set_setting("support_id", text.split(" ",2)[2].strip()); send(chat_id, "شناسه پشتیبانی ذخیره شد."); return
    if text.startswith("/addplan ") and is_admin(uid):
        try:
            fa,en,gb,days,price = [x.strip() for x in text.split(" ",1)[1].split("|")]
            with db() as c: c.execute("INSERT INTO plans(name_fa,name_en,volume_gb,days,price) VALUES(?,?,?,?,?)",(fa,en,int(gb),int(days),int(price)))
            send(chat_id, "تعرفه با موفقیت اضافه شد.")
        except Exception: send(chat_id, "فرمت نادرست است:\n/addplan نام فارسی|English name|50|30|150000")
        return
    if text.startswith("/addconfig ") and is_admin(uid):
        try:
            pending_config[uid] = int(text.split(" ",1)[1])
            send(chat_id, "حالا متن کامل کانفیگ این پلن را در پیام بعدی بفرست.")
        except Exception: send(chat_id, "استفاده: /addconfig PLAN_ID")
        return
    if text == "/stats" and is_admin(uid):
        with db() as c:
            users = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
            orders = c.execute("SELECT COUNT(*) n FROM orders").fetchone()["n"]
            paid = c.execute("SELECT COUNT(*) n FROM orders WHERE status='paid'").fetchone()["n"]
            revenue = c.execute("SELECT COALESCE(SUM(p.price),0) n FROM orders o JOIN plans p ON p.id=o.plan_id WHERE o.status='paid'").fetchone()["n"]
        send(chat_id, f"📊 آمار\nکاربران: {users}\nسفارش‌ها: {orders}\nپرداخت‌شده: {paid}\nفروش ثبت‌شده: {revenue:,} تومان"); return
    if text.startswith("/addadmin ") and uid == OWNER_ID:
        try:
            target = int(text.split(" ",1)[1])
            with db() as c:
                c.execute("INSERT INTO users(tg_id,role,lang) VALUES(?,'admin','fa') ON CONFLICT(tg_id) DO UPDATE SET role='admin'", (target,))
            send(chat_id, f"ادمین {target} اضافه شد.")
        except Exception: send(chat_id, "استفاده: /addadmin user_id")
        return
    if text.startswith("/approve_seller ") and uid == OWNER_ID:
        try:
            target = int(text.split(" ",1)[1])
            with db() as c:
                c.execute("INSERT INTO sellers(tg_id,approved) VALUES(?,1) ON CONFLICT(tg_id) DO UPDATE SET approved=1", (target,))
                c.execute("UPDATE users SET role='seller' WHERE tg_id=?", (target,))
            send(chat_id, f"فروشنده {target} تأیید شد.")
        except Exception: send(chat_id, "استفاده: /approve_seller user_id")
        return
    if m.get("contact"):
        phone = m["contact"].get("phone_number", "")
        with db() as c: c.execute("UPDATE users SET phone=? WHERE tg_id=?", (phone, uid))
        send(chat_id, "شماره تماس ذخیره شد." if lang_of(uid)=="fa" else "Phone number saved.", main_menu(uid))
        return
    if m.get("photo"):
        with db() as c:
            order = c.execute("SELECT * FROM orders WHERE user_id=? AND status='awaiting_receipt' ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
            if order:
                file_id = m["photo"][-1]["file_id"]
                c.execute("UPDATE orders SET receipt_file_id=?,status='receipt_sent' WHERE id=?", (file_id, order["id"]))
                with db() as c2:
                    admins = c2.execute("SELECT tg_id FROM users WHERE role IN ('owner','admin')").fetchall()
                for a in admins:
                    send(a["tg_id"], f"🧾 رسید سفارش #{order['id']}\nکاربر: <code>{uid}</code>\nبرای تأیید یا رد:", inline([
                      [{"text":"✅ پرداخت شد / Deliver config","callback_data":f"approve:{order['id']}"},
                       {"text":"❌ رد پرداخت / Reject","callback_data":f"reject:{order['id']}"}]
                    ]))
                    tg("sendPhoto", {"chat_id":a["tg_id"],"photo":file_id,"caption":f"Receipt for order #{order['id']}"})
                send(chat_id, "رسید دریافت شد و برای بررسی ارسال شد." if lang_of(uid)=="fa" else "Receipt received and sent for review.")
            else:
                send(chat_id, "سفارش منتظر رسیدی ندارید. ابتدا سرویس را انتخاب کنید." if lang_of(uid)=="fa" else "No order awaiting a receipt. Choose a service first.")
        return
    if text:
        send(chat_id, "از منوی ربات استفاده کنید." if lang_of(uid)=="fa" else "Please use the bot menu.", main_menu(uid))

def handle_callback(q):
    uid = q["from"]["id"]
    chat_id = q["message"]["chat"]["id"]
    data = q.get("data","")
    tg("answerCallbackQuery", {"callback_query_id": q["id"]})
    if data == "lang_toggle":
        with db() as c:
            old = c.execute("SELECT lang FROM users WHERE tg_id=?", (uid,)).fetchone()
            new = "en" if old and old["lang"]=="fa" else "fa"
            c.execute("UPDATE users SET lang=? WHERE tg_id=?", (new,uid))
        send(chat_id, "Language updated." if new=="en" else "زبان تغییر کرد.", main_menu(uid))
    elif data == "home":
        send(chat_id, "Main menu / منوی اصلی", main_menu(uid))
    elif data == "check_join":
        if force_join_ok(uid): send(chat_id, "عضویت تأیید شد / Membership verified.", main_menu(uid))
        else: prompt_join(chat_id)
    elif data == "plans": show_plans(chat_id, uid)
    elif data == "account": account(chat_id, uid)
    elif data == "support":
        s = setting("support_id","تنظیم نشده")
        send(chat_id, f"🆘 Support / پشتیبانی: {s}", main_menu(uid))
    elif data == "admin":
        if is_admin(uid): admin_menu(chat_id)
    elif data == "stats":
        if is_admin(uid):
            with db() as c:
                users = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
                orders = c.execute("SELECT COUNT(*) n FROM orders").fetchone()["n"]
                paid = c.execute("SELECT COUNT(*) n FROM orders WHERE status='paid'").fetchone()["n"]
                revenue = c.execute("SELECT COALESCE(SUM(p.price),0) n FROM orders o JOIN plans p ON p.id=o.plan_id WHERE o.status='paid'").fetchone()["n"]
            send(chat_id, f"📊 Users: {users}\nOrders: {orders}\nPaid: {paid}\nRecorded sales: {revenue:,}")
    elif data.startswith("buy:"):
        plan_id = int(data.split(":")[1])
        with db() as c:
            p = c.execute("SELECT * FROM plans WHERE id=? AND active=1", (plan_id,)).fetchone()
            if not p: send(chat_id, "سرویس در دسترس نیست."); return
            c.execute("INSERT INTO orders(user_id,plan_id) VALUES(?,?)", (uid,plan_id))
            order_id = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        fa = lang_of(uid)=="fa"
        card = setting("card_number","تنظیم نشده")
        text = (f"سفارش #{order_id}\nمبلغ: {p['price']:,} تومان\nشماره کارت: <code>{card}</code>\nپس از پرداخت، تصویر رسید را همین‌جا ارسال کنید."
                if fa else f"Order #{order_id}\nAmount: {p['price']:,} toman\nCard number: <code>{card}</code>\nPay, then send the receipt image here.")
        send(chat_id, text)
    elif data.startswith("approve:") and is_admin(uid):
        oid = int(data.split(":")[1])
        with db() as c:
            order = c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
            if not order or order["status"]=="paid":
                send(chat_id, "سفارش پیدا نشد یا قبلاً تأیید شده."); return
            cfg = c.execute("SELECT * FROM configs WHERE plan_id=? AND status='available' ORDER BY id LIMIT 1", (order["plan_id"],)).fetchone()
            if not cfg:
                send(chat_id, "⚠️ کانفیگ آماده‌ای برای این پلن موجود نیست. ابتدا کانفیگ اضافه کنید؛ سفارش هنوز تحویل نشده.")
                return
            c.execute("UPDATE configs SET status='delivered',order_id=? WHERE id=?", (oid,cfg["id"]))
            c.execute("UPDATE orders SET status='paid',config_id=? WHERE id=?", (cfg["id"],oid))
        send(order["user_id"], f"✅ پرداخت تأیید شد!\nکانفیگ شما:\n<code>{cfg['config']}</code>\nآن را در کلاینت سازگار با V2Ray وارد کنید.")
        send(chat_id, f"سفارش #{oid} تأیید شد و کانفیگ تحویل داده شد.")
    elif data.startswith("reject:") and is_admin(uid):
        oid = int(data.split(":")[1])
        with db() as c:
            order = c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
            if order: c.execute("UPDATE orders SET status='rejected' WHERE id=?", (oid,))
        if order: send(order["user_id"], f"پرداخت سفارش #{oid} رد شد. لطفاً با پشتیبانی تماس بگیرید.")
        send(chat_id, f"سفارش #{oid} رد شد.")

@app.route("/webhook/" + WEBHOOK_SECRET, methods=["POST"])
def webhook():
    update = request.get_json(silent=True) or {}
    try:
        if "message" in update: handle_message(update["message"])
        elif "callback_query" in update: handle_callback(update["callback_query"])
    except Exception:
        logging.exception("Failed to process update")
    return jsonify(ok=True)

@app.get("/health")
def health():
    return jsonify(status="ok")

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","8080")))
