# -*- coding: utf-8 -*-
"""
VPN Config Shop Bot  |  ربات فروش کانفیگ
Flask + Webhook + Supabase (REST)  |  Persian + English
Roles: owner > admin > seller > user
"""
import os, io, json, html, logging, threading, time
from datetime import datetime, timedelta, timezone

import requests
import telebot
from telebot import types
from flask import Flask, request, abort

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("shop")

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ.get("OWNER_ID", "6914909647"))
SB_URL = os.environ["SUPABASE_URL"].rstrip("/")
SB_KEY = os.environ["SUPABASE_KEY"]
BASE_URL = (os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", threaded=False)
app = Flask(__name__)
STATE = {}   # uid -> {"s": state, ...}
UC = {}      # user cache

# ───────────────────────── Database (Supabase REST) ─────────────────────────
HDR = {"apikey": SB_KEY, "Authorization": "Bearer " + SB_KEY, "Content-Type": "application/json"}


def _fv(v):
    return str(v).lower() if isinstance(v, bool) else v


def _rest(method, table, params=None, body=None, prefer="return=representation"):
    h = dict(HDR)
    if prefer:
        h["Prefer"] = prefer
    try:
        r = requests.request(method, f"{SB_URL}/rest/v1/{table}", headers=h, params=params,
                             data=json.dumps(body) if body is not None else None, timeout=20)
        if r.status_code >= 300:
            log.error("DB %s %s -> %s %s", method, table, r.status_code, r.text[:300])
            return []
        return r.json() if r.text else []
    except Exception as e:
        log.error("DB error: %s", e)
        return []


def sel(table, order=None, limit=None, raw=None, **f):
    p = {"select": "*"}
    p.update({k: f"eq.{_fv(v)}" for k, v in f.items()})
    if raw: p.update(raw)
    if order: p["order"] = order
    if limit: p["limit"] = limit
    return _rest("GET", table, p, prefer=None)


def ins(table, row):
    r = _rest("POST", table, body=row)
    return r[0] if r else None


def upsert(table, row):
    r = _rest("POST", table, body=row, prefer="resolution=merge-duplicates,return=representation")
    return r[0] if r else None


def upd(table, match, data):
    return _rest("PATCH", table, {k: f"eq.{_fv(v)}" for k, v in match.items()}, body=data)


def rm(table, match):
    return _rest("DELETE", table, {k: f"eq.{_fv(v)}" for k, v in match.items()})


def gs(key, default=""):
    r = sel("settings", key=key)
    return r[0]["value"] if r and r[0].get("value") is not None else default


def ss(key, val):
    upsert("settings", {"key": key, "value": val})


# ───────────────────────── Helpers ─────────────────────────
def esc(s): return html.escape(str(s if s is not None else ""))
def money(n): return f"{int(n or 0):,}"
def now_utc(): return datetime.now(timezone.utc)
def fdate(s): return (s or "")[:10]


def pdt(s):
    try:
        return datetime.fromisoformat(s[:19]).replace(tzinfo=timezone.utc)
    except Exception:
        return None


def user(uid):
    if uid in UC: return UC[uid]
    r = sel("users", id=uid)
    if r: UC[uid] = r[0]
    return r[0] if r else None


def save_user(uid, **d):
    upd("users", {"id": uid}, d)
    UC.pop(uid, None)


def ensure_user(tg):
    u = user(tg.id)
    uname = tg.username or ""
    name = (tg.first_name or "") + (" " + tg.last_name if tg.last_name else "")
    if not u:
        ins("users", {"id": tg.id, "name": name, "username": uname,
                      "role": "owner" if tg.id == OWNER_ID else "user"})
        UC.pop(tg.id, None)
        return user(tg.id)
    ch = {}
    if u.get("name") != name: ch["name"] = name
    if (u.get("username") or "") != uname: ch["username"] = uname
    if tg.id == OWNER_ID and u.get("role") != "owner": ch["role"] = "owner"
    if ch:
        save_user(tg.id, **ch)
        u = user(tg.id)
    return u


def lang(uid):
    u = user(uid)
    return (u or {}).get("lang") or "fa"


def x(uid, fa, en):
    return fa if lang(uid) == "fa" else en


def role(uid):
    if uid == OWNER_ID: return "owner"
    u = user(uid)
    return (u or {}).get("role", "user")


def is_staff(uid): return role(uid) in ("owner", "admin", "seller")
def is_admin(uid): return role(uid) in ("owner", "admin")
def is_owner(uid): return uid == OWNER_ID


def staff_ids():
    ids = {OWNER_ID}
    for r in sel("users", raw={"role": "in.(admin,seller)"}):
        ids.add(r["id"])
    return list(ids)


def K(*rows):
    kb = types.InlineKeyboardMarkup()
    for r in rows:
        kb.row(*[types.InlineKeyboardButton(t, url=d) if d.startswith(("http", "tg:"))
                 else types.InlineKeyboardButton(t, callback_data=d) for t, d in r])
    return kb


def ui(uid, text, kb=None, call=None):
    """Edit the panel message in place (open/close feel) or send a new one."""
    if call:
        try:
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                                  reply_markup=kb, disable_web_page_preview=True)
            return
        except Exception as e:
            if "not modified" in str(e): return
    bot.send_message(uid, text, reply_markup=kb, disable_web_page_preview=True)


def tg_link(u):
    if u.get("username"): return "https://t.me/" + u["username"]
    return f"tg://user?id={u['id']}"


def qr_png(text):
    try:
        if len(text) > 1200: return None
        import qrcode
        b = io.BytesIO()
        qrcode.make(text).save(b, "PNG")
        b.seek(0)
        return b
    except Exception:
        return None


# ───────────────────────── Plans / formatting ─────────────────────────
def traffic(uid, p):
    g = p.get("traffic_gb") or 0
    return x(uid, "نامحدود", "Unlimited") if not g else f"{g} GB"


def plan_line(uid, p):
    return (f"📦 <b>{esc(p['name'])}</b>\n   📊 {traffic(uid, p)}  •  ⏳ {p['days']} {x(uid, 'روز', 'days')}"
            f"  •  💵 {money(p['price'])} {x(uid, 'تومان', 'T')}")


def days_left(uid, o):
    e = pdt(o.get("expires_at") or "")
    if not e: return x(uid, "نامحدود", "No expiry")
    d = (e - now_utc()).days
    if e < now_utc(): return x(uid, "⛔️ منقضی شده", "⛔️ Expired")
    return f"{d} {x(uid, 'روز مانده', 'days left')}"


# ───────────────────────── Gates ─────────────────────────
def not_joined(uid):
    res = []
    for ch in sel("channels"):
        cid = int(ch["chat_id"]) if ch["chat_id"].lstrip("-").isdigit() else ch["chat_id"]
        try:
            m = bot.get_chat_member(cid, uid)
            if m.status in ("left", "kicked"): res.append(ch)
        except Exception as e:
            log.warning("channel check failed %s: %s", ch["chat_id"], e)
    return res


def ask_phone(uid):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add(types.KeyboardButton(x(uid, "📱 اشتراک‌گذاری شماره", "📱 Share my phone"), request_contact=True))
    bot.send_message(uid, x(uid, "📱 <b>فروشنده عزیز</b>، برای فعال شدن دسترسی باید شماره‌ات را با دکمه زیر به اشتراک بگذاری.",
                            "📱 <b>Seller</b>, you must share your phone number with the button below to activate your access."),
                     reply_markup=kb)


def passes(uid, call=None):
    u = user(uid)
    if not u: return False
    if u.get("banned"):
        bot.send_message(uid, x(uid, "🚫 دسترسی شما مسدود شده است.", "🚫 You are banned."))
        return False
    if role(uid) == "seller" and not u.get("phone"):
        ask_phone(uid)
        return False
    if not is_staff(uid):
        nj = not_joined(uid)
        if nj:
            rows = [[(f"📢 {c.get('title') or c['chat_id']}", c.get("link") or "https://t.me/")] for c in nj]
            rows.append([(x(uid, "✅ عضو شدم", "✅ I joined"), "chk")])
            ui(uid, x(uid, "📢 برای استفاده از ربات ابتدا در کانال(های) زیر عضو شوید:",
                      "📢 Please join the channel(s) below to use the bot:"), K(*rows), call)
            return False
    return True


# ───────────────────────── Main panel ─────────────────────────
def main_kb(uid):
    r = [[(x(uid, "🛒 خرید سرویس", "🛒 Buy service"), "buy"), (x(uid, "📦 اشتراک‌های من", "📦 My subscriptions"), "subs")],
         [(x(uid, "💰 تعرفه‌ها", "💰 Tariffs"), "tar"), (x(uid, "👤 حساب کاربری", "👤 My account"), "acc")],
         [(x(uid, "🆘 پشتیبانی", "🆘 Support"), "sup"), (x(uid, "📚 آموزش اتصال", "📚 Guide"), "gd")],
         [(x(uid, "🌐 زبان / Language", "🌐 Language / زبان"), "lang")]]
    if is_staff(uid):
        r.append([(x(uid, "🛠 پنل مدیریت", "🛠 Admin panel"), "a:open")])
    r.append([(x(uid, "🔽 بستن پنل", "🔽 Close panel"), "p:close")])
    return K(*r)


def show_main(uid, call=None):
    u = user(uid)
    w = gs("welcome")
    if not w:
        w = x(uid, "👋 سلام <b>{name}</b>!\nبه فروشگاه کانفیگ خوش آمدی 🚀\nاز پنل زیر استفاده کن 👇",
              "👋 Hi <b>{name}</b>!\nWelcome to the config shop 🚀\nUse the panel below 👇")
        w = w.replace("{name}", esc(u.get("name")))
    ui(uid, w, main_kb(uid), call)


def back_row(uid, to="p:main"):
    return [(x(uid, "🔙 بازگشت", "🔙 Back"), to)]


def lang_picker(uid):
    return K([("🇮🇷 فارسی", "l:fa"), ("🇬🇧 English", "l:en")])


@bot.message_handler(commands=["start", "panel", "menu"])
def h_start(m):
    uid = m.from_user.id
    u = ensure_user(m.from_user)
    STATE.pop(uid, None)
    if not u.get("lang"):
        bot.send_message(uid, "🌐 زبان خود را انتخاب کنید\nPlease choose your language", reply_markup=lang_picker(uid))
        return
    if passes(uid):
        show_main(uid)


@bot.message_handler(content_types=["contact"])
def h_contact(m):
    uid = m.from_user.id
    if m.contact and m.contact.user_id == uid:
        save_user(uid, phone=m.contact.phone_number)
        bot.send_message(uid, x(uid, "✅ شماره ثبت شد.", "✅ Phone saved."), reply_markup=types.ReplyKeyboardRemove())
        if passes(uid): show_main(uid)
    else:
        bot.send_message(uid, x(uid, "⚠️ فقط شماره خودت را بفرست.", "⚠️ Please share your own number."))


# ───────────────────────── Callbacks ─────────────────────────
@bot.callback_query_handler(func=lambda c: True)
def on_cb(call):
    uid = call.from_user.id
    d = call.data
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass
    try:
        route(call, uid, d)
    except Exception as e:
        log.exception("callback error: %s", e)


def route(call, uid, d):
    u = ensure_user(call.from_user)
    if d.startswith("l:"):
        save_user(uid, lang=d[2:])
        if passes(uid): show_main(uid, call)
        return
    if not u.get("lang"):
        ui(uid, "🌐 زبان خود را انتخاب کنید / Choose language", lang_picker(uid), call)
        return
    if not passes(uid, call): return
    p = d.split(":")

    # ── navigation ──
    if d in ("p:main", "chk"): STATE.pop(uid, None); return show_main(uid, call)
    if d == "p:close":
        return ui(uid, x(uid, "📕 پنل بسته شد.", "📕 Panel closed."),
                  K([(x(uid, "📖 باز کردن پنل", "📖 Open panel"), "p:open")]), call)
    if d == "p:open": return show_main(uid, call)
    if d == "lang":
        return ui(uid, "🌐 زبان خود را انتخاب کنید / Choose language",
                  K([("🇮🇷 فارسی", "l:fa"), ("🇬🇧 English", "l:en")], back_row(uid)), call)
    if d == "cancel":
        st = STATE.pop(uid, None)
        if st and st.get("oid"):
            o = get_order(st["oid"])
            if o and o["status"] == "waiting_receipt":
                upd("orders", {"id": o["id"]}, {"status": "cancelled"})
        return show_main(uid, call)

    # ── tariffs / buy ──
    if d == "tar":
        plans = sel("plans", active=True, order="price.asc")
        txt = x(uid, "💰 <b>تعرفه سرویس‌ها</b>\n\n", "💰 <b>Service tariffs</b>\n\n")
        txt += "\n\n".join(plan_line(uid, pl) for pl in plans) if plans else x(uid, "هنوز سرویسی ثبت نشده.", "No plans yet.")
        return ui(uid, txt, K([(x(uid, "🛒 خرید", "🛒 Buy"), "buy")], back_row(uid)), call)
    if d == "buy":
        if gs("shop_open", "1") == "0" and not is_staff(uid):
            return ui(uid, x(uid, "🔒 فروشگاه موقتاً بسته است.", "🔒 The shop is temporarily closed."),
                      K(back_row(uid)), call)
        plans = sel("plans", active=True, order="price.asc")
        if not plans:
            return ui(uid, x(uid, "هنوز سرویسی ثبت نشده.", "No plans yet."), K(back_row(uid)), call)
        rows = [[(f"{pl['name']} • {money(pl['price'])}", f"b:{pl['id']}")] for pl in plans]
        rows.append(back_row(uid))
        return ui(uid, x(uid, "🛒 سرویس مورد نظرت را انتخاب کن:", "🛒 Choose your service:"), K(*rows), call)
    if p[0] == "b":
        pl = sel("plans", id=int(p[1]))
        if not pl: return
        pl = pl[0]
        card, holder = gs("card_number"), gs("card_holder")
        txt = plan_line(uid, pl) + "\n\n"
        if card:
            txt += x(uid, "💳 <b>اطلاعات پرداخت</b>\nمبلغ را به کارت زیر واریز کن:\n\n", "💳 <b>Payment</b>\nTransfer the amount to this card:\n\n")
            txt += f"<code>{esc(card)}</code>\n👤 {esc(holder)}\n💵 <b>{money(pl['price'])}</b> {x(uid, 'تومان', 'T')}\n\n"
            txt += x(uid, "پس از پرداخت روی «ارسال رسید» بزن و عکس رسید را بفرست.", "After paying tap “Send receipt” and upload the receipt photo.")
        else:
            txt += x(uid, "⚠️ شماره کارت هنوز تنظیم نشده؛ با پشتیبانی تماس بگیر.", "⚠️ Card not set yet; contact support.")
        return ui(uid, txt, K([(x(uid, "📤 ارسال رسید", "📤 Send receipt"), f"pay:{pl['id']}")], back_row(uid, "buy")), call)
    if p[0] == "pay":
        pl = sel("plans", id=int(p[1]))
        if not pl: return
        pl = pl[0]
        o = ins("orders", {"user_id": uid, "plan_id": pl["id"], "plan_name": pl["name"], "traffic_gb": pl["traffic_gb"],
                           "days": pl["days"], "price": pl["price"], "status": "waiting_receipt"})
        STATE[uid] = {"s": "receipt", "oid": o["id"]}
        return ui(uid, x(uid, "📸 عکس رسید پرداخت را همین‌جا بفرست (یا کد پیگیری را بنویس).",
                         "📸 Send the payment receipt photo here (or type the reference code)."),
                  K([(x(uid, "❌ انصراف", "❌ Cancel"), "cancel")]), call)

    # ── subscriptions ──
    if d == "subs":
        orders = sel("orders", user_id=uid, status="delivered", order="id.desc", limit=30)
        if not orders:
            return ui(uid, x(uid, "📭 هنوز اشتراکی نداری.", "📭 You have no subscriptions yet."),
                      K([(x(uid, "🛒 خرید", "🛒 Buy"), "buy")], back_row(uid)), call)
        rows = [[(f"#{o['id']} • {o['plan_name']} • {days_left(uid, o)}", f"s:{o['id']}")] for o in orders]
        rows.append(back_row(uid))
        return ui(uid, x(uid, "📦 <b>اشتراک‌های من</b>\nبرای دیدن کانفیگ روی هرکدام بزن:",
                         "📦 <b>My subscriptions</b>\nTap one to see its config:"), K(*rows), call)
    if p[0] == "s":
        o = get_order(int(p[1]))
        if not o or o["user_id"] != uid: return
        txt = x(uid, "📦 <b>{n}</b>\n📊 حجم: {t}\n⏳ وضعیت: {d}\n📅 انقضا: {e}\n\n🔑 <b>کانفیگ (برای کپی لمس کنید):</b>\n<code>{c}</code>",
                "📦 <b>{n}</b>\n📊 Traffic: {t}\n⏳ Status: {d}\n📅 Expires: {e}\n\n🔑 <b>Config (tap to copy):</b>\n<code>{c}</code>")
        txt = txt.format(n=esc(o["plan_name"]), t=traffic(uid, o), d=days_left(uid, o),
                         e=fdate(o.get("expires_at")) or "∞", c=esc(o.get("config")))
        ui(uid, txt, K(back_row(uid, "subs")), call)
        q = qr_png(o.get("config") or "")
        if q: bot.send_photo(uid, q, caption="QR")
        return

    # ── account / support / guide ──
    if d == "acc":
        orders = sel("orders", user_id=uid)
        done = [o for o in orders if o["status"] == "delivered"]
        active = [o for o in done if not (pdt(o.get("expires_at") or "") and pdt(o["expires_at"]) < now_utc())]
        rl = {"owner": x(uid, "👑 مالک", "👑 Owner"), "admin": x(uid, "🛡 ادمین", "🛡 Admin"),
              "seller": x(uid, "🧑‍💼 فروشنده", "🧑‍💼 Seller"), "user": x(uid, "👤 کاربر", "👤 User")}[role(uid)]
        txt = x(uid,
                "👤 <b>حساب کاربری</b>\n\n🆔 آیدی: <code>{id}</code>\n📛 نام: {n}\n🔗 یوزرنیم: {un}\n🎖 نقش: {r}\n🌐 زبان: فارسی\n📅 عضویت: {j}\n"
                "━━━━━━━━━━\n🛒 سفارش‌ها: {oc}\n✅ اشتراک فعال: {ac}\n💰 مجموع خرید: {sp} تومان",
                "👤 <b>My account</b>\n\n🆔 ID: <code>{id}</code>\n📛 Name: {n}\n🔗 Username: {un}\n🎖 Role: {r}\n🌐 Language: English\n📅 Joined: {j}\n"
                "━━━━━━━━━━\n🛒 Orders: {oc}\n✅ Active subs: {ac}\n💰 Total spent: {sp} T")
        txt = txt.format(id=uid, n=esc(u["name"]), un="@" + u["username"] if u.get("username") else "-", r=rl,
                         j=fdate(u["joined_at"]), oc=len(done), ac=len(active), sp=money(sum(o["price"] or 0 for o in done)))
        return ui(uid, txt, K(back_row(uid)), call)
    if d == "sup":
        owner = user(OWNER_ID)
        txt = x(uid, "🆘 <b>پشتیبانی</b>\n\n", "🆘 <b>Support</b>\n\n")
        rows = []
        if owner:
            txt += x(uid, "👑 مالک: ", "👑 Owner: ") + (f"@{owner['username']}" if owner.get("username") else esc(owner["name"])) + f" (<code>{OWNER_ID}</code>)\n"
            rows.append([(x(uid, "👑 پیام به مالک", "👑 Message owner"), tg_link(owner))])
        sellers = sel("users", role="seller")
        for s in sellers[:8]:
            txt += "\n🧑‍💼 " + x(uid, "فروشنده: ", "Seller: ") + esc(s["name"]) + (f" @{s['username']}" if s.get("username") else "")
            txt += f"\n   🆔 <code>{s['id']}</code>" + (f"  📞 <code>{esc(s['phone'])}</code>" if s.get("phone") else "") + "\n"
            rows.append([(f"🧑‍💼 {s['name']}", tg_link(s))])
        extra = gs("support_extra")
        if extra: txt += "\n" + esc(extra)
        rows.append(back_row(uid))
        return ui(uid, txt, K(*rows), call)
    if d == "gd":
        g = gs("guide") or x(uid,
            "📚 <b>آموزش اتصال</b>\n\n1️⃣ کانفیگ را از «اشتراک‌های من» کپی کن.\n2️⃣ یکی از برنامه‌ها را نصب کن:\n"
            "• اندروید: v2rayNG / Hiddify\n• iOS: Streisand / V2Box / Shadowrocket\n• ویندوز: v2rayN / Hiddify\n• مک: V2Box / Hiddify\n"
            "3️⃣ در برنامه گزینه Import from clipboard را بزن و وصل شو ✅",
            "📚 <b>Connection guide</b>\n\n1️⃣ Copy your config from “My subscriptions”.\n2️⃣ Install an app:\n"
            "• Android: v2rayNG / Hiddify\n• iOS: Streisand / V2Box / Shadowrocket\n• Windows: v2rayN / Hiddify\n• Mac: V2Box / Hiddify\n"
            "3️⃣ Tap “Import from clipboard” and connect ✅")
        return ui(uid, g, K(back_row(uid)), call)

    # ── staff: order actions ──
    if p[0] == "o" and is_staff(uid):
        return order_action(call, uid, p)
    # ── admin panel ──
    if p[0] == "a" and is_staff(uid):
        return admin_route(call, uid, p)
    if p[0] == "u" and is_admin(uid):
        return user_action(call, uid, p)


# ───────────────────────── Orders ─────────────────────────
def get_order(oid):
    r = sel("orders", id=oid)
    return r[0] if r else None


def order_text(uid, o, u):
    st = {"pending": "⏳", "paid": "💳", "delivered": "✅", "rejected": "❌", "waiting_receipt": "🕓", "cancelled": "🚫"}.get(o["status"], "")
    t = x(uid, "🧾 <b>سفارش #{id}</b> {st}\n👤 {n} {un} <code>{uid}</code>\n📦 {p} • {tr}\n💵 {pr} تومان\n🕒 {t}",
          "🧾 <b>Order #{id}</b> {st}\n👤 {n} {un} <code>{uid}</code>\n📦 {p} • {tr}\n💵 {pr} T\n🕒 {t}")
    t = t.format(id=o["id"], st=st, n=esc(u.get("name")), un="@" + u["username"] if u.get("username") else "",
                 uid=o["user_id"], p=esc(o["plan_name"]), tr=traffic(uid, o), pr=money(o["price"]), t=fdate(o["created_at"]))
    if o.get("receipt_text"): t += "\n📝 " + esc(o["receipt_text"][:300])
    return t


def send_order_card(to, o):
    u = user(o["user_id"]) or {}
    cap = order_text(to, o, u)
    kb = None
    if o["status"] == "pending":
        kb = K([(x(to, "✅ پرداخت شد", "✅ Paid"), f"o:ok:{o['id']}"), (x(to, "❌ رد", "❌ Reject"), f"o:no:{o['id']}")])
    elif o["status"] == "paid":
        kb = K([(x(to, "📤 ارسال کانفیگ", "📤 Send config"), f"o:cfg:{o['id']}")])
    try:
        t, fid = o.get("receipt_type"), o.get("receipt_file_id")
        if t == "photo" and fid: bot.send_photo(to, fid, caption=cap, reply_markup=kb)
        elif t == "document" and fid: bot.send_document(to, fid, caption=cap, reply_markup=kb)
        else: bot.send_message(to, cap, reply_markup=kb)
    except Exception as e:
        log.warning("card send failed %s: %s", to, e)


def order_action(call, uid, p):
    act, oid = p[1], int(p[2])
    o = get_order(oid)
    if not o: return
    def strip():
        try: bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except Exception: pass
    if act == "v":
        return send_order_card(uid, o)
    if act == "ok":
        if o["status"] != "pending":
            return bot.send_message(uid, x(uid, f"ℹ️ سفارش #{oid} قبلاً بررسی شده ({o['status']}).", f"ℹ️ Order #{oid} already handled ({o['status']})."))
        upd("orders", {"id": oid}, {"status": "paid", "approved_by": uid})
        strip()
        STATE[uid] = {"s": "deliver", "oid": oid}
        bot.send_message(o["user_id"], x(o["user_id"], f"✅ پرداخت سفارش #{oid} تأیید شد. کانفیگ تا لحظاتی دیگر ارسال می‌شود…",
                                         f"✅ Payment for order #{oid} confirmed. Your config is coming shortly…"))
        bot.send_message(uid, x(uid, f"✍️ کانفیگ سفارش <b>#{oid}</b> ({esc(o['plan_name'])}) را بفرست (متن/لینک ساب).\nبرای لغو /start",
                                f"✍️ Send the config for order <b>#{oid}</b> ({esc(o['plan_name'])}) (text / sub link).\n/start to cancel"))
    elif act == "cfg":
        if o["status"] != "paid": return bot.send_message(uid, "ℹ️ " + o["status"])
        STATE[uid] = {"s": "deliver", "oid": oid}
        bot.send_message(uid, x(uid, f"✍️ کانفیگ سفارش <b>#{oid}</b> را بفرست:", f"✍️ Send config for order <b>#{oid}</b>:"))
    elif act == "no":
        if o["status"] != "pending":
            return bot.send_message(uid, x(uid, f"ℹ️ سفارش #{oid} قبلاً بررسی شده.", f"ℹ️ Order #{oid} already handled."))
        upd("orders", {"id": oid}, {"status": "rejected", "approved_by": uid})
        strip()
        bot.send_message(o["user_id"], x(o["user_id"], f"❌ رسید سفارش #{oid} تأیید نشد. در صورت اشتباه با پشتیبانی تماس بگیرید.",
                                         f"❌ Receipt for order #{oid} was rejected. Contact support if this is a mistake."))
        bot.send_message(uid, x(uid, f"❌ سفارش #{oid} رد شد.", f"❌ Order #{oid} rejected."))


def send_config(to, o, cfg):
    txt = x(to, "✅ <b>سفارش #{id} تحویل شد</b>\n📦 {p}\n📊 حجم: {t}\n⏳ مدت: {d} روز\n\n🔑 <b>کانفیگ (لمس برای کپی):</b>\n<code>{c}</code>\n\n"
                "📚 آموزش اتصال: /start ← آموزش اتصال",
            "✅ <b>Order #{id} delivered</b>\n📦 {p}\n📊 Traffic: {t}\n⏳ Duration: {d} days\n\n🔑 <b>Config (tap to copy):</b>\n<code>{c}</code>\n\n"
            "📚 Guide: /start → Guide")
    bot.send_message(to, txt.format(id=o["id"], p=esc(o["plan_name"]), t=traffic(to, o), d=o.get("days") or "∞", c=esc(cfg)))
    q = qr_png(cfg)
    if q: bot.send_photo(to, q, caption="QR")


def deliver(o, cfg, staff):
    now = now_utc()
    exp = (now + timedelta(days=o["days"])).isoformat() if o.get("days") else None
    upd("orders", {"id": o["id"]}, {"status": "delivered", "config": cfg, "delivered_at": now.isoformat(),
                                    "expires_at": exp, "approved_by": o.get("approved_by") or staff})
    send_config(o["user_id"], o, cfg)


def notify_staff(o):
    for sid in staff_ids():
        send_order_card(sid, o)


# ───────────────────────── Stats ─────────────────────────
def stats_text(uid):
    rows = _rest("GET", "orders", {"select": "id,price,status,approved_by,delivered_at"}, prefer=None)
    today = now_utc().strftime("%Y-%m-%d")
    def calc(uid_=None):
        d = [o for o in rows if o["status"] == "delivered" and (uid_ is None or o.get("approved_by") == uid_)]
        return len(d), sum(o["price"] or 0 for o in d), sum(o["price"] or 0 for o in d if fdate(o.get("delivered_at")) == today)
    n, rev, td = calc(uid)
    pend = len([o for o in rows if o["status"] in ("pending", "paid")])
    t = x(uid, "📊 <b>آمار من</b>\n\n✅ سفارش‌های تحویل‌داده: {n}\n💰 فروش من: {r} تومان\n📅 فروش امروز: {t} تومان\n⏳ سفارش‌های در انتظار (کل): {p}",
          "📊 <b>My stats</b>\n\n✅ Orders delivered: {n}\n💰 My sales: {r} T\n📅 Today: {t} T\n⏳ Pending (all): {p}").format(
          n=n, r=money(rev), t=money(td), p=pend)
    if is_owner(uid):
        gn, grev, gtd = calc()
        users = len(sel("users"))
        t += x(uid, f"\n\n━━ 👑 آمار کل ━━\n👥 کاربران: {users}\n✅ تحویل‌شده: {gn}\n💰 فروش کل: {money(grev)} تومان\n📅 امروز: {money(gtd)} تومان\n❌ ردشده: {len([o for o in rows if o['status']=='rejected'])}",
               f"\n\n━━ 👑 Global ━━\n👥 Users: {users}\n✅ Delivered: {gn}\n💰 Total sales: {money(grev)} T\n📅 Today: {money(gtd)} T\n❌ Rejected: {len([o for o in rows if o['status']=='rejected'])}")
        t += "\n\n" + x(uid, "👮 <b>وضعیت ادمین‌ها و فروشنده‌ها</b>", "👮 <b>Staff status</b>")
        for s in sel("users", raw={"role": "in.(admin,seller)"}):
            c, r, tdy = calc(s["id"])
            t += f"\n• {'🛡' if s['role']=='admin' else '🧑‍💼'} {esc(s['name'])} <code>{s['id']}</code> — {c} / {money(r)} ({x(uid,'امروز','today')} {money(tdy)})"
    return t


# ───────────────────────── Admin panel ─────────────────────────
def admin_kb(uid):
    if not is_admin(uid):
        r = [[(x(uid, "📊 آمار من", "📊 My stats"), "a:stats"), (x(uid, "⏳ سفارش‌ها", "⏳ Orders"), "a:pend")]]
    else:
        shop = gs("shop_open", "1") == "1"
        r = [[(x(uid, "📊 آمار", "📊 Stats"), "a:stats"), (x(uid, "⏳ سفارش‌ها", "⏳ Orders"), "a:pend")],
             [(x(uid, "📦 مدیریت سرویس‌ها", "📦 Plans"), "a:plans"), (x(uid, "💳 شماره کارت", "💳 Card"), "a:card")],
             [(x(uid, "📢 کانال اجباری", "📢 Forced channels"), "a:ch"), (x(uid, "👥 کاربران", "👥 Users"), "a:users")],
             [(x(uid, "📣 پیام همگانی", "📣 Broadcast"), "a:bc"), (x(uid, "⚙️ تنظیمات", "⚙️ Settings"), "a:set")],
             [(x(uid, "🟢 فروشگاه: باز", "🟢 Shop: open") if shop else x(uid, "🔴 فروشگاه: بسته", "🔴 Shop: closed"), "a:shop")]]
        if is_owner(uid):
            r.append([(x(uid, "👮 ادمین‌ها و فروشنده‌ها", "👮 Admins & sellers"), "a:staff")])
    r.append([(x(uid, "🔼 بستن پنل مدیریت", "🔼 Collapse"), "a:close"), (x(uid, "🏠 منوی اصلی", "🏠 Main"), "p:main")])
    return K(*r)


def admin_route(call, uid, p):
    a = p[1]
    back = K(back_row(uid, "a:main"))
    if a in ("open", "main"):
        STATE.pop(uid, None)
        return ui(uid, x(uid, f"🛠 <b>پنل مدیریت</b> ({role(uid)})", f"🛠 <b>Admin panel</b> ({role(uid)})"), admin_kb(uid), call)
    if a == "close":
        return ui(uid, x(uid, "🛠 پنل مدیریت بسته است.", "🛠 Admin panel collapsed."),
                  K([(x(uid, "🔽 باز کردن پنل مدیریت", "🔽 Open admin panel"), "a:open")]), call)
    if a == "stats": return ui(uid, stats_text(uid), back, call)
    if a == "pend":
        rows = sel("orders", raw={"status": "in.(pending,paid)"}, order="id.asc", limit=40)
        if not rows: return ui(uid, x(uid, "✅ سفارش در انتظاری نیست.", "✅ No pending orders."), back, call)
        b = [[(f"#{o['id']} {'💳' if o['status']=='paid' else '⏳'} {o['plan_name']} • {money(o['price'])}", f"o:v:{o['id']}")] for o in rows]
        b.append(back_row(uid, "a:main"))
        return ui(uid, x(uid, "⏳ <b>سفارش‌های در انتظار</b>\n⏳ = رسید بررسی نشده | 💳 = منتظر کانفیگ", "⏳ <b>Pending orders</b>\n⏳ = receipt to review | 💳 = waiting config"), K(*b), call)

    # everything below is admin/owner only
    if not is_admin(uid): return
    if a == "shop":
        ss("shop_open", "0" if gs("shop_open", "1") == "1" else "1")
        return ui(uid, x(uid, f"🛠 <b>پنل مدیریت</b>", "🛠 <b>Admin panel</b>"), admin_kb(uid), call)
    if a == "plans":
        plans = sel("plans", order="price.asc")
        b = []
        txt = x(uid, "📦 <b>سرویس‌ها</b>\n\n", "📦 <b>Plans</b>\n\n")
        for pl in plans:
            txt += ("🟢 " if pl["active"] else "⚪️ ") + plan_line(uid, pl) + "\n\n"
            b.append([(f"{'🔕' if pl['active'] else '🔔'} {pl['name']}", f"a:pt:{pl['id']}"), ("🗑", f"a:pd:{pl['id']}")])
        b.append([(x(uid, "➕ افزودن سرویس", "➕ Add plan"), "a:pa")])
        b.append(back_row(uid, "a:main"))
        return ui(uid, txt, K(*b), call)
    if a == "pa":
        STATE[uid] = {"s": "addplan"}
        return ui(uid, x(uid, "➕ در یک پیام بفرست:\n<code>نام | حجم‌GB | روز | قیمت‌تومان</code>\nمثال:\n<code>سرویس ۵۰ گیگ یک‌ماهه | 50 | 30 | 150000</code>\n(حجم 0 = نامحدود)",
                         "➕ Send in one message:\n<code>name | GB | days | price</code>\nExample:\n<code>50GB Monthly | 50 | 30 | 150000</code>\n(GB 0 = unlimited)"),
                  K(back_row(uid, "a:plans")), call)
    if a == "pt":
        pl = sel("plans", id=int(p[2]))
        if pl: upd("plans", {"id": pl[0]["id"]}, {"active": not pl[0]["active"]})
        return admin_route(call, uid, ["a", "plans"])
    if a == "pd":
        rm("plans", {"id": int(p[2])})
        return admin_route(call, uid, ["a", "plans"])
    if a == "card":
        STATE[uid] = {"s": "card"}
        return ui(uid, x(uid, f"💳 کارت فعلی: <code>{esc(gs('card_number','-'))}</code> — {esc(gs('card_holder','-'))}\n\nکارت جدید را بفرست:\n<code>شماره کارت | نام صاحب کارت</code>",
                         f"💳 Current: <code>{esc(gs('card_number','-'))}</code> — {esc(gs('card_holder','-'))}\n\nSend new card:\n<code>card number | holder name</code>"), back, call)
    if a == "ch":
        chs = sel("channels")
        b = [[(f"🗑 {c.get('title') or c['chat_id']}", f"a:chd:{c['id']}")] for c in chs]
        b.append([(x(uid, "➕ افزودن کانال", "➕ Add channel"), "a:cha")])
        b.append(back_row(uid, "a:main"))
        return ui(uid, x(uid, "📢 <b>کانال‌های اجباری</b>\n(ربات باید در کانال ادمین باشد)", "📢 <b>Forced channels</b>\n(the bot must be admin there)"), K(*b), call)
    if a == "cha":
        STATE[uid] = {"s": "addch"}
        return ui(uid, x(uid, "➕ یوزرنیم کانال را بفرست: <code>@mychannel</code>\nبرای کانال خصوصی: <code>-100123456789 https://t.me/+invite</code>",
                         "➕ Send channel username: <code>@mychannel</code>\nPrivate: <code>-100123456789 https://t.me/+invite</code>"),
                  K(back_row(uid, "a:ch")), call)
    if a == "chd":
        rm("channels", {"id": int(p[2])})
        return admin_route(call, uid, ["a", "ch"])
    if a == "users":
        STATE[uid] = {"s": "lookup"}
        return ui(uid, x(uid, f"👥 تعداد کاربران: <b>{len(sel('users'))}</b>\n\nبرای مدیریت کاربر، آیدی عددی‌اش را بفرست:",
                         f"👥 Total users: <b>{len(sel('users'))}</b>\n\nSend a user's numeric ID to manage:"), back, call)
    if a == "bc":
        STATE[uid] = {"s": "bc"}
        return ui(uid, x(uid, "📣 پیام همگانی را بفرست (متن/عکس/…):", "📣 Send the broadcast message (text/photo/…):"), back, call)
    if a == "set":
        return ui(uid, x(uid, "⚙️ <b>تنظیمات</b>", "⚙️ <b>Settings</b>"), K(
            [(x(uid, "👋 متن خوش‌آمدگویی", "👋 Welcome text"), "a:sw:welcome")],
            [(x(uid, "📚 متن آموزش", "📚 Guide text"), "a:sw:guide")],
            [(x(uid, "🆘 متن اضافه پشتیبانی", "🆘 Extra support text"), "a:sw:support_extra")],
            back_row(uid, "a:main")), call)
    if a == "sw":
        STATE[uid] = {"s": "set", "key": p[2]}
        return ui(uid, x(uid, "✍️ متن جدید را بفرست (برای پاک کردن بنویس: -)", "✍️ Send the new text (send - to clear)"), K(back_row(uid, "a:set")), call)
    if a == "staff" and is_owner(uid):
        b, txt = [], x(uid, "👮 <b>ادمین‌ها و فروشنده‌ها</b>\n\n", "👮 <b>Admins & sellers</b>\n\n")
        for s in sel("users", raw={"role": "in.(admin,seller)"}):
            txt += f"{'🛡' if s['role']=='admin' else '🧑‍💼'} {esc(s['name'])} <code>{s['id']}</code>" + (f" 📞 {esc(s['phone'])}" if s.get("phone") else (" ⚠️ no phone" if s["role"] == "seller" else "")) + "\n"
            b.append([(f"🗑 {s['name']}", f"a:srm:{s['id']}")])
        b.append([(x(uid, "➕ ادمین", "➕ Admin"), "a:sadd:admin"), (x(uid, "➕ فروشنده", "➕ Seller"), "a:sadd:seller")])
        b.append([(x(uid, "📊 وضعیت و آمار", "📊 Status & stats"), "a:stats")])
        b.append(back_row(uid, "a:main"))
        return ui(uid, txt, K(*b), call)
    if a == "sadd" and is_owner(uid):
        STATE[uid] = {"s": "addstaff", "role": p[2]}
        return ui(uid, x(uid, "🆔 آیدی عددی شخص را بفرست (او باید قبلاً ربات را /start کرده باشد):", "🆔 Send the person's numeric ID (they must have /start-ed the bot):"),
                  K(back_row(uid, "a:staff")), call)
    if a == "srm" and is_owner(uid):
        save_user(int(p[2]), role="user")
        return admin_route(call, uid, ["a", "staff"])


def show_user(uid, tid):
    t = user(tid)
    if not t:
        return bot.send_message(uid, x(uid, "❌ کاربر پیدا نشد.", "❌ User not found."))
    orders = sel("orders", user_id=tid, status="delivered")
    txt = x(uid, "👤 <b>{n}</b> @{un}\n🆔 <code>{id}</code>\n🎖 {r}\n📞 {ph}\n📅 {j}\n✅ خریدها: {c} | 💰 {s} تومان\n🚫 مسدود: {b}",
            "👤 <b>{n}</b> @{un}\n🆔 <code>{id}</code>\n🎖 {r}\n📞 {ph}\n📅 {j}\n✅ Purchases: {c} | 💰 {s} T\n🚫 Banned: {b}").format(
            n=esc(t["name"]), un=esc(t.get("username")), id=tid, r=t["role"], ph=esc(t.get("phone") or "-"), j=fdate(t["joined_at"]),
            c=len(orders), s=money(sum(o["price"] or 0 for o in orders)), b="✅" if t.get("banned") else "❌")
    bot.send_message(uid, txt, reply_markup=K(
        [(x(uid, "✅ رفع مسدودی" if t.get("banned") else "🚫 مسدود کردن", "✅ Unban" if t.get("banned") else "🚫 Ban"), f"u:ban:{tid}")],
        [(x(uid, "📨 پیام به کاربر", "📨 Message user"), f"u:msg:{tid}"), (x(uid, "🎁 ارسال کانفیگ دستی", "🎁 Send config manually"), f"u:cfg:{tid}")]))


def user_action(call, uid, p):
    act, tid = p[1], int(p[2])
    if act == "ban":
        if tid == OWNER_ID: return
        t = user(tid)
        save_user(tid, banned=not t.get("banned"))
        return show_user(uid, tid)
    if act == "msg":
        STATE[uid] = {"s": "umsg", "uid": tid}
        bot.send_message(uid, x(uid, "✍️ پیام را بفرست:", "✍️ Send the message:"))
    if act == "cfg":
        STATE[uid] = {"s": "ucfg", "uid": tid}
        bot.send_message(uid, x(uid, "🎁 فرمت:\nخط اول: <code>نام سرویس | روز | حجمGB</code>\nخط‌های بعد: کانفیگ",
                                "🎁 Format:\nFirst line: <code>plan name | days | GB</code>\nNext lines: the config"))


# ───────────────────────── Message states ─────────────────────────
def do_broadcast(uid, m):
    users = [u for u in sel("users") if not u.get("banned")]
    ok = 0
    for u in users:
        try:
            bot.copy_message(u["id"], m.chat.id, m.message_id)
            ok += 1
        except Exception:
            pass
        time.sleep(0.05)
    bot.send_message(uid, x(uid, f"📣 ارسال شد: {ok}/{len(users)}", f"📣 Sent: {ok}/{len(users)}"))


@bot.message_handler(content_types=["text", "photo", "document"])
def h_msg(m):
    uid = m.from_user.id
    u = ensure_user(m.from_user)
    st = STATE.get(uid)
    txt = (m.text or "").strip()
    if not st:
        if not u.get("lang") or not passes(uid): return
        return show_main(uid)
    s = st["s"]
    try:
        if s == "receipt":
            o = get_order(st["oid"])
            if not o or o["status"] != "waiting_receipt":
                STATE.pop(uid, None); return
            if m.photo: upd("orders", {"id": o["id"]}, {"receipt_type": "photo", "receipt_file_id": m.photo[-1].file_id})
            elif m.document: upd("orders", {"id": o["id"]}, {"receipt_type": "document", "receipt_file_id": m.document.file_id})
            elif txt: upd("orders", {"id": o["id"]}, {"receipt_type": "text", "receipt_text": txt[:500]})
            else: return
            upd("orders", {"id": o["id"]}, {"status": "pending"})
            STATE.pop(uid, None)
            bot.send_message(uid, x(uid, f"✅ رسید سفارش #{o['id']} ارسال شد. پس از تأیید، کانفیگ برایت فرستاده می‌شود 🙏",
                                    f"✅ Receipt for order #{o['id']} sent. Your config will arrive after approval 🙏"),
                             reply_markup=K([(x(uid, "🏠 منوی اصلی", "🏠 Main"), "p:main")]))
            return notify_staff(get_order(o["id"]))
        if not is_staff(uid): return
        if s == "deliver":
            if not txt: return
            o = get_order(st["oid"])
            if o and o["status"] == "paid":
                deliver(o, txt, uid)
                bot.send_message(uid, x(uid, f"✅ کانفیگ سفارش #{o['id']} برای مشتری ارسال شد.", f"✅ Config for order #{o['id']} delivered."))
            STATE.pop(uid, None)
        elif s == "addplan":
            n, g, d, pr = [i.strip() for i in txt.split("|")]
            ins("plans", {"name": n, "traffic_gb": int(g), "days": int(d), "price": int(pr)})
            STATE.pop(uid, None)
            bot.send_message(uid, x(uid, "✅ سرویس افزوده شد.", "✅ Plan added."), reply_markup=K(back_row(uid, "a:plans")))
        elif s == "card":
            n, h = [i.strip() for i in txt.split("|", 1)]
            ss("card_number", n); ss("card_holder", h)
            STATE.pop(uid, None)
            bot.send_message(uid, "✅", reply_markup=K(back_row(uid, "a:main")))
        elif s == "addch":
            parts = txt.split()
            cid = parts[0]
            chat = bot.get_chat(int(cid) if cid.lstrip("-").isdigit() else cid)
            link = parts[1] if len(parts) > 1 else ("https://t.me/" + cid[1:] if cid.startswith("@") else "")
            ins("channels", {"chat_id": cid, "title": chat.title, "link": link})
            STATE.pop(uid, None)
            bot.send_message(uid, x(uid, f"✅ کانال «{esc(chat.title)}» افزوده شد.", f"✅ Channel “{esc(chat.title)}” added."), reply_markup=K(back_row(uid, "a:ch")))
        elif s == "lookup":
            STATE.pop(uid, None)
            show_user(uid, int(txt))
        elif s == "umsg":
            bot.copy_message(st["uid"], m.chat.id, m.message_id)
            STATE.pop(uid, None)
            bot.send_message(uid, "✅")
        elif s == "ucfg":
            first, _, cfg = txt.partition("\n")
            parts = [i.strip() for i in first.split("|")]
            days = int(parts[1]) if len(parts) > 1 else 30
            o = ins("orders", {"user_id": st["uid"], "plan_name": parts[0], "days": days, "traffic_gb": int(parts[2]) if len(parts) > 2 else 0,
                               "price": 0, "status": "paid", "approved_by": uid})
            deliver(o, cfg.strip(), uid)
            STATE.pop(uid, None)
            bot.send_message(uid, "✅")
        elif s == "bc":
            STATE.pop(uid, None)
            threading.Thread(target=do_broadcast, args=(uid, m), daemon=True).start()
            bot.send_message(uid, x(uid, "📣 ارسال شروع شد…", "📣 Broadcasting…"))
        elif s == "set":
            ss(st["key"], "" if txt == "-" else txt)
            STATE.pop(uid, None)
            bot.send_message(uid, "✅", reply_markup=K(back_row(uid, "a:set")))
        elif s == "addstaff":
            tid = int(txt)
            if not user(tid):
                return bot.send_message(uid, x(uid, "❌ این کاربر هنوز ربات را استارت نکرده.", "❌ This user hasn't started the bot yet."))
            save_user(tid, role=st["role"])
            STATE.pop(uid, None)
            bot.send_message(uid, "✅", reply_markup=K(back_row(uid, "a:staff")))
            bot.send_message(tid, x(tid, f"🎉 شما به عنوان <b>{st['role']}</b> منصوب شدید. /start", f"🎉 You are now <b>{st['role']}</b>. /start"))
            if st["role"] == "seller" and not user(tid).get("phone"): ask_phone(tid)
    except Exception as e:
        log.warning("state %s failed: %s", s, e)
        bot.send_message(uid, x(uid, f"⚠️ ورودی نامعتبر یا خطا: {esc(e)}", f"⚠️ Invalid input or error: {esc(e)}"))


# ───────────────────────── Flask / webhook ─────────────────────────
@app.route("/")
def index(): return "OK"


@app.route("/health")
def health(): return "alive"


@app.route(f"/webhook/<token>", methods=["POST"])
def webhook(token):
    if token != BOT_TOKEN: abort(403)
    try:
        bot.process_new_updates([telebot.types.Update.de_json(request.get_data().decode("utf-8"))])
    except Exception as e:
        log.exception("update error: %s", e)
    return "ok", 200


def init_webhook():
    if BASE_URL:
        try:
            bot.remove_webhook()
            time.sleep(0.5)
            bot.set_webhook(url=f"{BASE_URL}/webhook/{BOT_TOKEN}", max_connections=40)
            log.info("Webhook set: %s", BASE_URL)
        except Exception as e:
            log.error("set_webhook failed: %s", e)


init_webhook()

if __name__ == "__main__":
    if BASE_URL:
        app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
    else:   # local test with polling
        bot.remove_webhook()
        bot.infinity_polling()
