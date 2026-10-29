# -*- coding: utf-8 -*-
"""
VPN Config Shop Bot  |  ربات فروش کانفیگ  (final)
Flask + Webhook + Supabase REST | Persian + English
Roles: owner > admin > seller > user
"""
import os, io, re, json, html, logging, threading, time, hashlib
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

import requests
import telebot
from telebot import types
from flask import Flask, request, abort

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("shop")

BOT_TOKEN = os.environ["BOT_TOKEN"].strip()
OWNER_ID = int(os.environ.get("OWNER_ID", "6914909647"))
SB_URL = os.environ["SUPABASE_URL"].strip().rstrip("/")
SB_KEY = os.environ["SUPABASE_KEY"].strip()
BASE_URL = (os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
# Safe token for URL path + Telegram secret header (Render's generated values may contain / + =)
HOOK = hashlib.sha256((os.environ.get("WEBHOOK_SECRET") or BOT_TOKEN).encode()).hexdigest()[:48]
TEHRAN = timezone(timedelta(hours=3, minutes=30))

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", threaded=False)
app = Flask(__name__)
STATE, UC, SC, JOINED = {}, {}, {}, {}
CH_CACHE = [None, 0]
_NS = object()

# ═════════════════════════ Database (Supabase REST) ═════════════════════════
HDR = {"apikey": SB_KEY, "Authorization": "Bearer " + SB_KEY, "Content-Type": "application/json"}
SESSION = requests.Session()


class DBError(Exception):
    pass


def _fv(v):
    return str(v).lower() if isinstance(v, bool) else v


def _rest(method, table, params=None, body=None, prefer="return=representation", strict=False):
    h = dict(HDR)
    if prefer: h["Prefer"] = prefer
    try:
        r = SESSION.request(method, f"{SB_URL}/rest/v1/{table}", headers=h, params=params,
                            data=json.dumps(body) if body is not None else None, timeout=20)
    except Exception as e:
        log.error("DB network error: %s", e)
        if strict: raise DBError(str(e))
        return []
    if r.status_code >= 300:
        log.error("DB %s %s -> %s %s", method, table, r.status_code, r.text[:300])
        if strict: raise DBError(f"{r.status_code} {r.text[:160]}")
        return []
    try:
        return r.json() if r.text else []
    except Exception:
        return []


def sel(table, order=None, limit=None, raw=None, select="*", **f):
    p = {"select": select}
    p.update({k: f"eq.{_fv(v)}" for k, v in f.items()})
    if raw: p.update(raw)
    if order: p["order"] = order
    if limit: p["limit"] = limit
    return _rest("GET", table, p, prefer=None)


def sel_all(table, select="*", raw=None, **f):
    """Paginated select (Supabase caps one response at 1000 rows)."""
    out, off = [], 0
    while True:
        p = {"select": select, "order": "id.asc", "limit": 1000, "offset": off}
        p.update({k: f"eq.{_fv(v)}" for k, v in f.items()})
        if raw: p.update(raw)
        rows = _rest("GET", table, p, prefer=None)
        out += rows
        if len(rows) < 1000: return out
        off += 1000


def ins(table, row):
    r = _rest("POST", table, body=row, strict=True)
    return r[0] if r else None


def upsert(table, row):
    r = _rest("POST", table, body=row, prefer="resolution=merge-duplicates,return=representation", strict=True)
    return r[0] if r else None


def upd(table, match, data):
    return _rest("PATCH", table, {k: f"eq.{_fv(v)}" for k, v in match.items()}, body=data, strict=True)


def rm(table, match):
    return _rest("DELETE", table, {k: f"eq.{_fv(v)}" for k, v in match.items()}, strict=True)


def claim(oid, from_status, to_status, **data):
    """Atomic status change. Returns the updated row, or None if someone else already changed it."""
    data["status"] = to_status
    rows = _rest("PATCH", "orders", {"id": f"eq.{oid}", "status": f"eq.{from_status}"}, body=data, strict=True)
    return rows[0] if rows else None


def gs(key, default=""):
    h = SC.get(key)
    if h is None or time.time() - h[1] > 30:
        r = sel("settings", key=key)
        h = (r[0]["value"] if r else None, time.time())
        SC[key] = h
    return default if h[0] is None else h[0]


def ss(key, val):
    upsert("settings", {"key": key, "value": val})
    SC[key] = (val, time.time())


def get_channels():
    if CH_CACHE[0] is None or time.time() - CH_CACHE[1] > 30:
        CH_CACHE[0], CH_CACHE[1] = sel("channels", order="id.asc"), time.time()
    return CH_CACHE[0]


# ── persistent conversation state (survives Render sleep/restart) ──
def st_get(uid):
    v = STATE.get(uid, _NS)
    if v is not _NS: return v
    r = sel("bot_states", uid=uid)
    v = r[0]["data"] if r else None
    STATE[uid] = v
    return v


def st_set(uid, data):
    STATE[uid] = data
    try: upsert("bot_states", {"uid": uid, "data": data, "updated_at": now_iso()})
    except DBError as e: log.warning("state save failed: %s", e)


def st_clear(uid):
    had = STATE.get(uid, _NS)
    STATE[uid] = None
    if had is not None:
        try: rm("bot_states", {"uid": uid})
        except DBError: pass


# ═════════════════════════ Helpers ═════════════════════════
def esc(s): return html.escape(str(s if s is not None else ""))
def plain(t): return html.unescape(re.sub(r"<[^>]+>", "", t))
def money(n): return f"{int(n or 0):,}"
def now_utc(): return datetime.now(timezone.utc)
def now_iso(): return now_utc().isoformat()
def fdate(s): return (s or "")[:10]


def pdt(s):
    try: return datetime.fromisoformat(s[:19]).replace(tzinfo=timezone.utc)
    except Exception: return None


def tdate(s):
    d = pdt(s or "")
    return d.astimezone(TEHRAN).strftime("%Y-%m-%d") if d else ""


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
    name = ((tg.first_name or "") + (" " + tg.last_name if tg.last_name else "")).strip()
    if not u:
        try:
            created = ins("users", {"id": tg.id, "name": name, "username": uname,
                                    "role": "owner" if tg.id == OWNER_ID else "user"})
        except DBError:
            return None
        UC[tg.id] = created
        return created
    ch = {}
    if u.get("name") != name: ch["name"] = name
    if (u.get("username") or "") != uname: ch["username"] = uname
    if tg.id == OWNER_ID and u.get("role") != "owner": ch["role"] = "owner"
    if ch:
        try: save_user(tg.id, **ch)
        except DBError: pass
        u = user(tg.id) or u
    return u


def lang(uid): return (user(uid) or {}).get("lang") or "fa"
def x(uid, fa, en): return fa if lang(uid) == "fa" else en


def role(uid):
    if uid == OWNER_ID: return "owner"
    return (user(uid) or {}).get("role", "user")


def is_staff(uid): return role(uid) in ("owner", "admin", "seller")
def is_admin(uid): return role(uid) in ("owner", "admin")
def is_owner(uid): return uid == OWNER_ID


def staff_ids():
    ids = {OWNER_ID}
    for r in sel("users", raw={"role": "in.(admin,seller)"}): ids.add(r["id"])
    return list(ids)


def admin_ids():
    ids = {OWNER_ID}
    for r in sel("users", role="admin"): ids.add(r["id"])
    return list(ids)


def K(*rows):
    kb = types.InlineKeyboardMarkup()
    for r in rows:
        kb.row(*[types.InlineKeyboardButton(t, url=d) if d.startswith(("http", "tg:"))
                 else types.InlineKeyboardButton(t, callback_data=d) for t, d in r])
    return kb


def send(to, text, kb=None, **kw):
    try:
        return bot.send_message(to, text, reply_markup=kb, disable_web_page_preview=True, **kw)
    except telebot.apihelper.ApiTelegramException as e:
        if "parse entities" in str(e):
            return bot.send_message(to, plain(text), reply_markup=kb, parse_mode="", disable_web_page_preview=True)
        raise


def ui(uid, text, kb=None, call=None):
    """Edit the panel in place (smooth open/close) or send a fresh message."""
    if call:
        try:
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                                  reply_markup=kb, disable_web_page_preview=True)
            return
        except telebot.apihelper.ApiTelegramException as e:
            s = str(e)
            if "not modified" in s: return
            if "parse entities" in s:
                try:
                    bot.edit_message_text(plain(text), call.message.chat.id, call.message.message_id,
                                          reply_markup=kb, parse_mode="", disable_web_page_preview=True)
                    return
                except Exception: pass
        except Exception: pass
    send(uid, text, kb)


def tg_link(u):
    return "https://t.me/" + u["username"] if u.get("username") else f"tg://user?id={u['id']}"


def qr_png(text):
    try:
        if not text or len(text) > 1200: return None
        import qrcode
        b = io.BytesIO()
        qrcode.make(text).save(b, "PNG")
        b.seek(0)
        return b
    except Exception:
        return None


def back_row(uid, to="p:main"): return [(x(uid, "🔙 بازگشت", "🔙 Back"), to)]


# ═════════════════════════ Plans / formatting ═════════════════════════
STL = {"waiting_receipt": ("🕓 منتظر رسید", "🕓 Awaiting receipt"), "pending": ("⏳ در حال بررسی", "⏳ Under review"),
       "paid": ("💳 تأیید شد؛ منتظر کانفیگ", "💳 Approved; awaiting config"), "delivered": ("✅ تحویل شد", "✅ Delivered"),
       "rejected": ("❌ رد شد", "❌ Rejected"), "cancelled": ("🚫 لغو شد", "🚫 Cancelled")}


def stl(uid, s):
    fa, en = STL.get(s, (s, s))
    return x(uid, fa, en)


def traffic(uid, p):
    g = p.get("traffic_gb") or 0
    return x(uid, "نامحدود", "Unlimited") if not g else f"{g} GB"


def dur(uid, p):
    d = p.get("days") or 0
    return x(uid, "نامحدود", "Unlimited") if not d else f"{d} {x(uid, 'روز', 'days')}"


_DIG = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_UNL = ("", "-", "0", "∞", "نامحدود", "نا محدود", "unlimited", "inf", "unlim")


def dig(s): return str(s).strip().translate(_DIG).replace(",", "").replace("،", "").replace("٬", "")


def num(s):
    """Number, or 0 for 'unlimited' words (نامحدود / unlimited / - / 0)."""
    v = dig(s).lower()
    return 0 if v in _UNL else int(v)


def visible_plans(plans):
    """Hide plans whose category is switched off."""
    hidden = {c["id"] for c in sel("categories") if not c.get("active")}
    return [p for p in plans if p.get("category_id") not in hidden]


def plan_line(uid, p):
    return (f"📦 <b>{esc(p['name'])}</b>\n   📊 {traffic(uid, p)}  •  ⏳ {dur(uid, p)}"
            f"  •  💵 {money(p['price'])} {x(uid, 'تومان', 'T')}")


def days_left(uid, o):
    e = pdt(o.get("expires_at") or "")
    if not e: return x(uid, "بدون انقضا", "No expiry")
    if e < now_utc(): return x(uid, "⛔️ منقضی شده", "⛔️ Expired")
    return f"{(e - now_utc()).days} {x(uid, 'روز مانده', 'days left')}"


# ═════════════════════════ Gates ═════════════════════════
def not_joined(uid):
    if time.time() - JOINED.get(uid, 0) < 120: return []
    res = []
    for ch in get_channels():
        cid = int(ch["chat_id"]) if ch["chat_id"].lstrip("-").isdigit() else ch["chat_id"]
        try:
            if bot.get_chat_member(cid, uid).status in ("left", "kicked"): res.append(ch)
        except Exception as e:
            log.warning("channel check failed %s: %s (is the bot admin there?)", ch["chat_id"], e)
    if not res: JOINED[uid] = time.time()
    return res


def ask_phone(uid):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add(types.KeyboardButton(x(uid, "📱 اشتراک‌گذاری شماره", "📱 Share my phone"), request_contact=True))
    send(uid, x(uid, "📱 <b>فروشنده عزیز</b>، برای فعال شدن دسترسی باید شماره‌ات را با دکمه زیر به اشتراک بگذاری.",
                "📱 <b>Seller</b>, you must share your phone number with the button below to activate your access."), kb)


def passes(uid, call=None):
    u = user(uid)
    if not u: return False
    if u.get("banned"):
        send(uid, x(uid, "🚫 دسترسی شما مسدود شده است.", "🚫 You are banned."))
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


# ═════════════════════════ Main panel ═════════════════════════
def main_kb(uid):
    r = [[(x(uid, "🛒 خرید سرویس", "🛒 Buy service"), "buy"), (x(uid, "📦 اشتراک‌های من", "📦 My subscriptions"), "subs")],
         [(x(uid, "💰 تعرفه‌ها", "💰 Tariffs"), "tar"), (x(uid, "👤 حساب کاربری", "👤 My account"), "acc")],
         [(x(uid, "🆘 پشتیبانی", "🆘 Support"), "sup"), (x(uid, "📚 آموزش اتصال", "📚 Guide"), "gd")],
         [(x(uid, "🌐 زبان / Language", "🌐 Language / زبان"), "lang")]]
    if is_staff(uid): r.append([(x(uid, "🛠 پنل مدیریت", "🛠 Admin panel"), "a:open")])
    r.append([(x(uid, "🔽 بستن پنل", "🔽 Close panel"), "p:close")])
    return K(*r)


def show_main(uid, call=None):
    w = gs("welcome")
    if not w:
        w = x(uid, "👋 سلام <b>{name}</b>!\nبه فروشگاه کانفیگ خوش آمدی 🚀\nاز پنل زیر استفاده کن 👇",
              "👋 Hi <b>{name}</b>!\nWelcome to the config shop 🚀\nUse the panel below 👇")
    ui(uid, w.replace("{name}", esc((user(uid) or {}).get("name"))), main_kb(uid), call)


def lang_picker(): return K([("🇮🇷 فارسی", "l:fa"), ("🇬🇧 English", "l:en")])


def db_error(uid):
    send(uid, x(uid, "⚠️ مشکل موقت در اتصال به پایگاه داده. چند لحظه بعد دوباره تلاش کن.",
                "⚠️ Temporary database problem. Please try again in a moment."))


@bot.message_handler(commands=["id"])
def h_id(m):
    send(m.chat.id, f"🆔 <code>{m.from_user.id}</code>")


@bot.message_handler(commands=["start", "panel", "menu"])
def h_start(m):
    uid = m.from_user.id
    u = ensure_user(m.from_user)
    if not u: return db_error(uid)
    st_clear(uid)
    if not u.get("lang"):
        return send(uid, "🌐 زبان خود را انتخاب کنید\nPlease choose your language", lang_picker())
    if passes(uid): show_main(uid)


@bot.message_handler(content_types=["contact"])
def h_contact(m):
    uid = m.from_user.id
    if not ensure_user(m.from_user): return db_error(uid)
    if m.contact and m.contact.user_id == uid:
        save_user(uid, phone=m.contact.phone_number)
        send(uid, x(uid, "✅ شماره ثبت شد.", "✅ Phone saved."), types.ReplyKeyboardRemove())
        if passes(uid): show_main(uid)
    else:
        send(uid, x(uid, "⚠️ فقط شماره خودت را بفرست.", "⚠️ Please share your own number."))


# ═════════════════════════ Callbacks ═════════════════════════
@bot.callback_query_handler(func=lambda c: True)
def on_cb(call):
    try: bot.answer_callback_query(call.id)
    except Exception: pass
    try:
        route(call, call.from_user.id, call.data or "")
    except DBError:
        db_error(call.from_user.id)
    except Exception as e:
        log.exception("callback error: %s", e)


def show_sub(uid, o, call=None):
    cfg = o.get("config") or ""
    long_cfg = len(cfg) > 2500
    txt = x(uid, "📦 <b>{n}</b>\n📊 حجم: {t}\n⏳ وضعیت: {d}\n📅 انقضا: {e}\n\n🔑 <b>کانفیگ (برای کپی لمس کنید):</b>\n<code>{c}</code>",
            "📦 <b>{n}</b>\n📊 Traffic: {t}\n⏳ Status: {d}\n📅 Expires: {e}\n\n🔑 <b>Config (tap to copy):</b>\n<code>{c}</code>")
    txt = txt.format(n=esc(o["plan_name"]), t=traffic(uid, o), d=days_left(uid, o), e=fdate(o.get("expires_at")) or "∞",
                     c=x(uid, "(طولانی است؛ در فایل زیر)", "(too long; see file below)") if long_cfg else esc(cfg))
    ui(uid, txt, K([(x(uid, "🔄 خرید مجدد", "🔄 Buy again"), "buy")], back_row(uid, "subs")), call)
    if long_cfg:
        f = io.BytesIO(cfg.encode()); f.name = f"config-{o['id']}.txt"
        bot.send_document(uid, f)
    else:
        q = qr_png(cfg)
        if q: bot.send_photo(uid, q, caption="QR")


def route(call, uid, d):
    u = ensure_user(call.from_user)
    if not u: return db_error(uid)
    if d.startswith("l:"):
        save_user(uid, lang=d[2:] if d[2:] in ("fa", "en") else "fa")
        if passes(uid): show_main(uid, call)
        return
    if not u.get("lang"):
        return ui(uid, "🌐 زبان خود را انتخاب کنید / Choose language", lang_picker(), call)
    if d == "chk": JOINED.pop(uid, None)
    if not passes(uid, call): return
    p = d.split(":")

    # ── navigation ──
    if d in ("p:main", "chk"):
        st_clear(uid)
        return show_main(uid, call)
    if d == "p:close":
        return ui(uid, x(uid, "📕 پنل بسته شد.", "📕 Panel closed."), K([(x(uid, "📖 باز کردن پنل", "📖 Open panel"), "p:open")]), call)
    if d == "p:open": return show_main(uid, call)
    if d == "lang":
        return ui(uid, "🌐 زبان خود را انتخاب کنید / Choose language", K([("🇮🇷 فارسی", "l:fa"), ("🇬🇧 English", "l:en")], back_row(uid)), call)
    if d == "cancel":
        st = st_get(uid)
        st_clear(uid)
        if st and st.get("oid"):
            claim(st["oid"], "waiting_receipt", "cancelled")
        return show_main(uid, call)

    # ── tariffs / buy ──
    if d == "tar":
        plans = sel("plans", active=True, order="price.asc")
        txt = x(uid, "💰 <b>تعرفه سرویس‌ها</b>\n\n", "💰 <b>Service tariffs</b>\n\n")
        plans = visible_plans(plans)
        cmap = {c["id"]: c["name"] for c in sel("categories", active=True, order="id.asc")}
        if not plans:
            txt += x(uid, "هنوز سرویسی ثبت نشده.", "No plans yet.")
        elif not any(pl.get("category_id") in cmap for pl in plans):
            txt += "\n\n".join(plan_line(uid, pl) for pl in plans)
        else:
            blocks = []
            for cid, name in list(cmap.items()) + [(None, x(uid, "سایر", "Other"))]:
                grp = [pl for pl in plans if (pl.get("category_id") if pl.get("category_id") in cmap else None) == cid]
                if grp: blocks.append(f"📁 <b>{esc(name)}</b>\n\n" + "\n\n".join(plan_line(uid, pl) for pl in grp))
            txt += "\n\n━━━━━━━━━━\n\n".join(blocks)
        return ui(uid, txt, K([(x(uid, "🛒 خرید", "🛒 Buy"), "buy")], back_row(uid)), call)
    if d == "buy" or p[0] in ("b", "pay", "bc"):
        if gs("shop_open", "1") == "0" and not is_staff(uid):
            return ui(uid, x(uid, "🔒 فروشگاه موقتاً بسته است.", "🔒 The shop is temporarily closed."), K(back_row(uid)), call)
    if d == "buy" or p[0] == "bc":
        plans = visible_plans(sel("plans", active=True, order="price.asc"))
        if not plans:
            return ui(uid, x(uid, "هنوز سرویسی ثبت نشده.", "No plans yet."), K(back_row(uid)), call)
        cats = [c for c in sel("categories", active=True, order="id.asc") if any(pl.get("category_id") == c["id"] for pl in plans)]
        pbtn = lambda pl: [(f"{pl['name']} • {money(pl['price'])}", f"b:{pl['id']}")]
        if p[0] == "bc":
            cid = int(p[1])
            cat = next((c for c in cats if c["id"] == cid), None)
            if not cat: return ui(uid, x(uid, "این دسته در دسترس نیست.", "This category is unavailable."), K(back_row(uid, "buy")), call)
            rows = [pbtn(pl) for pl in plans if pl.get("category_id") == cid] + [back_row(uid, "buy")]
            return ui(uid, f"📁 <b>{esc(cat['name'])}</b>\n" + x(uid, "سرویس مورد نظرت را انتخاب کن:", "Choose your service:"), K(*rows), call)
        if cats:
            rows = [[(f"📁 {c['name']}", f"bc:{c['id']}")] for c in cats]
            rows += [pbtn(pl) for pl in plans if not pl.get("category_id")] + [back_row(uid)]
            return ui(uid, x(uid, "🛒 دسته‌بندی مورد نظرت را انتخاب کن:", "🛒 Choose a category:"), K(*rows), call)
        rows = [pbtn(pl) for pl in plans] + [back_row(uid)]
        return ui(uid, x(uid, "🛒 سرویس مورد نظرت را انتخاب کن:", "🛒 Choose your service:"), K(*rows), call)
    if p[0] == "b":
        pl = sel("plans", id=int(p[1]), active=True)
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
        rows = ([[(x(uid, "📤 ارسال رسید", "📤 Send receipt"), f"pay:{pl['id']}")]] if card else []) + [back_row(uid, f"bc:{pl['category_id']}" if pl.get("category_id") else "buy")]
        return ui(uid, txt, K(*rows), call)
    if p[0] == "pay":
        pl = sel("plans", id=int(p[1]), active=True)
        if not pl: return
        pl = pl[0]
        upd("orders", {"user_id": uid, "status": "waiting_receipt"}, {"status": "cancelled"})
        o = ins("orders", {"user_id": uid, "plan_id": pl["id"], "plan_name": pl["name"], "traffic_gb": pl["traffic_gb"],
                           "days": pl["days"], "price": pl["price"], "status": "waiting_receipt"})
        st_set(uid, {"s": "receipt", "oid": o["id"]})
        return ui(uid, x(uid, f"📸 سفارش <b>#{o['id']}</b>\nعکس رسید پرداخت را همین‌جا بفرست (یا کد پیگیری را بنویس).",
                         f"📸 Order <b>#{o['id']}</b>\nSend the payment receipt photo here (or type the reference code)."),
                  K([(x(uid, "❌ انصراف", "❌ Cancel"), "cancel")]), call)

    # ── subscriptions / orders ──
    if d == "subs":
        orders = sel("orders", user_id=uid, status="delivered", order="id.desc", limit=30)
        if not orders:
            return ui(uid, x(uid, "📭 هنوز اشتراکی نداری.", "📭 You have no subscriptions yet."),
                      K([(x(uid, "🛒 خرید", "🛒 Buy"), "buy")], back_row(uid)), call)
        rows = [[(f"#{o['id']} • {o['plan_name']} • {days_left(uid, o)}", f"s:{o['id']}")] for o in orders] + [back_row(uid)]
        return ui(uid, x(uid, "📦 <b>اشتراک‌های من</b>\nبرای دیدن کانفیگ روی هرکدام بزن:", "📦 <b>My subscriptions</b>\nTap one to see its config:"), K(*rows), call)
    if p[0] == "s":
        o = sel("orders", id=int(p[1]))
        if not o or o[0]["user_id"] != uid or o[0]["status"] != "delivered": return
        return show_sub(uid, o[0], call)
    if d == "ord":
        orders = sel("orders", user_id=uid, order="id.desc", limit=10)
        txt = x(uid, "🧾 <b>آخرین سفارش‌های من</b>\n\n", "🧾 <b>My recent orders</b>\n\n")
        txt += "\n".join(f"#{o['id']} • {esc(o['plan_name'])} • {money(o['price'])} • {stl(uid, o['status'])}" for o in orders) if orders else x(uid, "سفارشی نداری.", "No orders yet.")
        return ui(uid, txt, K(back_row(uid, "acc")), call)

    # ── account / support / guide ──
    if d == "acc":
        orders = sel("orders", user_id=uid)
        done = [o for o in orders if o["status"] == "delivered"]
        active = [o for o in done if not (pdt(o.get("expires_at") or "") and pdt(o["expires_at"]) < now_utc())]
        rl = {"owner": x(uid, "👑 مالک", "👑 Owner"), "admin": x(uid, "🛡 ادمین", "🛡 Admin"),
              "seller": x(uid, "🧑‍💼 فروشنده", "🧑‍💼 Seller"), "user": x(uid, "👤 کاربر", "👤 User")}[role(uid)]
        txt = x(uid,
                "👤 <b>حساب کاربری</b>\n\n🆔 آیدی: <code>{id}</code>\n📛 نام: {n}\n🔗 یوزرنیم: {un}\n🎖 نقش: {r}\n🌐 زبان: فارسی\n📅 عضویت: {j}\n"
                "━━━━━━━━━━\n🛒 خریدها: {oc}\n✅ اشتراک فعال: {ac}\n💰 مجموع خرید: {sp} تومان",
                "👤 <b>My account</b>\n\n🆔 ID: <code>{id}</code>\n📛 Name: {n}\n🔗 Username: {un}\n🎖 Role: {r}\n🌐 Language: English\n📅 Joined: {j}\n"
                "━━━━━━━━━━\n🛒 Purchases: {oc}\n✅ Active subs: {ac}\n💰 Total spent: {sp} T")
        txt = txt.format(id=uid, n=esc(u["name"]), un="@" + u["username"] if u.get("username") else "-", r=rl,
                         j=fdate(u["joined_at"]), oc=len(done), ac=len(active), sp=money(sum(o["price"] or 0 for o in done)))
        return ui(uid, txt, K([(x(uid, "🧾 سفارش‌های من", "🧾 My orders"), "ord")], back_row(uid)), call)
    if d == "sup":
        owner = user(OWNER_ID)
        txt = x(uid, "🆘 <b>پشتیبانی</b>\n\n", "🆘 <b>Support</b>\n\n")
        rows = [[(x(uid, "✉️ ارسال پیام به پشتیبانی", "✉️ Message support"), "tk")]]
        if owner:
            txt += x(uid, "👑 مالک: ", "👑 Owner: ") + (f"@{owner['username']}" if owner.get("username") else esc(owner["name"])) + f"\n   🆔 <code>{OWNER_ID}</code>\n"
            rows.append([(x(uid, "👑 پیام به مالک", "👑 Message owner"), tg_link(owner))])
        for s in [s for s in sel("users", role="seller") if s.get("phone")][:8]:
            txt += "\n🧑‍💼 " + x(uid, "فروشنده: ", "Seller: ") + esc(s["name"]) + (f" @{s['username']}" if s.get("username") else "")
            txt += f"\n   🆔 <code>{s['id']}</code>  📞 <code>{esc(s['phone'])}</code>\n"
            rows.append([(f"🧑‍💼 {s['name']}", tg_link(s))])
        if gs("support_extra"): txt += "\n" + esc(gs("support_extra"))
        return ui(uid, txt, K(*(rows + [back_row(uid)])), call)
    if d == "tk":
        st_set(uid, {"s": "ticket"})
        return ui(uid, x(uid, "✉️ پیامت را بنویس تا برای پشتیبانی ارسال شود:", "✉️ Type your message and it will be sent to support:"),
                  K([(x(uid, "❌ انصراف", "❌ Cancel"), "cancel")]), call)
    if d == "gd":
        g = gs("guide") or x(uid,
            "📚 <b>آموزش اتصال</b>\n\n1️⃣ کانفیگ را از «اشتراک‌های من» کپی کن.\n2️⃣ یکی از برنامه‌ها را نصب کن:\n"
            "• اندروید: v2rayNG / Hiddify\n• iOS: Streisand / V2Box / Shadowrocket\n• ویندوز: v2rayN / Hiddify\n• مک: V2Box / Hiddify\n"
            "3️⃣ در برنامه گزینه Import from clipboard را بزن و وصل شو ✅",
            "📚 <b>Connection guide</b>\n\n1️⃣ Copy your config from “My subscriptions”.\n2️⃣ Install an app:\n"
            "• Android: v2rayNG / Hiddify\n• iOS: Streisand / V2Box / Shadowrocket\n• Windows: v2rayN / Hiddify\n• Mac: V2Box / Hiddify\n"
            "3️⃣ Tap “Import from clipboard” and connect ✅")
        return ui(uid, g, K(back_row(uid)), call)

    # ── staff areas ──
    if p[0] == "o" and is_staff(uid): return order_action(call, uid, p)
    if p[0] == "a" and is_staff(uid): return admin_route(call, uid, p)
    if p[0] == "u" and is_admin(uid): return user_action(call, uid, p)


# ═════════════════════════ Orders ═════════════════════════
def get_order(oid):
    r = sel("orders", id=oid)
    return r[0] if r else None


def order_text(uid, o, u):
    t = x(uid, "🧾 <b>سفارش #{id}</b>  {st}\n👤 {n} {un} <code>{uid}</code>\n📦 {p} • {tr}\n💵 {pr} تومان\n🕒 {t}",
          "🧾 <b>Order #{id}</b>  {st}\n👤 {n} {un} <code>{uid}</code>\n📦 {p} • {tr}\n💵 {pr} T\n🕒 {t}")
    t = t.format(id=o["id"], st=stl(uid, o["status"]), n=esc(u.get("name")), un="@" + u["username"] if u.get("username") else "",
                 uid=o["user_id"], p=esc(o["plan_name"]), tr=traffic(uid, o), pr=money(o["price"]), t=fdate(o["created_at"]))
    if o.get("receipt_text"): t += "\n📝 " + esc(o["receipt_text"][:300])
    return t


def send_order_card(to, o):
    u = user(o["user_id"]) or {}
    cap, kb = order_text(to, o, u), None
    if o["status"] == "pending":
        kb = K([(x(to, "✅ پرداخت شد", "✅ Paid"), f"o:ok:{o['id']}"), (x(to, "❌ رد", "❌ Reject"), f"o:no:{o['id']}")])
    elif o["status"] == "paid":
        kb = K([(x(to, "📤 ارسال کانفیگ", "📤 Send config"), f"o:cfg:{o['id']}")])
    try:
        t, fid = o.get("receipt_type"), o.get("receipt_file_id")
        if t == "photo" and fid: bot.send_photo(to, fid, caption=cap, reply_markup=kb)
        elif t == "document" and fid: bot.send_document(to, fid, caption=cap, reply_markup=kb)
        else: send(to, cap, kb)
    except Exception as e:
        log.warning("card send failed %s: %s", to, e)


def notify_staff(o):
    for sid in staff_ids(): send_order_card(sid, o)


def order_action(call, uid, p):
    act, oid = p[1], int(p[2])
    o = get_order(oid)
    if not o: return
    def strip():
        try: bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except Exception: pass
    def handled():
        cur = get_order(oid)
        send(uid, x(uid, f"ℹ️ سفارش #{oid} قبلاً بررسی شده: {stl(uid, cur['status'])}", f"ℹ️ Order #{oid} already handled: {stl(uid, cur['status'])}"))
    if act == "v": return send_order_card(uid, o)
    if act == "ok":
        if not claim(oid, "pending", "paid", approved_by=uid): strip(); return handled()
        strip()
        st_set(uid, {"s": "deliver", "oid": oid})
        try: send(o["user_id"], x(o["user_id"], f"✅ پرداخت سفارش #{oid} تأیید شد. کانفیگ تا لحظاتی دیگر ارسال می‌شود…",
                                  f"✅ Payment for order #{oid} confirmed. Your config is coming shortly…"))
        except Exception: pass
        send(uid, x(uid, f"✍️ کانفیگ سفارش <b>#{oid}</b> ({esc(o['plan_name'])}) را بفرست (متن/لینک ساب).\nبرای لغو /start",
                    f"✍️ Send the config for order <b>#{oid}</b> ({esc(o['plan_name'])}) (text / sub link).\n/start to cancel"))
    elif act == "cfg":
        if o["status"] != "paid": return handled()
        st_set(uid, {"s": "deliver", "oid": oid})
        send(uid, x(uid, f"✍️ کانفیگ سفارش <b>#{oid}</b> را بفرست:", f"✍️ Send config for order <b>#{oid}</b>:"))
    elif act == "no":
        if not claim(oid, "pending", "rejected", approved_by=uid): strip(); return handled()
        strip()
        st_set(uid, {"s": "reason", "oid": oid})
        send(uid, x(uid, f"❌ سفارش #{oid} رد شد.\nدلیل را برای مشتری بنویس (یا بفرست <code>-</code> برای رد بدون دلیل):",
                    f"❌ Order #{oid} rejected.\nType the reason for the customer (or send <code>-</code> to skip):"))


def send_config(to, o, cfg):
    long_cfg = len(cfg) > 2500
    txt = x(to, "✅ <b>سفارش #{id} تحویل شد</b>\n📦 {p}\n📊 حجم: {t}\n⏳ مدت: {d}\n\n🔑 <b>کانفیگ (لمس برای کپی):</b>\n<code>{c}</code>\n\n📚 آموزش اتصال: /start ← آموزش اتصال",
            "✅ <b>Order #{id} delivered</b>\n📦 {p}\n📊 Traffic: {t}\n⏳ Duration: {d}\n\n🔑 <b>Config (tap to copy):</b>\n<code>{c}</code>\n\n📚 Guide: /start → Guide")
    send(to, txt.format(id=o["id"], p=esc(o["plan_name"]), t=traffic(to, o), d=dur(to, o),
                        c=x(to, "(طولانی است؛ در فایل زیر)", "(too long; see file below)") if long_cfg else esc(cfg)))
    if long_cfg:
        f = io.BytesIO(cfg.encode()); f.name = f"config-{o['id']}.txt"
        bot.send_document(to, f)
    else:
        q = qr_png(cfg)
        if q: bot.send_photo(to, q, caption="QR")


def deliver(o, cfg, staff):
    now = now_utc()
    exp = (now + timedelta(days=o["days"])).isoformat() if o.get("days") else None
    row = claim(o["id"], "paid", "delivered", config=cfg, delivered_at=now.isoformat(), expires_at=exp,
                approved_by=o.get("approved_by") or staff)
    if not row: return None
    try: send_config(o["user_id"], row, cfg)
    except Exception as e:
        log.warning("config send failed: %s", e)
        send(staff, x(staff, "⚠️ ارسال به مشتری ناموفق بود (شاید ربات را بلاک کرده). کانفیگ در حسابش ذخیره شد.",
                      "⚠️ Could not message the customer (maybe blocked the bot). The config is saved on their account."))
    return row


# ═════════════════════════ Stats ═════════════════════════
def stats_text(uid):
    rows = sel_all("orders", "id,price,status,approved_by,delivered_at")
    today = tdate(now_iso())
    week = now_utc() - timedelta(days=7)
    def calc(who=None):
        d = [o for o in rows if o["status"] == "delivered" and (who is None or o.get("approved_by") == who)]
        return (len(d), sum(o["price"] or 0 for o in d), sum(o["price"] or 0 for o in d if tdate(o.get("delivered_at")) == today),
                sum(o["price"] or 0 for o in d if (pdt(o.get("delivered_at") or "") or week) > week))
    n, rev, td, wk = calc(uid)
    pend = len([o for o in rows if o["status"] in ("pending", "paid")])
    t = x(uid, "📊 <b>آمار من</b>\n\n✅ تحویل‌داده: {n}\n💰 فروش من: {r} تومان\n📅 امروز: {t} تومان\n🗓 ۷ روز اخیر: {w} تومان\n⏳ در انتظار (کل): {p}",
          "📊 <b>My stats</b>\n\n✅ Delivered: {n}\n💰 My sales: {r} T\n📅 Today: {t} T\n🗓 Last 7 days: {w} T\n⏳ Pending (all): {p}").format(
          n=n, r=money(rev), t=money(td), w=money(wk), p=pend)
    if is_owner(uid):
        gn, grev, gtd, gwk = calc()
        users = len(sel_all("users", "id"))
        rej = len([o for o in rows if o["status"] == "rejected"])
        t += x(uid, f"\n\n━━ 👑 آمار کل ━━\n👥 کاربران: {users}\n✅ تحویل‌شده: {gn}\n💰 فروش کل: {money(grev)} تومان\n📅 امروز: {money(gtd)}\n🗓 ۷ روز: {money(gwk)}\n❌ ردشده: {rej}",
               f"\n\n━━ 👑 Global ━━\n👥 Users: {users}\n✅ Delivered: {gn}\n💰 Total: {money(grev)} T\n📅 Today: {money(gtd)}\n🗓 7 days: {money(gwk)}\n❌ Rejected: {rej}")
        t += "\n\n" + x(uid, "👮 <b>وضعیت ادمین‌ها و فروشنده‌ها</b>", "👮 <b>Staff status</b>")
        for s in sel("users", raw={"role": "in.(admin,seller)"}):
            c, r, tdy, _ = calc(s["id"])
            t += f"\n• {'🛡' if s['role'] == 'admin' else '🧑‍💼'} {esc(s['name'])} <code>{s['id']}</code> — {c} / {money(r)} ({x(uid, 'امروز', 'today')} {money(tdy)})"
    return t


# ═════════════════════════ Admin panel ═════════════════════════
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
        if is_owner(uid): r.append([(x(uid, "👮 ادمین‌ها و فروشنده‌ها", "👮 Admins & sellers"), "a:staff")])
    r.append([(x(uid, "🔼 بستن پنل مدیریت", "🔼 Collapse"), "a:close"), (x(uid, "🏠 منوی اصلی", "🏠 Main"), "p:main")])
    return K(*r)


def admin_route(call, uid, p):
    a = p[1]
    back = K(back_row(uid, "a:main"))
    if a in ("open", "main"):
        st_clear(uid)
        return ui(uid, x(uid, f"🛠 <b>پنل مدیریت</b> ({role(uid)})", f"🛠 <b>Admin panel</b> ({role(uid)})"), admin_kb(uid), call)
    if a == "close":
        return ui(uid, x(uid, "🛠 پنل مدیریت بسته است.", "🛠 Admin panel collapsed."),
                  K([(x(uid, "🔽 باز کردن پنل مدیریت", "🔽 Open admin panel"), "a:open")]), call)
    if a == "stats": return ui(uid, stats_text(uid), back, call)
    if a == "pend":
        rows = sel("orders", raw={"status": "in.(pending,paid)"}, order="id.asc", limit=40)
        if not rows: return ui(uid, x(uid, "✅ سفارش در انتظاری نیست.", "✅ No pending orders."), back, call)
        b = [[(f"#{o['id']} {'💳' if o['status'] == 'paid' else '⏳'} {o['plan_name']} • {money(o['price'])}", f"o:v:{o['id']}")] for o in rows] + [back_row(uid, "a:main")]
        return ui(uid, x(uid, "⏳ <b>سفارش‌های در انتظار</b>\n⏳ = رسید بررسی نشده | 💳 = منتظر کانفیگ", "⏳ <b>Pending orders</b>\n⏳ = receipt to review | 💳 = waiting config"), K(*b), call)

    if not is_admin(uid): return   # everything below: admin/owner only
    if a == "shop":
        ss("shop_open", "0" if gs("shop_open", "1") == "1" else "1")
        return ui(uid, x(uid, "🛠 <b>پنل مدیریت</b>", "🛠 <b>Admin panel</b>"), admin_kb(uid), call)
    if a == "plans":
        b, txt = [], x(uid, "📦 <b>سرویس‌ها</b>\n\n", "📦 <b>Plans</b>\n\n")
        cmap = {c["id"]: c["name"] for c in sel("categories")}
        for pl in sel("plans", order="price.asc"):
            txt += ("🟢 " if pl["active"] else "⚪️ ") + plan_line(uid, pl) + (f"\n   📁 {esc(cmap[pl['category_id']])}" if pl.get("category_id") in cmap else "") + "\n\n"
            b.append([(f"{'🔕' if pl['active'] else '🔔'} {pl['name'][:18]}", f"a:pt:{pl['id']}"), ("✏️", f"a:pe:{pl['id']}"), ("🗑", f"a:pd:{pl['id']}")])
        b += [[(x(uid, "➕ افزودن سرویس", "➕ Add plan"), "a:pa"), (x(uid, "📁 دسته‌بندی‌ها", "📁 Categories"), "a:cats")], back_row(uid, "a:main")]
        return ui(uid, txt, K(*b), call)
    fmt_fa = "<code>نام | حجم‌GB | روز | قیمت‌تومان</code>\nمثال:\n<code>سرویس ۵۰ گیگ یک‌ماهه | 50 | 30 | 150000</code>\n(حجم یا روز: عدد 0 یا کلمهٔ «نامحدود» = نامحدود)"
    fmt_en = "<code>name | GB | days | price</code>\nExample:\n<code>50GB Monthly | 50 | 30 | 150000</code>\n(GB or days: 0 or the word “unlimited” = unlimited)"
    if a in ("pa", "pac"):
        allc = sel("categories", order="id.asc")
        if a == "pa" and allc:
            rows = [[(f"📁 {c['name']}", f"a:pac:{c['id']}")] for c in allc] + [[(x(uid, "📂 بدون دسته", "📂 No category"), "a:pac:0")], back_row(uid, "a:plans")]
            return ui(uid, x(uid, "📁 سرویس در کدام دسته قرار بگیرد؟", "📁 Which category should the plan go in?"), K(*rows), call)
        st_set(uid, {"s": "addplan", "cat": (int(p[2]) or None) if a == "pac" else None})
        return ui(uid, x(uid, "➕ در یک پیام بفرست:\n" + fmt_fa, "➕ Send in one message:\n" + fmt_en), K(back_row(uid, "a:plans")), call)
    if a == "pe":
        st_set(uid, {"s": "editplan", "pid": int(p[2])})
        allc = sel("categories", order="id.asc")
        mv = ([[(x(uid, "📁 انتقال به: ", "📁 Move to: ") + c["name"], f"a:pm:{p[2]}:{c['id']}")] for c in allc]
              + [[(x(uid, "📂 انتقال به: بدون دسته", "📂 Move to: no category"), f"a:pm:{p[2]}:0")]]) if allc else []
        return ui(uid, x(uid, "✏️ مشخصات جدید را بفرست:\n" + fmt_fa, "✏️ Send the new details:\n" + fmt_en), K(*(mv + [back_row(uid, "a:plans")])), call)
    if a == "pm":
        upd("plans", {"id": int(p[2])}, {"category_id": int(p[3]) or None})
        st_clear(uid)
        return admin_route(call, uid, ["a", "plans"])
    # ── categories ──
    if a == "cats":
        plans_all = sel("plans")
        b, txt = [], x(uid, "📁 <b>دسته‌بندی‌ها</b>\nمثلاً: سرور معمولی، گیمینگ\n\n", "📁 <b>Categories</b>\nE.g. Standard, Gaming\n\n")
        for c in sel("categories", order="id.asc"):
            n = len([pl for pl in plans_all if pl.get("category_id") == c["id"]])
            txt += f"{'🟢' if c['active'] else '⚪️'} {esc(c['name'])} ({n})\n"
            b.append([(f"{'🔕' if c['active'] else '🔔'} {c['name'][:14]}", f"a:ct:{c['id']}"), ("✏️", f"a:ce:{c['id']}"), ("🗑", f"a:cd:{c['id']}")])
        b += [[(x(uid, "➕ افزودن دسته", "➕ Add category"), "a:cadd")], back_row(uid, "a:plans")]
        return ui(uid, txt, K(*b), call)
    if a == "cadd":
        st_set(uid, {"s": "cadd"})
        return ui(uid, x(uid, "📁 نام دسته را بفرست (مثلاً: گیمینگ):", "📁 Send the category name (e.g. Gaming):"), K(back_row(uid, "a:cats")), call)
    if a == "ce":
        st_set(uid, {"s": "cedit", "cid": int(p[2])})
        return ui(uid, x(uid, "✏️ نام جدید دسته را بفرست:", "✏️ Send the new category name:"), K(back_row(uid, "a:cats")), call)
    if a == "ct":
        c = sel("categories", id=int(p[2]))
        if c: upd("categories", {"id": c[0]["id"]}, {"active": not c[0]["active"]})
        return admin_route(call, uid, ["a", "cats"])
    if a == "cd":
        return ui(uid, x(uid, "🗑 این دسته حذف شود؟ سرویس‌های داخلش حذف نمی‌شوند و «بدون دسته» می‌شوند.", "🗑 Delete this category? Its plans are kept and become uncategorized."),
                  K([(x(uid, "✅ بله، حذف", "✅ Yes, delete"), f"a:cdy:{p[2]}"), (x(uid, "❌ خیر", "❌ No"), "a:cats")]), call)
    if a == "cdy":
        upd("plans", {"category_id": int(p[2])}, {"category_id": None})
        rm("categories", {"id": int(p[2])})
        return admin_route(call, uid, ["a", "cats"])
    if a == "pt":
        pl = sel("plans", id=int(p[2]))
        if pl: upd("plans", {"id": pl[0]["id"]}, {"active": not pl[0]["active"]})
        return admin_route(call, uid, ["a", "plans"])
    if a == "pd":
        return ui(uid, x(uid, "🗑 این سرویس حذف شود؟ (سفارش‌های قبلی حفظ می‌شوند)", "🗑 Delete this plan? (past orders are kept)"),
                  K([(x(uid, "✅ بله، حذف", "✅ Yes, delete"), f"a:pdy:{p[2]}"), (x(uid, "❌ خیر", "❌ No"), "a:plans")]), call)
    if a == "pdy":
        rm("plans", {"id": int(p[2])})
        return admin_route(call, uid, ["a", "plans"])
    if a == "card":
        st_set(uid, {"s": "card"})
        return ui(uid, x(uid, f"💳 کارت فعلی: <code>{esc(gs('card_number', '-'))}</code> — {esc(gs('card_holder', '-'))}\n\nکارت جدید را بفرست:\n<code>شماره کارت | نام صاحب کارت</code>",
                         f"💳 Current: <code>{esc(gs('card_number', '-'))}</code> — {esc(gs('card_holder', '-'))}\n\nSend new card:\n<code>card number | holder name</code>"), back, call)
    if a == "ch":
        chs = get_channels()
        b = [[(f"🗑 {c.get('title') or c['chat_id']}", f"a:chd:{c['id']}")] for c in chs] + [[(x(uid, "➕ افزودن کانال", "➕ Add channel"), "a:cha")], back_row(uid, "a:main")]
        return ui(uid, x(uid, "📢 <b>کانال‌های اجباری</b>\n(ربات باید در کانال ادمین باشد)", "📢 <b>Forced channels</b>\n(the bot must be admin there)"), K(*b), call)
    if a == "cha":
        st_set(uid, {"s": "addch"})
        return ui(uid, x(uid, "➕ یوزرنیم کانال را بفرست: <code>@mychannel</code>\nکانال خصوصی: <code>-100123456789 https://t.me/+invite</code>",
                         "➕ Send channel username: <code>@mychannel</code>\nPrivate: <code>-100123456789 https://t.me/+invite</code>"), K(back_row(uid, "a:ch")), call)
    if a == "chd":
        rm("channels", {"id": int(p[2])})
        CH_CACHE[0] = None
        JOINED.clear()
        return admin_route(call, uid, ["a", "ch"])
    if a == "users":
        st_set(uid, {"s": "lookup"})
        n = len(sel_all("users", "id"))
        return ui(uid, x(uid, f"👥 تعداد کاربران: <b>{n}</b>\n\nآیدی عددی یا @یوزرنیم کاربر را بفرست:", f"👥 Total users: <b>{n}</b>\n\nSend a numeric ID or @username:"), back, call)
    if a == "bc":
        st_set(uid, {"s": "bc"})
        return ui(uid, x(uid, "📣 پیام همگانی را بفرست (متن/عکس/…):", "📣 Send the broadcast message (text/photo/…):"), back, call)
    if a == "set":
        return ui(uid, x(uid, "⚙️ <b>تنظیمات</b>", "⚙️ <b>Settings</b>"), K(
            [(x(uid, "👋 متن خوش‌آمدگویی", "👋 Welcome text"), "a:sw:welcome")], [(x(uid, "📚 متن آموزش", "📚 Guide text"), "a:sw:guide")],
            [(x(uid, "🆘 متن اضافه پشتیبانی", "🆘 Extra support text"), "a:sw:support_extra")], back_row(uid, "a:main")), call)
    if a == "sw":
        st_set(uid, {"s": "set", "key": p[2]})
        return ui(uid, x(uid, "✍️ متن جدید را بفرست (HTML ساده مجاز است؛ برای پاک کردن بنویس: -)", "✍️ Send the new text (simple HTML allowed; send - to clear)"), K(back_row(uid, "a:set")), call)
    if a == "staff" and is_owner(uid):
        b, txt = [], x(uid, "👮 <b>ادمین‌ها و فروشنده‌ها</b>\n\n", "👮 <b>Admins & sellers</b>\n\n")
        for s in sel("users", raw={"role": "in.(admin,seller)"}):
            txt += f"{'🛡' if s['role'] == 'admin' else '🧑‍💼'} {esc(s['name'])} <code>{s['id']}</code>" + (f" 📞 {esc(s['phone'])}" if s.get("phone") else (" ⚠️ no phone" if s["role"] == "seller" else "")) + "\n"
            b.append([(f"🗑 {s['name'] or s['id']}", f"a:srm:{s['id']}")])
        b += [[(x(uid, "➕ ادمین", "➕ Admin"), "a:sadd:admin"), (x(uid, "➕ فروشنده", "➕ Seller"), "a:sadd:seller")],
              [(x(uid, "📊 وضعیت و آمار", "📊 Status & stats"), "a:stats")], back_row(uid, "a:main")]
        return ui(uid, txt, K(*b), call)
    if a == "sadd" and is_owner(uid):
        st_set(uid, {"s": "addstaff", "role": p[2]})
        return ui(uid, x(uid, "🆔 آیدی عددی شخص را بفرست (آیدی را با /id در ربات می‌توان دید):", "🆔 Send the person's numeric ID (they can get it with /id):"), K(back_row(uid, "a:staff")), call)
    if a == "srm" and is_owner(uid):
        save_user(int(p[2]), role="user")
        return admin_route(call, uid, ["a", "staff"])


def show_user(uid, t):
    orders = sel("orders", user_id=t["id"], status="delivered")
    txt = x(uid, "👤 <b>{n}</b> {un}\n🆔 <code>{id}</code>\n🎖 {r}\n📞 {ph}\n📅 {j}\n✅ خریدها: {c} | 💰 {s} تومان\n🚫 مسدود: {b}",
            "👤 <b>{n}</b> {un}\n🆔 <code>{id}</code>\n🎖 {r}\n📞 {ph}\n📅 {j}\n✅ Purchases: {c} | 💰 {s} T\n🚫 Banned: {b}").format(
            n=esc(t["name"]), un="@" + esc(t["username"]) if t.get("username") else "", id=t["id"], r=t["role"], ph=esc(t.get("phone") or "-"),
            j=fdate(t["joined_at"]), c=len(orders), s=money(sum(o["price"] or 0 for o in orders)), b="✅" if t.get("banned") else "❌")
    ban = (x(uid, "✅ رفع مسدودی", "✅ Unban") if t.get("banned") else x(uid, "🚫 مسدود کردن", "🚫 Ban"))
    send(uid, txt, K([(ban, f"u:ban:{t['id']}")], [(x(uid, "📨 پیام به کاربر", "📨 Message user"), f"u:msg:{t['id']}"),
                                                 (x(uid, "🎁 ارسال کانفیگ دستی", "🎁 Send config manually"), f"u:cfg:{t['id']}")]))


def user_action(call, uid, p):
    act, tid = p[1], int(p[2])
    if act == "ban":
        t = user(tid)
        if tid == OWNER_ID or not t: return
        save_user(tid, banned=not t.get("banned"))
        return show_user(uid, user(tid))
    if act == "msg":
        st_set(uid, {"s": "umsg", "uid": tid})
        send(uid, x(uid, "✍️ پیام را بفرست:", "✍️ Send the message:"))
    if act == "cfg":
        st_set(uid, {"s": "ucfg", "uid": tid})
        send(uid, x(uid, "🎁 فرمت:\nخط اول: <code>نام سرویس | روز | حجمGB</code> (0 یا نامحدود = نامحدود)\nخط‌های بعد: کانفیگ", "🎁 Format:\nFirst line: <code>plan name | days | GB</code>\nNext lines: the config"))


# ═════════════════════════ Message states ═════════════════════════
def do_broadcast(uid, m):
    users = [u for u in sel_all("users", "id,banned") if not u.get("banned")]
    ok = 0
    for u in users:
        try:
            bot.copy_message(u["id"], m.chat.id, m.message_id)
            ok += 1
        except Exception: pass
        time.sleep(0.05)
    send(uid, x(uid, f"📣 ارسال شد: {ok}/{len(users)}", f"📣 Sent: {ok}/{len(users)}"))


@bot.message_handler(content_types=["text", "photo", "document"])
def h_msg(m):
    uid = m.from_user.id
    u = ensure_user(m.from_user)
    if not u: return db_error(uid)
    st = st_get(uid)
    txt = (m.text or "").strip()
    # Fallback: receipt arrived after a restart and no state survived
    if not st and (m.photo or m.document):
        w = sel("orders", user_id=uid, status="waiting_receipt", order="id.desc", limit=1)
        if w: st = {"s": "receipt", "oid": w[0]["id"]}
    if not st:
        if u.get("lang") and passes(uid): show_main(uid)
        return
    s = st["s"]
    if not u.get("lang") or not passes(uid): return
    try:
        if s == "receipt":
            if m.photo: data = {"receipt_type": "photo", "receipt_file_id": m.photo[-1].file_id}
            elif m.document: data = {"receipt_type": "document", "receipt_file_id": m.document.file_id}
            elif txt: data = {"receipt_type": "text", "receipt_text": txt[:500]}
            else: return
            row = claim(st["oid"], "waiting_receipt", "pending", **data)
            st_clear(uid)
            if not row: return send(uid, x(uid, "ℹ️ این سفارش دیگر فعال نیست؛ از منو دوباره سفارش بده.", "ℹ️ This order is no longer active; please order again."))
            send(uid, x(uid, f"✅ رسید سفارش #{row['id']} ارسال شد. پس از تأیید، کانفیگ برایت فرستاده می‌شود 🙏",
                        f"✅ Receipt for order #{row['id']} sent. Your config will arrive after approval 🙏"), K([(x(uid, "🏠 منوی اصلی", "🏠 Main"), "p:main")]))
            return notify_staff(row)
        if s == "ticket":
            st_clear(uid)
            for aid in admin_ids():
                try:
                    send(aid, f"📩 <b>{esc(u['name'])}</b> {'@' + u['username'] if u.get('username') else ''} <code>{uid}</code>",
                         K([(x(aid, "↩️ پاسخ", "↩️ Reply"), f"u:msg:{uid}")]))
                    bot.copy_message(aid, m.chat.id, m.message_id)
                except Exception: pass
            return send(uid, x(uid, "✅ پیامت برای پشتیبانی ارسال شد.", "✅ Your message was sent to support."), K([(x(uid, "🏠 منوی اصلی", "🏠 Main"), "p:main")]))
        if not is_staff(uid): return
        if s == "deliver":
            if not txt: return
            o = get_order(st["oid"])
            st_clear(uid)
            if o and deliver(o, txt, uid):
                send(uid, x(uid, f"✅ کانفیگ سفارش #{o['id']} برای مشتری ارسال شد.", f"✅ Config for order #{o['id']} delivered."))
            else:
                send(uid, x(uid, "ℹ️ این سفارش قبلاً تحویل شده یا فعال نیست.", "ℹ️ This order was already delivered or is not active."))
            return
        if s == "reason":
            st_clear(uid)
            o = get_order(st["oid"])
            if o and txt and txt != "-":
                try: send(o["user_id"], x(o["user_id"], f"❌ رسید سفارش #{o['id']} تأیید نشد.\n📝 دلیل: {esc(txt)}\nدر صورت اشتباه با پشتیبانی تماس بگیرید.",
                                          f"❌ Receipt for order #{o['id']} was rejected.\n📝 Reason: {esc(txt)}\nContact support if this is a mistake."))
                except Exception: pass
            elif o:
                try: send(o["user_id"], x(o["user_id"], f"❌ رسید سفارش #{o['id']} تأیید نشد. در صورت اشتباه با پشتیبانی تماس بگیرید.",
                                          f"❌ Receipt for order #{o['id']} was rejected. Contact support if this is a mistake."))
                except Exception: pass
            return send(uid, "✅")
        if not is_admin(uid): return
        if s in ("addplan", "editplan"):
            n, g, d, pr = [i.strip() for i in txt.split("|")]
            row = {"name": n, "traffic_gb": num(g), "days": num(d), "price": int(dig(pr))}
            if s == "addplan":
                if st.get("cat"): row["category_id"] = st["cat"]
                ins("plans", row)
            else:
                upd("plans", {"id": st["pid"]}, row)
            st_clear(uid)
            send(uid, x(uid, "✅ ذخیره شد.", "✅ Saved."), K(back_row(uid, "a:plans")))
        elif s in ("cadd", "cedit"):
            if not txt: return
            if s == "cadd": ins("categories", {"name": txt[:40]})
            else: upd("categories", {"id": st["cid"]}, {"name": txt[:40]})
            st_clear(uid)
            send(uid, "✅", K(back_row(uid, "a:cats")))
        elif s == "card":
            n, h = [i.strip() for i in txt.split("|", 1)]
            ss("card_number", n); ss("card_holder", h)
            st_clear(uid)
            send(uid, "✅", K(back_row(uid, "a:main")))
        elif s == "addch":
            parts = txt.split()
            cid = parts[0]
            chat = bot.get_chat(int(cid) if cid.lstrip("-").isdigit() else cid)
            link = parts[1] if len(parts) > 1 else ("https://t.me/" + cid[1:] if cid.startswith("@") else "")
            try: ins("channels", {"chat_id": cid, "title": chat.title or cid, "link": link})
            except DBError: return send(uid, x(uid, "ℹ️ این کانال قبلاً اضافه شده.", "ℹ️ This channel is already added."))
            CH_CACHE[0] = None; JOINED.clear()
            st_clear(uid)
            send(uid, x(uid, f"✅ کانال «{esc(chat.title)}» افزوده شد.", f"✅ Channel “{esc(chat.title)}” added."), K(back_row(uid, "a:ch")))
        elif s == "lookup":
            t = sel("users", username=txt.lstrip("@")) if txt.startswith("@") else sel("users", id=int(txt))
            if not t: return send(uid, x(uid, "❌ کاربر پیدا نشد.", "❌ User not found."))
            st_clear(uid)
            show_user(uid, t[0])
        elif s == "umsg":
            bot.copy_message(st["uid"], m.chat.id, m.message_id)
            st_clear(uid)
            send(uid, "✅")
        elif s == "ucfg":
            first, _, cfg = txt.partition("\n")
            parts = [i.strip() for i in first.split("|")]
            if not cfg.strip(): raise ValueError("config is empty")
            o = ins("orders", {"user_id": st["uid"], "plan_name": parts[0], "days": num(parts[1]) if len(parts) > 1 else 30,
                               "traffic_gb": num(parts[2]) if len(parts) > 2 else 0, "price": 0, "status": "paid", "approved_by": uid})
            deliver(o, cfg.strip(), uid)
            st_clear(uid)
            send(uid, "✅")
        elif s == "bc":
            st_clear(uid)
            threading.Thread(target=do_broadcast, args=(uid, m), daemon=True).start()
            send(uid, x(uid, "📣 ارسال شروع شد…", "📣 Broadcasting…"))
        elif s == "set":
            ss(st["key"], "" if txt == "-" else txt)
            st_clear(uid)
            send(uid, "✅", K(back_row(uid, "a:set")))
        elif s == "addstaff" and is_owner(uid):
            tid = int(txt)
            if not user(tid): ins("users", {"id": tid, "name": "", "username": "", "role": st["role"]})
            else: save_user(tid, role=st["role"])
            UC.pop(tid, None)
            st_clear(uid)
            send(uid, "✅", K(back_row(uid, "a:staff")))
            try:
                send(tid, x(tid, f"🎉 شما به عنوان <b>{st['role']}</b> منصوب شدید. /start", f"🎉 You are now <b>{st['role']}</b>. /start"))
                if st["role"] == "seller" and not (user(tid) or {}).get("phone"): ask_phone(tid)
            except Exception:
                send(uid, x(uid, "ℹ️ هنوز نمی‌توانم به او پیام بدهم؛ باید یک‌بار ربات را /start کند.", "ℹ️ I can't message them yet; they must /start the bot once."))
    except DBError as e:
        log.warning("state %s db error: %s", s, e)
        send(uid, x(uid, f"⚠️ خطای پایگاه داده: {esc(e)}", f"⚠️ Database error: {esc(e)}"))
    except Exception as e:
        log.warning("state %s failed: %s", s, e)
        send(uid, x(uid, f"⚠️ ورودی نامعتبر یا خطا: {esc(e)}\nفرمت را بررسی کن یا /start بزن.", f"⚠️ Invalid input or error: {esc(e)}\nCheck the format or send /start."))


# ═════════════════════════ Flask / webhook ═════════════════════════
SEEN = OrderedDict()


@app.route("/")
def index(): return "OK"


@app.route("/health")
def health(): return "alive"


@app.route("/webhook/<token>", methods=["POST"])
def webhook(token):
    if token != HOOK or request.headers.get("X-Telegram-Bot-Api-Secret-Token") != HOOK: abort(403)
    try:
        upd_ = telebot.types.Update.de_json(request.get_data().decode("utf-8"))
        if upd_.update_id in SEEN: return "ok", 200     # Telegram retry -> ignore duplicate
        SEEN[upd_.update_id] = 1
        while len(SEEN) > 2000: SEEN.popitem(last=False)
        bot.process_new_updates([upd_])
    except Exception as e:
        log.exception("update error: %s", e)
    return "ok", 200


def init_webhook():
    if not BASE_URL: return
    try:
        bot.remove_webhook()
        time.sleep(0.5)
        bot.set_webhook(url=f"{BASE_URL}/webhook/{HOOK}", secret_token=HOOK, max_connections=40)
        log.info("Webhook set: %s", BASE_URL)
    except Exception as e:
        log.error("set_webhook failed: %s", e)


def keepalive():
    """Pings our own public URL so Render's free tier doesn't spin down."""
    while True:
        time.sleep(540)
        try: requests.get(BASE_URL + "/health", timeout=20)
        except Exception: pass


init_webhook()
if BASE_URL and os.environ.get("KEEPALIVE", "1") == "1":
    threading.Thread(target=keepalive, daemon=True).start()

if __name__ == "__main__":
    if BASE_URL:
        app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
    else:   # local test with polling
        bot.remove_webhook()
        bot.infinity_polling()
