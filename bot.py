import asyncio
import html
import logging
import os
import secrets

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()
DB_PATH = os.getenv("DB_PATH", "wishli.db")
router = Router()


# ---------------------------------------------------------------- database

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
                id INTEGER PRIMARY KEY,
                full_name TEXT NOT NULL,
                name_key TEXT NOT NULL,
                code TEXT UNIQUE NOT NULL,
                searchable INTEGER NOT NULL DEFAULT 1,
                show_booker INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS wishes(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                photo TEXT,
                link TEXT,
                price TEXT,
                done INTEGER NOT NULL DEFAULT 0,
                booked_by INTEGER,
                booked_anon INTEGER NOT NULL DEFAULT 0,
                booker_name TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_wishes_owner ON wishes(owner_id);
            """
        )
        await db.commit()


async def fetch_one(sql, args=()):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(sql, args)
        return await cur.fetchone()


async def fetch_all(sql, args=()):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(sql, args)
        return await cur.fetchall()


async def run(sql, args=()):
    """Execute a write query. Returns (lastrowid, rowcount)."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(sql, args)
        await db.commit()
        return cur.lastrowid, cur.rowcount


async def get_user(uid):
    return await fetch_one("SELECT * FROM users WHERE id=?", (uid,))


async def own_wish(wid, uid):
    return await fetch_one("SELECT * FROM wishes WHERE id=? AND owner_id=?", (wid, uid))


# ------------------------------------------------------------------ states

class Reg(StatesGroup):
    name = State()


class Add(StatesGroup):
    title = State()
    photo = State()
    link = State()
    price = State()


class Edit(StatesGroup):
    value = State()


class Find(StatesGroup):
    name = State()


# ----------------------------------------------------------------- helpers

def esc(s):
    return html.escape(str(s))


def norm_link(s):
    s = s.strip()
    if " " in s or "." not in s:
        return None
    if not s.lower().startswith(("http://", "https://")):
        s = "https://" + s
    return s[:500]


def wish_label(w, own):
    title = w["title"] if len(w["title"]) <= 40 else w["title"][:37] + "…"
    if w["done"]:
        mark = "✅"
    elif w["booked_by"]:
        mark = "🎁" if own else "🔒"
    else:
        mark = "▫️"
    return f"{mark} {title}"


def wish_text(w, own, show_booker):
    lines = [f"<b>{esc(w['title'])}</b>"]
    if w["price"]:
        lines.append(f"💰 {esc(w['price'])}")
    if w["link"]:
        lines.append(f'🔗 <a href="{esc(w["link"])}">Посилання</a>')
    lines.append("")
    if w["done"]:
        lines.append("✅ Виконано")
    elif w["booked_by"]:
        if own:
            if show_booker and not w["booked_anon"] and w["booker_name"]:
                lines.append(f"🎁 Забронював(ла): {esc(w['booker_name'])}")
            else:
                lines.append("🎁 Хтось уже забронював цей подарунок")
        else:
            lines.append("🔒 Вже заброньовано")
    else:
        lines.append("Вільне — можна дарувати 💝" if not own else "Поки ніхто не бронював")
    return "\n".join(lines)


async def show(target, text, kb: InlineKeyboardMarkup | None = None, photo=None):
    """Send a fresh message (deleting the old one when coming from a button)."""
    if isinstance(target, CallbackQuery):
        msg = target.message
        try:
            await target.answer()
        except Exception:
            pass
        try:
            await msg.delete()
        except Exception:
            pass
    else:
        msg = target
    if photo:
        try:
            await msg.answer_photo(photo, caption=text, reply_markup=kb)
            return
        except Exception:
            logging.exception("photo send failed, falling back to text")
    await msg.answer(text, reply_markup=kb)


def back_kb(cb="menu", text="⬅️ Назад"):
    b = InlineKeyboardBuilder()
    b.button(text=text, callback_data=cb)
    return b.as_markup()


def cancel_kb(cb):
    return back_kb(cb, "✖️ Скасувати")


def skip_kb(cancel_cb="my"):
    b = InlineKeyboardBuilder()
    b.button(text="⏭ Пропустити", callback_data="skip")
    b.button(text="✖️ Скасувати", callback_data=cancel_cb)
    b.adjust(1)
    return b.as_markup()


async def ensure_user(target, state: FSMContext):
    user = await get_user(target.from_user.id)
    if user:
        return user
    await state.set_state(Reg.name)
    msg = target.message if isinstance(target, CallbackQuery) else target
    if isinstance(target, CallbackQuery):
        await target.answer()
    await msg.answer("Спершу введи своє ім'я та прізвище ✍️")
    return None


# ------------------------------------------------------------------- views

async def show_menu(target, user):
    b = InlineKeyboardBuilder()
    b.button(text="🎁 Мій вішліст", callback_data="my")
    b.button(text="🔍 Знайти вішліст друга", callback_data="find")
    b.button(text="🔗 Поділитися посиланням", callback_data="share")
    b.adjust(1)
    await show(target, f"Привіт, <b>{esc(user['full_name'])}</b>! Що робимо?", b.as_markup())


async def show_my(target, user):
    wishes = await fetch_all("SELECT * FROM wishes WHERE owner_id=? ORDER BY id", (user["id"],))
    b = InlineKeyboardBuilder()
    for w in wishes:
        b.button(text=wish_label(w, own=True), callback_data=f"mw:{w['id']}")
    b.button(text="➕ Додати бажання", callback_data="add")
    b.button(text="⬅️ Меню", callback_data="menu")
    b.adjust(1)
    text = "🎁 <b>Мій вішліст</b>"
    if wishes:
        text += "\n\n✅ виконано · 🎁 хтось забронював"
    else:
        text += "\n\nПоки що порожньо. Додай перше бажання!"
    await show(target, text, b.as_markup())


async def show_user_list(target, owner, viewer_id):
    if owner["id"] == viewer_id:
        await show_my(target, owner)
        return
    wishes = await fetch_all("SELECT * FROM wishes WHERE owner_id=? ORDER BY id", (owner["id"],))
    b = InlineKeyboardBuilder()
    for w in wishes:
        b.button(text=wish_label(w, own=False), callback_data=f"fw:{w['id']}")
    b.button(text="⬅️ Меню", callback_data="menu")
    b.adjust(1)
    text = f"🎁 <b>Вішліст: {esc(owner['full_name'])}</b>"
    if wishes:
        text += "\n\n▫️ вільне · 🔒 заброньовано · ✅ виконано"
    else:
        text += "\n\nУ цієї людини поки немає бажань."
    await show(target, text, b.as_markup())


async def show_wish(target, wid, viewer_id, note=""):
    w = await fetch_one("SELECT * FROM wishes WHERE id=?", (wid,))
    if not w:
        await show(target, "Це бажання більше не існує.", back_kb("menu", "⬅️ Меню"))
        return
    owner = await get_user(w["owner_id"])
    own = viewer_id == w["owner_id"]
    text = wish_text(w, own, owner["show_booker"] if owner else 1)
    if not own and w["booked_by"] == viewer_id and not w["done"]:
        text = text.replace("🔒 Вже заброньовано", "🎁 Ти забронював(ла) це бажання")
    if note:
        text += f"\n\n{note}"
    b = InlineKeyboardBuilder()
    if own:
        b.button(text="✏️ Назва", callback_data=f"ed:title:{wid}")
        b.button(text="💰 Ціна", callback_data=f"ed:price:{wid}")
        b.button(text="🔗 Посилання", callback_data=f"ed:link:{wid}")
        b.button(text="🖼 Фото", callback_data=f"ed:photo:{wid}")
        b.button(
            text="↩️ Повернути в список" if w["done"] else "✅ Виконано",
            callback_data=f"done:{wid}",
        )
        b.button(text="🗑 Видалити", callback_data=f"delq:{wid}")
        b.button(text="⬅️ До списку", callback_data="my")
        b.adjust(2, 2, 1, 1, 1)
    else:
        if not w["done"]:
            if w["booked_by"] is None:
                b.button(text="🎁 Забронювати", callback_data=f"bk:{wid}")
            elif w["booked_by"] == viewer_id:
                b.button(text="❌ Скасувати бронь", callback_data=f"ub:{wid}")
        b.button(text="⬅️ До списку", callback_data=f"fu:{w['owner_id']}")
        b.adjust(1)
    await show(target, text, b.as_markup(), photo=w["photo"])


async def show_pick(target, user, prefix, title):
    wishes = await fetch_all("SELECT * FROM wishes WHERE owner_id=? ORDER BY id", (user["id"],))
    if not wishes:
        await show(target, "Твій список порожній.", back_kb("menu", "⬅️ Меню"))
        return
    b = InlineKeyboardBuilder()
    for w in wishes:
        b.button(text=wish_label(w, own=True), callback_data=f"{prefix}:{w['id']}")
    b.button(text="⬅️ Меню", callback_data="menu")
    b.adjust(1)
    await show(target, title, b.as_markup())


async def show_settings(target, user):
    b = InlineKeyboardBuilder()
    b.button(
        text=f"{'✅' if user['searchable'] else '⬜️'} Мене можна знайти за іменем",
        callback_data="set:vis",
    )
    b.button(
        text=f"{'✅' if user['show_booker'] else '⬜️'} Бачити, хто забронював",
        callback_data="set:who",
    )
    b.button(text="✏️ Змінити ім'я", callback_data="set:name")
    b.button(text="⬅️ Меню", callback_data="menu")
    b.adjust(1)
    text = (
        "⚙️ <b>Налаштування</b>\n\n"
        "Якщо вимкнути пошук, твій список відкриється лише за особистим посиланням.\n"
        "Якщо вимкнути «Бачити, хто забронював», ти не дізнаєшся імен, "
        "тож сюрприз буде повним."
    )
    await show(target, text, b.as_markup())


HELP_TEXT = (
    "ℹ️ <b>Як це працює</b>\n\n"
    "1. Додай свої бажання у «Мій вішліст».\n"
    "2. Надішли друзям особисте посилання або дай знайти себе за іменем.\n"
    "3. Друзі бронюють подарунки, а тобі приходить сповіщення 🎁\n\n"
    "/add — додати бажання\n"
    "/my — мій вішліст\n"
    "/delete — видалити бажання\n"
    "/complete — позначити виконаним\n"
    "/find — знайти вішліст друга\n"
    "/share — моє посилання\n"
    "/settings — налаштування\n"
    "/help — ця підказка"
)


# ---------------------------------------------------------------- commands
# Commands are registered first so they work in any state.

@router.message(CommandStart())
async def cmd_start(m: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    if command.args:
        owner = await fetch_one("SELECT * FROM users WHERE code=?", (command.args,))
        if owner:
            await show_user_list(m, owner, m.from_user.id)
            return
    user = await get_user(m.from_user.id)
    if not user:
        await state.set_state(Reg.name)
        await m.answer(
            "Привіт! 🌿 Це <b>Wishli</b> — твій особистий вішліст.\n\n"
            "Щоб почати, введи своє ім'я та прізвище. "
            "За ними друзі зможуть тебе знайти."
        )
        return
    await show_menu(m, user)


@router.message(Command("help"))
async def cmd_help(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(HELP_TEXT)


@router.message(Command("my"))
async def cmd_my(m: Message, state: FSMContext):
    await state.clear()
    user = await ensure_user(m, state)
    if user:
        await show_my(m, user)


@router.message(Command("add"))
async def cmd_add(m: Message, state: FSMContext):
    await state.clear()
    user = await ensure_user(m, state)
    if user:
        await state.set_state(Add.title)
        await m.answer("Що хочеш отримати? Напиши назву бажання ✨", reply_markup=cancel_kb("my"))


@router.message(Command("delete"))
async def cmd_delete(m: Message, state: FSMContext):
    await state.clear()
    user = await ensure_user(m, state)
    if user:
        await show_pick(m, user, "delq", "🗑 Яке бажання видалити?")


@router.message(Command("complete"))
async def cmd_complete(m: Message, state: FSMContext):
    await state.clear()
    user = await ensure_user(m, state)
    if user:
        await show_pick(m, user, "done", "✅ Яке бажання виконано?")


@router.message(Command("find"))
async def cmd_find(m: Message, state: FSMContext):
    await state.clear()
    user = await ensure_user(m, state)
    if user:
        await state.set_state(Find.name)
        await m.answer("Введи ім'я та прізвище друга 🔍", reply_markup=cancel_kb("menu"))


@router.message(Command("share"))
async def cmd_share(m: Message, state: FSMContext):
    await state.clear()
    user = await ensure_user(m, state)
    if user:
        await send_share(m, user)


@router.message(Command("settings"))
async def cmd_settings(m: Message, state: FSMContext):
    await state.clear()
    user = await ensure_user(m, state)
    if user:
        await show_settings(m, user)


async def send_share(target, user):
    bot = target.bot
    me = await bot.me()
    link = f"https://t.me/{me.username}?start={user['code']}"
    text = (
        "🔗 <b>Твоє особисте посилання</b>\n\n"
        f"{link}\n\n"
        "Перешли його друзям, і вони одразу побачать твій вішліст."
    )
    await show(target, text, back_kb("menu", "⬅️ Меню"))


# ------------------------------------------------------------ registration

@router.message(Reg.name, F.text)
async def reg_name(m: Message, state: FSMContext):
    name = " ".join(m.text.split())
    if not 3 <= len(name) <= 80:
        await m.answer("Введи, будь ласка, ім'я та прізвище (від 3 до 80 символів).")
        return
    await run(
        "INSERT INTO users(id, full_name, name_key, code) VALUES(?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET full_name=excluded.full_name, name_key=excluded.name_key",
        (m.from_user.id, name, name.casefold(), secrets.token_urlsafe(6)),
    )
    await state.clear()
    await show_menu(m, await get_user(m.from_user.id))


# ------------------------------------------------------- navigation buttons

@router.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    user = await ensure_user(cb, state)
    if user:
        await show_menu(cb, user)


@router.callback_query(F.data == "my")
async def cb_my(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    user = await ensure_user(cb, state)
    if user:
        await show_my(cb, user)


@router.callback_query(F.data == "share")
async def cb_share(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    user = await ensure_user(cb, state)
    if user:
        await send_share(cb, user)


@router.callback_query(F.data == "find")
async def cb_find(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    user = await ensure_user(cb, state)
    if user:
        await state.set_state(Find.name)
        await show(cb, "Введи ім'я та прізвище друга 🔍", cancel_kb("menu"))


@router.callback_query(F.data.startswith("fu:"))
async def cb_friend_list(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    owner = await get_user(int(cb.data.split(":")[1]))
    if not owner:
        await cb.answer("Користувача не знайдено", show_alert=True)
        return
    await show_user_list(cb, owner, cb.from_user.id)


@router.callback_query(F.data.startswith("mw:"))
async def cb_my_wish(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    wid = int(cb.data.split(":")[1])
    if not await own_wish(wid, cb.from_user.id):
        await cb.answer("Бажання не знайдено", show_alert=True)
        return
    await show_wish(cb, wid, cb.from_user.id)


@router.callback_query(F.data.startswith("fw:"))
async def cb_friend_wish(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_wish(cb, int(cb.data.split(":")[1]), cb.from_user.id)


# ------------------------------------------------------------ add a wish

@router.callback_query(F.data == "add")
async def cb_add(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    user = await ensure_user(cb, state)
    if user:
        await state.set_state(Add.title)
        await show(cb, "Що хочеш отримати? Напиши назву бажання ✨", cancel_kb("my"))


@router.message(Add.title, F.text)
async def add_title(m: Message, state: FSMContext):
    title = m.text.strip()
    if not title or len(title) > 100:
        await m.answer("Назва має бути до 100 символів. Спробуй ще раз.")
        return
    await state.update_data(title=title)
    await state.set_state(Add.photo)
    await m.answer("🖼 Надішли фото (необов'язково)", reply_markup=skip_kb())


async def ask_link(target, state):
    await state.set_state(Add.link)
    await show(target, "🔗 Надішли посилання на товар (необов'язково)", skip_kb())


async def ask_price(target, state):
    await state.set_state(Add.price)
    await show(target, "💰 Вкажи орієнтовну ціну (необов'язково)", skip_kb())


async def finish_add(target, state):
    d = await state.get_data()
    uid = target.from_user.id
    await run(
        "INSERT INTO wishes(owner_id, title, photo, link, price) VALUES(?,?,?,?,?)",
        (uid, d["title"], d.get("photo"), d.get("link"), d.get("price")),
    )
    await state.clear()
    user = await get_user(uid)
    await show_my(target, user)


@router.message(Add.photo, F.photo)
async def add_photo(m: Message, state: FSMContext):
    await state.update_data(photo=m.photo[-1].file_id)
    await ask_link(m, state)


@router.message(Add.photo)
async def add_photo_bad(m: Message):
    await m.answer("Надішли саме фото або натисни «Пропустити».")


@router.message(Add.link, F.text)
async def add_link(m: Message, state: FSMContext):
    link = norm_link(m.text)
    if not link:
        await m.answer("Схоже, це не посилання. Спробуй ще раз або натисни «Пропустити».")
        return
    await state.update_data(link=link)
    await ask_price(m, state)


@router.message(Add.price, F.text)
async def add_price(m: Message, state: FSMContext):
    await state.update_data(price=m.text.strip()[:50])
    await finish_add(m, state)


@router.callback_query(F.data == "skip", Add.photo)
async def skip_photo(cb: CallbackQuery, state: FSMContext):
    await ask_link(cb, state)


@router.callback_query(F.data == "skip", Add.link)
async def skip_link(cb: CallbackQuery, state: FSMContext):
    await ask_price(cb, state)


@router.callback_query(F.data == "skip", Add.price)
async def skip_price(cb: CallbackQuery, state: FSMContext):
    await finish_add(cb, state)


# --------------------------------------------------- edit / delete / done

EDIT_FIELDS = {
    "title": "✏️ Напиши нову назву",
    "price": "💰 Напиши ціну (або «-», щоб прибрати)",
    "link": "🔗 Надішли посилання (або «-», щоб прибрати)",
    "photo": "🖼 Надішли нове фото",
}


@router.callback_query(F.data.startswith("ed:"))
async def cb_edit(cb: CallbackQuery, state: FSMContext):
    _, field, wid = cb.data.split(":")
    wid = int(wid)
    if field not in EDIT_FIELDS or not await own_wish(wid, cb.from_user.id):
        await cb.answer("Бажання не знайдено", show_alert=True)
        return
    await state.set_state(Edit.value)
    await state.update_data(field=field, wid=wid)
    await show(cb, EDIT_FIELDS[field], cancel_kb(f"mw:{wid}"))


@router.message(Edit.value, F.photo)
async def edit_photo(m: Message, state: FSMContext):
    d = await state.get_data()
    if d["field"] != "photo":
        await m.answer("Тут потрібен текст, а не фото 🙂")
        return
    await run(
        "UPDATE wishes SET photo=? WHERE id=? AND owner_id=?",
        (m.photo[-1].file_id, d["wid"], m.from_user.id),
    )
    await state.clear()
    await show_wish(m, d["wid"], m.from_user.id, note="✅ Збережено")


@router.message(Edit.value, F.text)
async def edit_text(m: Message, state: FSMContext):
    d = await state.get_data()
    field, wid = d["field"], d["wid"]
    val = m.text.strip()
    if field == "photo":
        await m.answer("Надішли саме фото 🖼")
        return
    if field == "title":
        if not val or len(val) > 100:
            await m.answer("Назва має бути до 100 символів.")
            return
    elif val == "-":
        val = None
    elif field == "link":
        val = norm_link(val)
        if not val:
            await m.answer("Схоже, це не посилання. Спробуй ще раз.")
            return
    else:
        val = val[:50]
    await run(f"UPDATE wishes SET {field}=? WHERE id=? AND owner_id=?", (val, wid, m.from_user.id))
    await state.clear()
    await show_wish(m, wid, m.from_user.id, note="✅ Збережено")


@router.callback_query(F.data.startswith("done:"))
async def cb_done(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    wid = int(cb.data.split(":")[1])
    await run("UPDATE wishes SET done = 1 - done WHERE id=? AND owner_id=?", (wid, cb.from_user.id))
    await show_wish(cb, wid, cb.from_user.id)


@router.callback_query(F.data.startswith("delq:"))
async def cb_delete_ask(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    wid = int(cb.data.split(":")[1])
    w = await own_wish(wid, cb.from_user.id)
    if not w:
        await cb.answer("Бажання не знайдено", show_alert=True)
        return
    b = InlineKeyboardBuilder()
    b.button(text="🗑 Так, видалити", callback_data=f"delok:{wid}")
    b.button(text="↩️ Ні, залишити", callback_data=f"mw:{wid}")
    b.adjust(1)
    await show(cb, f"Видалити «{esc(w['title'])}»?", b.as_markup())


@router.callback_query(F.data.startswith("delok:"))
async def cb_delete_ok(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    wid = int(cb.data.split(":")[1])
    await run("DELETE FROM wishes WHERE id=? AND owner_id=?", (wid, cb.from_user.id))
    user = await get_user(cb.from_user.id)
    await show_my(cb, user)


# ------------------------------------------------------------ find a friend

@router.message(Find.name, F.text)
async def find_name(m: Message, state: FSMContext):
    tokens = [t.replace("%", "").replace("_", "") for t in m.text.casefold().split()]
    tokens = [t for t in tokens if t]
    if not tokens:
        await m.answer("Введи ім'я та прізвище.")
        return
    sql = "SELECT * FROM users WHERE searchable=1 AND id != ?"
    args = [m.from_user.id]
    for t in tokens:
        sql += " AND name_key LIKE ?"
        args.append(f"%{t}%")
    sql += " LIMIT 10"
    found = await fetch_all(sql, tuple(args))
    if not found:
        await m.answer(
            "Нікого не знайшла 😔\nПеревір написання або попроси друга "
            "надіслати тобі посилання на свій вішліст.",
            reply_markup=cancel_kb("menu"),
        )
        return
    await state.clear()
    if len(found) == 1:
        await show_user_list(m, found[0], m.from_user.id)
        return
    b = InlineKeyboardBuilder()
    for u in found:
        b.button(text=u["full_name"], callback_data=f"fu:{u['id']}")
    b.button(text="⬅️ Меню", callback_data="menu")
    b.adjust(1)
    await m.answer("Знайшла кількох людей. Обери потрібну:", reply_markup=b.as_markup())


# ------------------------------------------------------------------ booking

@router.callback_query(F.data.startswith("bk:"))
async def cb_book_ask(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    wid = int(cb.data.split(":")[1])
    w = await fetch_one("SELECT * FROM wishes WHERE id=?", (wid,))
    if not w or w["owner_id"] == cb.from_user.id:
        await cb.answer("Це бажання недоступне", show_alert=True)
        return
    if w["booked_by"] is not None or w["done"]:
        await cb.answer("Хтось уже забронював це бажання 🙈", show_alert=True)
        return
    b = InlineKeyboardBuilder()
    b.button(text="🕶 Анонімно", callback_data=f"bka:{wid}:1")
    b.button(text="🙋 З моїм іменем", callback_data=f"bka:{wid}:0")
    b.button(text="⬅️ Назад", callback_data=f"fw:{wid}")
    b.adjust(1)
    await show(cb, "Як забронювати?", b.as_markup())


@router.callback_query(F.data.startswith("bka:"))
async def cb_book(cb: CallbackQuery, state: FSMContext):
    _, wid, anon = cb.data.split(":")
    wid, anon = int(wid), int(anon)
    viewer = await get_user(cb.from_user.id)
    name = None if anon else (viewer["full_name"] if viewer else cb.from_user.full_name)
    _, changed = await run(
        "UPDATE wishes SET booked_by=?, booked_anon=?, booker_name=? "
        "WHERE id=? AND booked_by IS NULL AND done=0 AND owner_id != ?",
        (cb.from_user.id, anon, name, wid, cb.from_user.id),
    )
    if not changed:
        await cb.answer("Хтось уже забронював це бажання 🙈", show_alert=True)
        return
    w = await fetch_one("SELECT * FROM wishes WHERE id=?", (wid,))
    try:
        await cb.bot.send_message(
            w["owner_id"],
            "🎁 <b>Хтось хоче зробити тобі подаруночок!</b>\n"
            f"Обране бажання: {esc(w['title'])}",
        )
    except Exception:
        logging.warning("could not notify owner %s", w["owner_id"])
    await show_wish(cb, wid, cb.from_user.id, note="✅ Забронювано! Дякуємо 💝")


@router.callback_query(F.data.startswith("ub:"))
async def cb_unbook(cb: CallbackQuery, state: FSMContext):
    wid = int(cb.data.split(":")[1])
    await run(
        "UPDATE wishes SET booked_by=NULL, booked_anon=0, booker_name=NULL "
        "WHERE id=? AND booked_by=?",
        (wid, cb.from_user.id),
    )
    await show_wish(cb, wid, cb.from_user.id, note="Бронь скасовано")


# ----------------------------------------------------------------- settings

@router.callback_query(F.data.startswith("set:"))
async def cb_settings(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    user = await ensure_user(cb, state)
    if not user:
        return
    action = cb.data.split(":")[1]
    if action == "vis":
        await run("UPDATE users SET searchable = 1 - searchable WHERE id=?", (user["id"],))
    elif action == "who":
        await run("UPDATE users SET show_booker = 1 - show_booker WHERE id=?", (user["id"],))
    elif action == "name":
        await state.set_state(Reg.name)
        await show(cb, "Введи нове ім'я та прізвище ✍️", cancel_kb("set:back"))
        return
    await show_settings(cb, await get_user(user["id"]))


@router.callback_query(F.data == "set:back")
async def cb_settings_back(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    user = await get_user(cb.from_user.id)
    if user:
        await show_settings(cb, user)


# ----------------------------------------------------------------- fallback

@router.message()
async def fallback(m: Message):
    await m.answer("Не зрозуміла 🙈 Натисни /start, щоб відкрити меню, або /help.")


# --------------------------------------------------------------------- main

async def main():
    logging.basicConfig(level=logging.INFO)
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("Додай BOT_TOKEN у файл .env (див. .env.example)")
    await init_db()
    bot = Bot(
        token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
