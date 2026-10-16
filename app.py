"""
PIXEL WARS — кланы воюют за общий холст. Mini App + бот + оплата Telegram Stars.
Один процесс: бот (polling) + веб-сервер (витрина и API).

Переменные: BOT_TOKEN (обязательно), ADMIN_ID, WEBAPP_URL (в Codespaces определяется сам),
PORT (8080), COOLDOWN (секунд между пикселями, 30), DB_PATH.
"""
import asyncio, base64, hashlib, hmac, json, logging, os, random, sqlite3, string, subprocess, time
from array import array
from collections import Counter
from contextlib import closing
from urllib.parse import parse_qsl

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice,
                           MenuButtonWebApp, Message, PreCheckoutQuery, WebAppInfo)
from aiohttp import web

TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
PORT = int(os.getenv("PORT", "8080"))
DB_PATH = os.getenv("DB_PATH", "data/pixelwar.db")
COOLDOWN = int(os.getenv("COOLDOWN", "30"))
W = H = 96
SHIELD_SEC = 3600
# предмет: (звёзды, количество, название, описание) — цены можно менять
ITEMS = {
    "skip": (15, 10, "⚡ Турбо", "10 пикселей без ожидания"),
    "brush": (25, 3, "💣 Кисть 3×3", "3 заливки квадратом 3×3"),
    "shield": (50, 1, "🛡 Щит на час", "Защитит область 11×11 от чужих пикселей"),
}


def public_url() -> str:
    u = os.getenv("WEBAPP_URL", "").rstrip("/")
    n, d = os.getenv("CODESPACE_NAME"), os.getenv("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN")
    return u or (f"https://{n}-{PORT}.{d}" if n and d else "")


WEBAPP_URL = public_url()
bot: Bot | None = None
BOT_USERNAME = ""
colors = bytearray(W * H)          # 0 = пусто, 1..16 = цвет палитры
owners = array("H", [0] * (W * H))  # id клана-владельца пикселя
ver, dirty = 1, False
_stand = (0.0, None)


class GameError(Exception):
    pass


# ---------------- База ----------------
def db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    global colors, owners
    with closing(db()) as c, c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT, clan INTEGER DEFAULT 0,
            last REAL DEFAULT 0, skip INTEGER DEFAULT 0, brush INTEGER DEFAULT 0,
            shield INTEGER DEFAULT 0, placed INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS clans(id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE, name TEXT);
        CREATE TABLE IF NOT EXISTS shields(x0 INT, y0 INT, x1 INT, y1 INT, until REAL, clan INT);
        CREATE TABLE IF NOT EXISTS canvas(id INTEGER PRIMARY KEY, colors BLOB, owners BLOB);
        CREATE TABLE IF NOT EXISTS paid(charge TEXT PRIMARY KEY);""")
        row = c.execute("SELECT colors, owners FROM canvas WHERE id=1").fetchone()
    if row and len(row["colors"]) == W * H:
        colors = bytearray(row["colors"])
        owners = array("H")
        owners.frombytes(row["owners"])


def save_canvas():
    global dirty
    with closing(db()) as c, c:
        c.execute("INSERT OR REPLACE INTO canvas VALUES(1,?,?)", (bytes(colors), owners.tobytes()))
    dirty = False


# ---------------- Авторизация Telegram ----------------
def check_init(init: str, token: str = "", max_age: int = 86400):
    """Проверка подписи initData (https://core.telegram.org/bots/webapps#validating-data)."""
    token = token or TOKEN
    pairs = dict(parse_qsl(init, keep_blank_values=True))
    got = pairs.pop("hash", "")
    data = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    ok = hmac.compare_digest(hmac.new(key, data.encode(), hashlib.sha256).hexdigest(), got)
    if not ok or time.time() - int(pairs.get("auth_date", "0") or 0) > max_age:
        return None
    try:
        return json.loads(pairs["user"])
    except (KeyError, ValueError):
        return None


def auth(request):
    init = request.headers.get("X-Init", "")
    if not init and os.getenv("DEV") == "1":
        return {"id": 1, "first_name": "Dev"}
    user = check_init(init) if init else None
    if not user:
        raise web.HTTPUnauthorized(text=json.dumps({"error": "Откройте игру из Telegram"}),
                                   content_type="application/json")
    return user


# ---------------- Игровая логика ----------------
def ensure_user(uid: int, name: str = ""):
    with closing(db()) as c, c:
        c.execute("INSERT INTO users(id, name) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name",
                  (uid, (name or "Игрок")[:30]))
        return c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def make_clan(uid: int, name: str) -> str:
    name = " ".join(name.split())[:20]
    if len(name) < 2:
        raise GameError("Название клана: от 2 символов")
    code = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    with closing(db()) as c, c:
        cid = c.execute("INSERT INTO clans(code, name) VALUES(?,?)", (code, name)).lastrowid
        c.execute("UPDATE users SET clan=? WHERE id=?", (cid, uid))
    return code


def join_clan(uid: int, code: str) -> str:
    with closing(db()) as c, c:
        row = c.execute("SELECT id, name FROM clans WHERE code=?", (code.strip().upper(),)).fetchone()
        if not row:
            raise GameError("Клан с таким кодом не найден")
        c.execute("UPDATE users SET clan=? WHERE id=?", (row["id"], uid))
    return row["name"]


def do_place(uid: int, x: int, y: int, col: int, size: int = 1, now: float | None = None) -> int:
    global ver, dirty
    now = now or time.time()
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in (x, y, col, size)) \
            or not (0 <= x < W and 0 <= y < H and 1 <= col <= 16) or size not in (1, 3):
        raise GameError("Неверные данные")
    with closing(db()) as c, c:   # при GameError транзакция откатывается целиком
        u = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not u or not u["clan"]:
            raise GameError("Сначала вступите в клан")
        if size == 3:
            if u["brush"] < 1:
                raise GameError("Нет кистей — загляните в магазин")
            c.execute("UPDATE users SET brush=brush-1 WHERE id=?", (uid,))
            cells = [(x + i, y + j) for i in (-1, 0, 1) for j in (-1, 0, 1)]
        else:
            if now - u["last"] < COOLDOWN:
                if u["skip"] < 1:
                    raise GameError("Перезарядка")
                c.execute("UPDATE users SET skip=skip-1 WHERE id=?", (uid,))
            cells = [(x, y)]
        sh = c.execute("SELECT * FROM shields WHERE until>? AND clan!=?", (now, u["clan"])).fetchall()
        hit = [(cx, cy) for cx, cy in cells if 0 <= cx < W and 0 <= cy < H
               and not any(s["x0"] <= cx <= s["x1"] and s["y0"] <= cy <= s["y1"] for s in sh)]
        if not hit:
            raise GameError("Эта область под щитом")
        for cx, cy in hit:
            colors[cy * W + cx] = col
            owners[cy * W + cx] = u["clan"]
        c.execute("UPDATE users SET last=?, placed=placed+? WHERE id=?",
                  (now if size == 1 else u["last"], len(hit), uid))
    ver += 1
    dirty = True
    return len(hit)


def do_shield(uid: int, x: int, y: int, now: float | None = None):
    global ver
    now = now or time.time()
    if not (isinstance(x, int) and isinstance(y, int) and 0 <= x < W and 0 <= y < H):
        raise GameError("Неверные данные")
    with closing(db()) as c, c:
        u = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not u or not u["clan"]:
            raise GameError("Сначала вступите в клан")
        if u["shield"] < 1:
            raise GameError("Нет щитов — загляните в магазин")
        c.execute("UPDATE users SET shield=shield-1 WHERE id=?", (uid,))
        c.execute("INSERT INTO shields VALUES(?,?,?,?,?,?)",
                  (max(0, x - 5), max(0, y - 5), min(W - 1, x + 5), min(H - 1, y + 5), now + SHIELD_SEC, u["clan"]))
    ver += 1


def credit(uid: int, item: str, charge: str) -> bool:
    """Начисляет купленное. Повторный charge игнорируется."""
    col = {"skip": "skip", "brush": "brush", "shield": "shield"}[item]
    with closing(db()) as c, c:
        if c.execute("SELECT 1 FROM paid WHERE charge=?", (charge,)).fetchone():
            return False
        c.execute("INSERT INTO paid VALUES(?)", (charge,))
        c.execute(f"UPDATE users SET {col}={col}+? WHERE id=?", (ITEMS[item][1], uid))
    return True


def standings():
    """(топ-10, {clan_id: место}, Counter очков) — с кэшем на 2 секунды."""
    global _stand
    if _stand[1] and time.time() - _stand[0] < 2 and _stand[1][3] == ver:
        return _stand[1][:3]
    cnt = Counter(owners)
    cnt.pop(0, None)
    with closing(db()) as c:
        names = {r["id"]: r["name"] for r in c.execute("SELECT id, name FROM clans")}
    ranked = sorted(cnt.items(), key=lambda kv: -kv[1])
    top = [{"id": k, "name": names.get(k, "?"), "n": v} for k, v in ranked[:10]]
    res = (top, {k: i + 1 for i, (k, _) in enumerate(ranked)}, cnt, ver)
    _stand = (time.time(), res)
    return res[:3]


# ---------------- Веб-API ----------------
def guarded(fn):
    async def wrap(request):
        try:
            return await fn(request)
        except GameError as e:
            return web.json_response({"error": str(e)}, status=400)
    return wrap


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


@guarded
async def api_state(request):
    tu = auth(request)
    u = ensure_user(tu["id"], tu.get("first_name", ""))
    now = time.time()
    with closing(db()) as c:
        clan = c.execute("SELECT * FROM clans WHERE id=?", (u["clan"],)).fetchone()
        sh = [[r["x0"], r["y0"], r["x1"], r["y1"], r["clan"]]
              for r in c.execute("SELECT * FROM shields WHERE until>?", (now,))]
    top, rank, cnt = standings()
    out = {"ver": ver, "w": W, "h": H, "top": top, "shields": sh, "bot": BOT_USERNAME,
           "cd": max(0.0, COOLDOWN - (now - u["last"])), "cdmax": COOLDOWN,
           "items": {k: {"stars": v[0], "qty": v[1], "title": v[2], "desc": v[3]} for k, v in ITEMS.items()},
           "me": {"name": u["name"], "clan": u["clan"], "cname": clan["name"] if clan else "",
                  "code": clan["code"] if clan else "", "score": cnt.get(u["clan"], 0),
                  "rank": rank.get(u["clan"], 0), "skip": u["skip"], "brush": u["brush"],
                  "shield": u["shield"], "placed": u["placed"]}}
    if request.query.get("v") != str(ver):
        out["c"], out["o"] = b64(bytes(colors)), b64(owners.tobytes())
    return web.json_response(out)


@guarded
async def api_place(request):
    u, b = auth(request), await request.json()
    n = do_place(u["id"], b.get("x"), b.get("y"), b.get("c"), b.get("size", 1))
    return web.json_response({"ok": True, "n": n})


@guarded
async def api_shield(request):
    u, b = auth(request), await request.json()
    do_shield(u["id"], b.get("x"), b.get("y"))
    return web.json_response({"ok": True})


@guarded
async def api_clan(request):
    u, b = auth(request), await request.json()
    ensure_user(u["id"], u.get("first_name", ""))
    if b.get("code"):
        join_clan(u["id"], str(b["code"]))
    else:
        make_clan(u["id"], str(b.get("name", "")))
    return web.json_response({"ok": True})


@guarded
async def api_invoice(request):
    u, b = auth(request), await request.json()
    item = b.get("item")
    if item not in ITEMS:
        raise GameError("Неизвестный товар")
    stars, _, title, desc = ITEMS[item]
    link = await bot.create_invoice_link(title=title, description=desc, payload=f"{item}:{u['id']}",
                                         provider_token="", currency="XTR",
                                         prices=[LabeledPrice(label=title, amount=stars)])
    return web.json_response({"link": link})


async def index(request):
    return web.FileResponse(os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html"),
                            headers={"Cache-Control": "no-store"})


# ---------------- Бот ----------------
router = Router()


def play_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⚔️ В бой", web_app=WebAppInfo(url=WEBAPP_URL))]])


@router.message(CommandStart())
async def start(m: Message, command: CommandObject):
    ensure_user(m.from_user.id, m.from_user.first_name or "")
    arg, note = (command.args or ""), ""
    if arg.startswith("clan_"):
        try:
            note = f"✅ Вы вступили в клан «{join_clan(m.from_user.id, arg[5:])}»!\n\n"
        except GameError as e:
            note = f"⚠️ {e}\n\n"
    await m.answer(f"{note}⚔️ <b>PIXEL WARS</b>\n\nОбщий холст, на котором кланы воюют за территорию. "
                   "Ставь пиксели, зови друзей в клан, защищай свои земли щитами.\n\nНажми «В бой» 👇",
                   reply_markup=play_kb())


@router.message(Command("paysupport"))
async def paysupport(m: Message):
    await m.answer("По вопросам оплаты и возврата Stars напишите администратору бота.")


@router.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    await q.answer(ok=True)


@router.message(F.successful_payment)
async def paid(m: Message):
    sp = m.successful_payment
    item, _, uid = sp.invoice_payload.partition(":")
    if item in ITEMS and uid.isdigit() and int(uid) == m.from_user.id:
        if credit(int(uid), item, sp.telegram_payment_charge_id):
            await m.answer(f"✅ {ITEMS[item][2]} — зачислено! Возвращайтесь на холст.", reply_markup=play_kb())


@router.message(Command("newseason"), F.from_user.id == ADMIN_ID)
async def newseason(m: Message):
    global ver
    top, _, _ = standings()
    colors[:] = bytes(W * H)
    for i in range(W * H):
        owners[i] = 0
    with closing(db()) as c, c:
        c.execute("DELETE FROM shields")
    ver += 1
    save_canvas()
    medals = "\n".join(f"{['🥇','🥈','🥉'][i]} {t['name']} — {t['n']} px" for i, t in enumerate(top[:3]))
    await m.answer("🏁 Сезон завершён, холст очищен.\n\nПобедители:\n" + (medals or "—"))


async def saver():
    while True:
        await asyncio.sleep(5)
        if dirty:
            try:
                save_canvas()
            except Exception:
                logging.exception("save_canvas")


def open_port_public():
    n = os.getenv("CODESPACE_NAME")
    if n:
        try:
            subprocess.run(["gh", "codespace", "ports", "visibility", f"{PORT}:public", "-c", n],
                           timeout=25, capture_output=True)
        except Exception:
            pass


async def main():
    global bot, BOT_USERNAME
    logging.basicConfig(level=logging.INFO)
    if not TOKEN:
        raise SystemExit("Не задан BOT_TOKEN")
    init_db()
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    BOT_USERNAME = (await bot.get_me()).username
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/api/state", api_state)
    for name, fn in (("place", api_place), ("shield", api_shield), ("clan", api_clan), ("invoice", api_invoice)):
        app.router.add_post(f"/api/{name}", fn)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    await asyncio.get_running_loop().run_in_executor(None, open_port_public)
    if WEBAPP_URL:
        try:
            await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="⚔️ Играть", web_app=WebAppInfo(url=WEBAPP_URL)))
        except Exception:
            logging.exception("menu button")
        logging.info("Игра доступна: %s", WEBAPP_URL)
    else:
        logging.warning("WEBAPP_URL не определён: задайте публичный https-адрес")
    asyncio.create_task(saver())
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
