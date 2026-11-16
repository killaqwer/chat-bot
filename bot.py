"""
Бот для чата: модерация по теме + база знаний по папкам.

Что умеет:
  В ГРУППЕ
    - раз в CHECK_INTERVAL_MIN минут (по умолчанию 60) прогоняет новые сообщения через нейросеть:
        useful   -> закрепляет (без уведомления) и сохраняет в базу в нужную папку
        neutral  -> оставляет как есть
        offtopic -> удаляет
    - файлы (документы, фото, видео, аудио) всегда считаются полезными
    - команды (ответом на сообщение):
        /keep               - не удалять это сообщение
        /pin [Папка/Под]    - закрепить и сохранить в базу
        /save [Папка/Под]   - только сохранить в базу
        /check              - прогнать проверку прямо сейчас
        /chatid             - показать id чата
  В ЛИЧКЕ С БОТОМ
    - /start или /base      - открыть базу (папки -> подпапки -> записи)
    - кинул текст/файл/пересылку -> сохранится в открытую папку
      (если открыт корень и есть ключ нейросети - бот сам подберёт папку)
    - кнопки: создать папку, удалить папку, удалить запись
    - /myid                 - узнать свой id
"""

import asyncio
import html
import json
import logging
import os
import re
import sqlite3
import time
from pathlib import Path

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message


# ---------------------------------------------------------------- настройки
def load_env(name: str = ".env") -> None:
    p = Path(__file__).with_name(name)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()


def ids(var: str) -> set[int]:
    return {int(x) for x in re.findall(r"-?\d+", os.getenv(var, ""))}


BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_IDS = ids("ADMIN_IDS")      # кто имеет доступ к базе; пусто = все
GROUP_IDS = ids("GROUP_IDS")      # в каких чатах модерировать; пусто = во всех
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
LLM_MODEL = os.getenv("LLM_MODEL", "openai/gpt-oss-120b")
CHAT_TOPIC = os.getenv(
    "CHAT_TOPIC",
    "заработок на нейросетях (способы, схемы, инструменты, промпты, кейсы) и наши проекты: "
    "игра в Roblox (режимы, идеи, разработка), TikTok-аккаунт",
)
CHECK_INTERVAL_MIN = int(os.getenv("CHECK_INTERVAL_MIN", "60"))
DB_PATH = os.getenv("DB_PATH", str(Path(__file__).with_name("bot.db")))

FILE_KINDS = {"document", "photo", "video", "audio"}  # всегда полезные
KIND_RU = {
    "text": "текст", "document": "файл", "photo": "фото", "video": "видео", "audio": "аудио",
    "voice": "голосовое", "video_note": "кружок", "animation": "гифка", "sticker": "стикер",
}
PAGE = 15

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

# ---------------------------------------------------------------- база данных
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.executescript(
    """
CREATE TABLE IF NOT EXISTS pending(
    chat_id INTEGER, msg_id INTEGER, user_name TEXT, text TEXT, kind TEXT,
    file_id TEXT, file_name TEXT, ts INTEGER, keep INTEGER DEFAULT 0,
    PRIMARY KEY(chat_id, msg_id));
CREATE TABLE IF NOT EXISTS categories(
    id INTEGER PRIMARY KEY AUTOINCREMENT, parent_id INTEGER NOT NULL DEFAULT 0, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS items(
    id INTEGER PRIMARY KEY AUTOINCREMENT, category_id INTEGER NOT NULL, title TEXT, text TEXT,
    kind TEXT, file_id TEXT, src_chat INTEGER, src_msg INTEGER, author TEXT, ts INTEGER);
"""
)
db.commit()


def children(parent: int) -> list[sqlite3.Row]:
    return db.execute("SELECT * FROM categories WHERE parent_id=? ORDER BY name", (parent,)).fetchall()


def get_cat(cid: int):
    return db.execute("SELECT * FROM categories WHERE id=?", (cid,)).fetchone()


def create_cat(parent: int, name: str) -> int:
    cur = db.execute("INSERT INTO categories(parent_id, name) VALUES(?,?)", (parent, name))
    db.commit()
    return cur.lastrowid


def ensure_path(path: list) -> int:
    """['Roblox','Режим','Идеи'] -> id папки (создаёт недостающие)."""
    if isinstance(path, str):
        path = path.split("/")
    parent = 0
    for raw in list(path)[:4]:
        name = str(raw).strip()[:50] or "Разное"
        found = next((c for c in children(parent) if c["name"].casefold() == name.casefold()), None)
        parent = found["id"] if found else create_cat(parent, name)
    return parent


def path_names(cid: int) -> list[str]:
    names = []
    while cid:
        c = get_cat(cid)
        if not c:
            break
        names.append(c["name"])
        cid = c["parent_id"]
    return list(reversed(names))


def path_str(cid: int) -> str:
    return " / ".join(["База"] + path_names(cid))


def count_all(cid: int) -> int:
    n = db.execute("SELECT COUNT(*) FROM items WHERE category_id=?", (cid,)).fetchone()[0]
    return n + sum(count_all(c["id"]) for c in children(cid))


def delete_cat(cid: int) -> None:
    for c in children(cid):
        delete_cat(c["id"])
    db.execute("DELETE FROM items WHERE category_id=?", (cid,))
    db.execute("DELETE FROM categories WHERE id=?", (cid,))
    db.commit()


def tree_text(parent: int = 0, prefix: str = "", out: list | None = None) -> list[str]:
    out = [] if out is None else out
    for c in children(parent):
        p = f"{prefix}/{c['name']}" if prefix else c["name"]
        out.append(p)
        tree_text(c["id"], p, out)
    return out


def save_item(cat_id: int, title: str, row: dict, src_chat=None, src_msg=None) -> int:
    if not title:
        title = (row.get("text") or "").strip().split("\n")[0][:60] or row.get("file_name") \
            or KIND_RU.get(row.get("kind"), "запись")
    cur = db.execute(
        "INSERT INTO items(category_id,title,text,kind,file_id,src_chat,src_msg,author,ts) VALUES(?,?,?,?,?,?,?,?,?)",
        (cat_id, title[:80], row.get("text") or "", row.get("kind"), row.get("file_id"),
         src_chat, src_msg, row.get("user_name"), int(time.time())),
    )
    db.commit()
    return cur.lastrowid


# ---------------------------------------------------------------- разбор сообщений
def extract(m: Message) -> dict:
    kind, file_id, fname = "text", None, None
    if m.animation:
        kind, file_id, fname = "animation", m.animation.file_id, m.animation.file_name
    elif m.document:
        kind, file_id, fname = "document", m.document.file_id, m.document.file_name
    elif m.photo:
        kind, file_id = "photo", m.photo[-1].file_id
    elif m.video:
        kind, file_id, fname = "video", m.video.file_id, m.video.file_name
    elif m.audio:
        kind, file_id, fname = "audio", m.audio.file_id, m.audio.file_name
    elif m.voice:
        kind, file_id = "voice", m.voice.file_id
    elif m.video_note:
        kind, file_id = "video_note", m.video_note.file_id
    elif m.sticker:
        kind, file_id = "sticker", m.sticker.file_id
    u = m.from_user
    name = (u.full_name if u else None) or (m.sender_chat.title if m.sender_chat else "?")
    return {"text": m.text or m.caption or "", "kind": kind, "file_id": file_id,
            "file_name": fname, "user_name": name}


def can_use(user_id: int | None) -> bool:
    return not ADMIN_IDS or (user_id in ADMIN_IDS)


def msg_link(chat_id, msg_id) -> str | None:
    s = str(chat_id)
    if chat_id and msg_id and s.startswith("-100"):
        return f"https://t.me/c/{s[4:]}/{msg_id}"
    return None


# ---------------------------------------------------------------- нейросеть
SYSTEM_PROMPT = """Ты модератор группового чата. Тема чата: {topic}.

Для каждого сообщения выбери verdict:
- "useful"   — ценная информация по теме: способ заработка, нейросеть/инструмент, схема, инструкция,
               идея, полезная ссылка, итоги и решения. Такое закрепят и сохранят в базу знаний.
- "neutral"  — обсуждение по теме, вопросы, короткие ответы по делу ("ок", "давай так"), организационное.
- "offtopic" — не по делу: флуд, бессмысленные мемы, стикеры, болтовня на посторонние темы. Это удалят.
Если сомневаешься между offtopic и neutral — ставь neutral.

Для useful укажи "path" — путь папок от общего к частному, 1–3 уровня, например
["Roblox","Режим","Идеи режима"] или ["Нейросети","Заработок","Схемы"]. Сначала используй существующие
папки (список ниже), новую создавай только если ничего не подходит. И "title" — заголовок до 60 символов.

Существующие папки:
{tree}

Ответ строго JSON без пояснений:
{{"results":[{{"id":0,"verdict":"useful","path":["..."],"title":"..."}}]}}"""


def parse_json(s: str) -> dict:
    try:
        return json.loads(s)
    except Exception:
        m = re.search(r"\{.*\}", s, re.S)
        return json.loads(m.group(0)) if m else {}


async def classify(rows: list[dict]) -> dict[int, dict]:
    """Возвращает {индекс: {"verdict","path","title"}}. Пустой dict = не удалось (повторим позже)."""
    if not LLM_API_KEY:
        # без нейросети ничего не удаляем: файлы -> в базу, остальное оставляем
        return {i: {"verdict": "useful" if r["kind"] in FILE_KINDS else "neutral",
                    "path": ["Файлы"], "title": ""} for i, r in enumerate(rows)}
    msgs = []
    for i, r in enumerate(rows):
        t = (r.get("text") or "")[:1500]
        if r["kind"] != "text":
            t = f"[{KIND_RU.get(r['kind'], r['kind'])}{': ' + r['file_name'] if r.get('file_name') else ''}] {t}"
        msgs.append({"id": i, "author": r.get("user_name"), "text": t})
    tree = "\n".join(tree_text()[:150]) or "(пока нет)"
    payload = {
        "model": LLM_MODEL,
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT.format(topic=CHAT_TOPIC, tree=tree)},
            {"role": "user", "content": json.dumps({"messages": msgs}, ensure_ascii=False)},
        ],
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{LLM_BASE_URL}/chat/completions",
                              headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                              json=payload, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                data = await resp.json(content_type=None)
        if "choices" not in data:
            log.error("LLM ответил ошибкой: %s", str(data)[:500])
            return {}
        parsed = parse_json(data["choices"][0]["message"]["content"])
        out = {}
        for x in parsed.get("results", []):
            try:
                out[int(x["id"])] = x
            except Exception:
                pass
        return out
    except Exception:
        log.exception("Ошибка запроса к нейросети")
        return {}


async def suggest_path(row: dict) -> list:
    if not LLM_API_KEY:
        return ["Входящие"]
    res = (await classify([row])).get(0) or {}
    return res.get("path") or ["Входящие"]


# ---------------------------------------------------------------- почасовая проверка
async def process_pending(bot: Bot) -> str:
    rows = [dict(r) for r in db.execute("SELECT * FROM pending ORDER BY ts").fetchall()]
    if not rows:
        return "Новых сообщений нет."
    stats = {"useful": 0, "neutral": 0, "offtopic": 0, "skipped": 0}
    for start in range(0, len(rows), 30):
        chunk = rows[start:start + 30]
        verdicts = await classify(chunk)
        for i, r in enumerate(chunk):
            v = verdicts.get(i)
            too_old = time.time() - r["ts"] > 40 * 3600  # удалять можно только <48ч
            if v is None and not too_old:
                stats["skipped"] += 1
                continue  # нейросеть не ответила — попробуем в следующий раз
            v = v or {"verdict": "neutral"}
            verdict = v.get("verdict", "neutral")
            if r["kind"] in FILE_KINDS:
                verdict = "useful"
            if r["keep"] and verdict == "offtopic":
                verdict = "neutral"
            try:
                if verdict == "useful":
                    cat = ensure_path(v.get("path") or ["Разное"])
                    save_item(cat, v.get("title", ""), r, r["chat_id"], r["msg_id"])
                    await bot.pin_chat_message(r["chat_id"], r["msg_id"], disable_notification=True)
                elif verdict == "offtopic":
                    await bot.delete_message(r["chat_id"], r["msg_id"])
            except Exception as e:
                log.warning("Не смог обработать %s/%s: %s", r["chat_id"], r["msg_id"], e)
            stats[verdict if verdict in stats else "neutral"] += 1
            db.execute("DELETE FROM pending WHERE chat_id=? AND msg_id=?", (r["chat_id"], r["msg_id"]))
        db.commit()
    return (f"Готово. Закреплено и сохранено: {stats['useful']}, оставлено: {stats['neutral']}, "
            f"удалено: {stats['offtopic']}, отложено: {stats['skipped']}.")


async def scheduler(bot: Bot) -> None:
    await asyncio.sleep(15)
    while True:
        try:
            log.info(await process_pending(bot))
        except Exception:
            log.exception("Ошибка проверки")
        await asyncio.sleep(CHECK_INTERVAL_MIN * 60)


# ---------------------------------------------------------------- интерфейс базы
router = Router()
state: dict[int, dict] = {}  # user_id -> {"cat": id, "await": None | "newcat"}


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def render_folder(cid: int, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    subs = children(cid)
    items = db.execute("SELECT id,title,kind FROM items WHERE category_id=? ORDER BY id DESC", (cid,)).fetchall()
    kb = [[btn(f"📁 {c['name']} ({count_all(c['id'])})", f"cat:{c['id']}:0")] for c in subs]
    s = page * PAGE
    for it in items[s:s + PAGE]:
        icon = "📝" if it["kind"] == "text" else "📎"
        kb.append([btn(f"{icon} {(it['title'] or 'без названия')[:50]}", f"it:{it['id']}")])
    nav = []
    if page > 0:
        nav.append(btn("◀️", f"cat:{cid}:{page - 1}"))
    if s + PAGE < len(items):
        nav.append(btn("▶️", f"cat:{cid}:{page + 1}"))
    if nav:
        kb.append(nav)
    ctrl = [btn("➕ Папка", f"nc:{cid}")]
    if cid:
        c = get_cat(cid)
        ctrl += [btn("⬆️ Назад", f"cat:{c['parent_id'] if c else 0}:0"), btn("🗑 Папку", f"dc:{cid}")]
    kb.append(ctrl)
    text = (f"📂 <b>{html.escape(path_str(cid))}</b>\n"
            f"Папок: {len(subs)} · записей: {len(items)}\n\n"
            f"Кинь сюда текст, файл или пересылку — сохраню в эту папку.")
    return text, InlineKeyboardMarkup(inline_keyboard=kb)


async def show_folder(target: Message, cid: int, page: int = 0, edit: bool = False) -> None:
    text, kb = render_folder(cid, page)
    if edit:
        try:
            await target.edit_text(text, reply_markup=kb)
            return
        except Exception:
            pass
    await target.answer(text, reply_markup=kb)


async def send_item(bot: Bot, chat_id: int, it: sqlite3.Row) -> None:
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        btn("🗑 Удалить", f"di:{it['id']}"), btn("⬅️ К папке", f"cat:{it['category_id']}:0")]])
    head = f"<b>{html.escape(it['title'] or '')}</b>\n"
    link = msg_link(it["src_chat"], it["src_msg"])
    tail = f"\n\n<a href=\"{link}\">🔗 в чате</a>" if link else ""
    body = html.escape(it["text"] or "")
    k, fid = it["kind"], it["file_id"]
    if k == "text" or not fid:
        await bot.send_message(chat_id, (head + body)[:3900] + tail, reply_markup=kb,
                               disable_web_page_preview=True)
        return
    if k in ("video_note", "sticker"):
        send = bot.send_video_note if k == "video_note" else bot.send_sticker
        await send(chat_id, fid, reply_markup=kb)
        return
    send = {"document": bot.send_document, "photo": bot.send_photo, "video": bot.send_video,
            "audio": bot.send_audio, "voice": bot.send_voice, "animation": bot.send_animation
            }.get(k, bot.send_document)
    await send(chat_id, fid, caption=(head + body)[:900] + tail, reply_markup=kb)


# ---- общие команды
@router.message(Command("myid"))
async def cmd_myid(m: Message):
    await m.reply(f"Твой id: <code>{m.from_user.id}</code>")


@router.message(Command("chatid"))
async def cmd_chatid(m: Message):
    await m.reply(f"id этого чата: <code>{m.chat.id}</code>")


@router.message(Command("check"))
async def cmd_check(m: Message, bot: Bot):
    if not can_use(m.from_user.id):
        return
    await m.reply("Проверяю…")
    await m.reply(await process_pending(bot))


# ---- команды в группе (ответом на сообщение)
GROUP = F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP})


@router.message(GROUP, Command("keep"))
async def cmd_keep(m: Message):
    t = m.reply_to_message
    if not t:
        return await m.reply("Ответь этой командой на сообщение, которое нельзя удалять.")
    db.execute("UPDATE pending SET keep=1 WHERE chat_id=? AND msg_id=?", (m.chat.id, t.message_id))
    db.commit()
    await m.reply("Ок, не трону 👌")


@router.message(GROUP, Command("pin", "save"))
async def cmd_pin_save(m: Message, command: CommandObject, bot: Bot):
    t = m.reply_to_message
    if not t:
        return await m.reply("Ответь командой на сообщение. Можно указать папку: /save Roblox/Режим/Идеи")
    row = extract(t)
    path = [p for p in (command.args or "").split("/") if p.strip()] or await suggest_path(row)
    cat = ensure_path(path)
    save_item(cat, "", row, m.chat.id, t.message_id)
    db.execute("DELETE FROM pending WHERE chat_id=? AND msg_id=?", (m.chat.id, t.message_id))
    db.commit()
    if command.command == "pin":
        try:
            await bot.pin_chat_message(m.chat.id, t.message_id, disable_notification=True)
        except Exception as e:
            return await m.reply(f"Сохранил, но закрепить не вышло: {e}")
    await m.reply(f"Сохранил в 📂 {html.escape(path_str(cat))}")


# ---- личка: база
PRIVATE = F.chat.type == ChatType.PRIVATE


@router.message(PRIVATE, CommandStart())
@router.message(PRIVATE, Command("base"))
async def cmd_base(m: Message):
    if not can_use(m.from_user.id):
        return await m.answer("Нет доступа. Попроси админа добавить твой id: /myid")
    state[m.from_user.id] = {"cat": 0, "await": None}
    await show_folder(m, 0)


@router.callback_query(F.data.startswith(("cat:", "it:", "nc:", "dc:", "ydc:", "di:", "ydi:")))
async def on_cb(cb: CallbackQuery, bot: Bot):
    uid = cb.from_user.id
    if not can_use(uid):
        return await cb.answer("Нет доступа", show_alert=True)
    parts = cb.data.split(":")
    action, arg = parts[0], int(parts[1])
    st = state.setdefault(uid, {"cat": 0, "await": None})
    st["await"] = None

    if action == "cat":
        if arg and not get_cat(arg):
            arg = 0
        st["cat"] = arg
        await show_folder(cb.message, arg, int(parts[2]) if len(parts) > 2 else 0, edit=True)
    elif action == "it":
        it = db.execute("SELECT * FROM items WHERE id=?", (arg,)).fetchone()
        if not it:
            return await cb.answer("Запись уже удалена", show_alert=True)
        await send_item(bot, cb.message.chat.id, it)
    elif action == "nc":
        st["cat"], st["await"] = arg, "newcat"
        await cb.message.answer(f"Напиши название новой папки внутри «{html.escape(path_str(arg))}»:")
    elif action == "dc":
        n = count_all(arg)
        kb = InlineKeyboardMarkup(inline_keyboard=[[btn("Да, удалить", f"ydc:{arg}"),
                                                    btn("Отмена", f"cat:{arg}:0")]])
        await cb.message.answer(f"Удалить папку «{html.escape(path_str(arg))}» со всем содержимым "
                                f"({n} записей)?", reply_markup=kb)
    elif action == "ydc":
        c = get_cat(arg)
        parent = c["parent_id"] if c else 0
        delete_cat(arg)
        st["cat"] = parent
        await cb.message.answer("Удалено 🗑")
        await show_folder(cb.message, parent)
    elif action == "di":
        kb = InlineKeyboardMarkup(inline_keyboard=[[btn("Да, удалить", f"ydi:{arg}"),
                                                    btn("Отмена", f"it:{arg}")]])
        await cb.message.answer("Удалить эту запись?", reply_markup=kb)
    elif action == "ydi":
        it = db.execute("SELECT category_id FROM items WHERE id=?", (arg,)).fetchone()
        db.execute("DELETE FROM items WHERE id=?", (arg,))
        db.commit()
        await cb.message.answer("Удалено 🗑")
        await show_folder(cb.message, it["category_id"] if it else 0)
    await cb.answer()


@router.message(PRIVATE)
async def on_private(m: Message):
    if not can_use(m.from_user.id):
        return await m.answer("Нет доступа. Твой id: " + str(m.from_user.id))
    if (m.text or "").startswith("/"):
        return
    st = state.setdefault(m.from_user.id, {"cat": 0, "await": None})
    if st.get("await") == "newcat" and m.text:
        st["await"] = None
        cid = create_cat(st["cat"], m.text.strip()[:50])
        st["cat"] = cid
        await m.answer("Папка создана ✅")
        return await show_folder(m, cid)
    row = extract(m)
    if not row["text"] and not row["file_id"]:
        return
    cat = st["cat"] if st["cat"] and get_cat(st["cat"]) else ensure_path(await suggest_path(row))
    save_item(cat, "", row)
    kb = InlineKeyboardMarkup(inline_keyboard=[[btn("📂 Открыть папку", f"cat:{cat}:0")]])
    await m.answer(f"Сохранил в 📂 {html.escape(path_str(cat))}", reply_markup=kb)


# ---- группа: все обычные сообщения -> в очередь на проверку
@router.message(GROUP)
async def on_group(m: Message):
    if GROUP_IDS and m.chat.id not in GROUP_IDS:
        return
    if m.from_user and m.from_user.is_bot:
        return
    row = extract(m)
    if not row["text"] and not row["file_id"]:
        return  # служебные сообщения (вход/выход, закреп и т.п.)
    if row["text"].startswith("/"):
        return
    db.execute(
        "INSERT OR IGNORE INTO pending(chat_id,msg_id,user_name,text,kind,file_id,file_name,ts) VALUES(?,?,?,?,?,?,?,?)",
        (m.chat.id, m.message_id, row["user_name"], row["text"], row["kind"], row["file_id"],
         row["file_name"], int(time.time())),
    )
    db.commit()


# ---------------------------------------------------------------- запуск
async def main():
    if not BOT_TOKEN:
        raise SystemExit("Нет BOT_TOKEN — впиши его в файл .env")
    if not LLM_API_KEY:
        log.warning("LLM_API_KEY не задан: бот НЕ будет удалять сообщения, только сохранять файлы.")
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    asyncio.create_task(scheduler(bot))
    log.info("Бот запущен")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
