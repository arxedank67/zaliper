import os
import asyncio
import html
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
from aiohttp import web
from telegram import Update, InputMediaPhoto
from telegram.ext import (
    Application, CommandHandler, ContextTypes
)
from twscrape import API, gather
from twscrape.models import Tweet

# --- Настройки ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
OWNER_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "1660547849"))

DATA_DIR = Path("accounts_data")
DATA_DIR.mkdir(exist_ok=True)

TEMP_DIR = Path("temp_images")
TEMP_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---- Ограничение доступа ----
_raw_allowed = os.environ.get("ALLOWED_USERS", "").strip()
ALLOWED_USERS: set[int] | None = None
if _raw_allowed:
    ALLOWED_USERS = {OWNER_USER_ID}
    for p in _raw_allowed.split(","):
        p = p.strip()
        if p.lstrip("-").isdigit():
            ALLOWED_USERS.add(int(p))
    logger.info(f"Whitelist активен: {sorted(ALLOWED_USERS)}")

MAX_USERS = 1000
DEDUP_TTL = 3 * 60 * 60
MAX_TRACKED_PER_CHAT = 3000

SEARCH_SEMAPHORE = asyncio.Semaphore(2)

http_session: aiohttp.ClientSession | None = None
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36"
}


# ---- Модель пользователя ----
@dataclass
class UserState:
    user_id: int
    username: str = ""
    x_api: API | None = None
    targets: list[int] = field(default_factory=list)
    dedup_enabled: bool = True
    sent_tweets: dict[int, float] = field(default_factory=dict)
    search_active: bool = False
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    total_searches: int = 0
    total_posts: int = 0

    @property
    def db_path(self) -> Path:
        return DATA_DIR / f"accounts_{self.user_id}.db"


USER_DATA: dict[int, UserState] = {}
SENT_MESSAGES: dict[int, list[int]] = {}


def get_user(user_id: int, username: str = "") -> UserState:
    u = USER_DATA.get(user_id)
    if u is None:
        if len(USER_DATA) >= MAX_USERS:
            raise RuntimeError("Достигнут лимит пользователей")
        u = UserState(user_id=user_id, username=username)
        USER_DATA[user_id] = u
        logger.info(f"New user: {user_id} (@{username})")
    elif username:
        u.username = username
    u.last_seen = time.time()
    return u


def get_x_api(user: UserState) -> API:
    if user.x_api is None:
        user.x_api = API(str(user.db_path))
        logger.info(f"[u{user.user_id}] X API создан: {user.db_path}")
    return user.x_api


def is_allowed(user_id: int) -> bool:
    if ALLOWED_USERS is None:
        return True
    return user_id in ALLOWED_USERS


def is_owner(user_id: int) -> bool:
    return user_id == OWNER_USER_ID


# ---- Утилиты ----
def esc(s: str) -> str:
    return html.escape(s or "")


def track_sent(chat_id: int, *messages):
    if chat_id not in SENT_MESSAGES:
        SENT_MESSAGES[chat_id] = []
    for m in messages:
        if m is None:
            continue
        mid = getattr(m, "message_id", None)
        if mid is not None:
            SENT_MESSAGES[chat_id].append(mid)
    if len(SENT_MESSAGES[chat_id]) > MAX_TRACKED_PER_CHAT:
        SENT_MESSAGES[chat_id] = SENT_MESSAGES[chat_id][-MAX_TRACKED_PER_CHAT // 2:]


# ---- Per-user dedup ----
def is_sent(user: UserState, tweet_id: int) -> bool:
    if not user.dedup_enabled:
        return False
    ts = user.sent_tweets.get(tweet_id)
    if ts is None:
        return False
    if time.time() - ts > DEDUP_TTL:
        del user.sent_tweets[tweet_id]
        return False
    return True


def mark_sent(user: UserState, tweet_id: int):
    if user.dedup_enabled:
        user.sent_tweets[tweet_id] = time.time()


def cleanup_user_dedup(user: UserState) -> int:
    now = time.time()
    expired = [tid for tid, ts in user.sent_tweets.items() if now - ts > DEDUP_TTL]
    for tid in expired:
        del user.sent_tweets[tid]
    return len(expired)


async def dedup_cleanup_loop():
    while True:
        await asyncio.sleep(30 * 60)
        total = sum(cleanup_user_dedup(u) for u in USER_DATA.values())
        if total:
            logger.info(f"Dedup cleanup: removed {total} entries")


async def user_cleanup_loop():
    while True:
        await asyncio.sleep(24 * 60 * 60)
        now = time.time()
        to_remove = []
        for uid, u in USER_DATA.items():
            if uid == OWNER_USER_ID:
                continue
            inactive = now - u.last_seen > 7 * 24 * 60 * 60
            if inactive and not u.targets and not u.sent_tweets:
                to_remove.append(uid)
        for uid in to_remove:
            u = USER_DATA.pop(uid, None)
            if u and u.db_path.exists():
                try:
                    u.db_path.unlink()
                except Exception:
                    pass
        if to_remove:
            logger.info(f"User cleanup: removed {len(to_remove)} inactive users")


# ---- X account helpers ----
async def add_x_account(user: UserState, auth_token: str, ct0: str) -> bool:
    try:
        api = get_x_api(user)
        acc_name = f"u{user.user_id}"
        await api.pool.add_account_cookies(
            acc_name,
            f"auth_token={auth_token}; ct0={ct0}"
        )
        logger.info(f"[u{user.user_id}] X аккаунт добавлен")
        return True
    except Exception as e:
        logger.error(f"[u{user.user_id}] ошибка добавления X аккаунта: {e}")
        return False


async def user_has_active_x_account(user: UserState) -> bool:
    if user.x_api is None:
        if user.db_path.exists():
            get_x_api(user)
        else:
            return False
    try:
        accounts = await user.x_api.pool.accounts_info()
        return bool(accounts) and any(a.get("active") for a in accounts)
    except Exception:
        return False


async def ensure_owner_account():
    auth = os.environ.get("X_AUTH_TOKEN")
    ct0 = os.environ.get("X_CT0")
    if not (auth and ct0):
        return
    owner = get_user(OWNER_USER_ID, "owner")
    if await user_has_active_x_account(owner):
        logger.info("Owner X аккаунт уже есть в БД.")
        return
    logger.info("Восстанавливаю owner X аккаунт из env…")
    await add_x_account(owner, auth, ct0)


# ---- Типы медиа ----
def tweet_matches_media_type(tweet: Tweet, media_type: str | None) -> bool:
    if media_type in (None, "any"):
        return True
    media = tweet.media
    if media_type == "text":
        return media is None
    if media is None:
        return False
    has_photos = bool(getattr(media, "photos", None))
    has_videos = bool(getattr(media, "videos", None))
    has_animated = bool(getattr(media, "animated", None))
    if media_type == "photo":
        return has_photos
    if media_type == "video":
        return has_videos
    if media_type == "gif":
        return has_animated
    return True


def media_type_icon(mt: str | None) -> str:
    return {
        None: "📎", "any": "📎", "photo": "🖼",
        "video": "🎬", "gif": "🎞", "text": "📝",
    }.get(mt, "📎")


# ---- Отправка ----
async def send_tweet_to(chat_id: int, photos: list, caption: str,
                        context: ContextTypes.DEFAULT_TYPE):
    if len(photos) == 1:
        with open(photos[0], "rb") as f:
            m = await context.bot.send_photo(
                chat_id=chat_id, photo=f, caption=caption, parse_mode="HTML"
            )
            track_sent(chat_id, m)
    else:
        media_group = []
        for p in photos:
            with open(p, "rb") as f:
                media_group.append(InputMediaPhoto(media=f))
        sent = await context.bot.send_media_group(chat_id=chat_id, media=media_group)
        track_sent(chat_id, *sent)
        m = await context.bot.send_message(
            chat_id=chat_id, text=caption, parse_mode="HTML"
        )
        track_sent(chat_id, m)


async def download_tweet_media(tweet: Tweet, context: ContextTypes.DEFAULT_TYPE,
                               recipients: list[int]) -> int:
    media = tweet.media
    if not media:
        return 0
    photos = getattr(media, "photos", None)
    if not photos:
        return 0

    files = []
    for i, photo in enumerate(photos[:10]):
        url = getattr(photo, "url", None)
        if not url:
            pid = getattr(photo, "id", None)
            if pid:
                url = f"https://pbs.twimg.com/media/{pid}.jpg"
        if not url:
            continue
        fp = TEMP_DIR / f"{tweet.id}_{i}.jpg"
        try:
            async with http_session.get(url, headers=HEADERS) as resp:
                if resp.status == 200:
                    with open(fp, "wb") as f:
                        f.write(await resp.read())
                    files.append(fp)
        except Exception as e:
            logger.error(f"[DL] {url}: {e}")

    if not files:
        return 0

    preview = esc(tweet.rawContent[:180]) if tweet.rawContent else ""
    caption = (
        f'🔗 <a href="https://x.com/i/status/{tweet.id}">Ссылка на пост</a>\n'
        f'📝 <i>{preview}</i>'
    )

    for cid in recipients:
        try:
            await send_tweet_to(cid, files, caption, context)
            if len(recipients) > 1:
                await asyncio.sleep(0.7)
        except Exception as e:
            logger.error(f"Отправка в {cid}: {e}")

    for p in files:
        p.unlink(missing_ok=True)

    return len(files)


# ---- Парсер /search ----
def parse_search_args(args: list):
    include_groups = []
    exclude = []
    limit = 20
    lang = None
    media_type = None
    since = None
    until = None

    for raw in args:
        arg = raw.strip()
        if not arg:
            continue
        if arg.startswith("n-") and arg[2:].isdigit():
            limit = max(1, min(100, int(arg[2:])))
            continue
        if arg.startswith("lang:") or arg.startswith("lang-"):
            code = arg.split(":", 1)[1] if ":" in arg else arg.split("-", 1)[1]
            code = code.strip().lower()
            if 2 <= len(code) <= 5:
                lang = code
            continue
        if arg.startswith("type:"):
            t = arg.split(":", 1)[1].strip().lower()
            mapping = {
                "photo": "photo", "image": "photo", "img": "photo",
                "video": "video", "vid": "video",
                "gif": "gif", "animated": "gif",
                "text": "text", "tweet": "text",
                "any": "any", "all": "any",
            }
            if t in mapping:
                media_type = mapping[t]
            continue
        if arg.startswith("since:"):
            since = arg.split(":", 1)[1].strip()
            continue
        if arg.startswith("until:"):
            until = arg.split(":", 1)[1].strip()
            continue
        if arg.startswith("-") and not arg.startswith("--") and len(arg) > 1:
            word = arg[1:].strip().lstrip("#")
            if word:
                exclude.append(word)
            continue
        parts = [p.strip().lstrip("#") for p in arg.split(",")]
        g = [p for p in parts if p]
        if g:
            include_groups.append(g)

    return include_groups, exclude, limit, lang, media_type, since, until


def tweet_has_excluded(tweet: Tweet, exclude: list) -> bool:
    if not exclude:
        return False
    text = (tweet.rawContent or "").lower()
    for w in exclude:
        w = w.lower().lstrip("#")
        if w and w in text:
            return True
    return False


def format_groups_for_display(groups: list) -> str:
    parts = []
    for g in groups:
        parts.append(g[0] if len(g) == 1 else " + ".join(g))
    return "  |  ".join(parts)


def extract_chat_id_from_forward(message):
    origin = getattr(message, "forward_origin", None)
    if not origin:
        return None, "Сообщение не переслано."
    sender_user = getattr(origin, "sender_user", None)
    if sender_user:
        if getattr(sender_user, "is_bot", False):
            return None, "Это сообщение от бота. Перешлите от получателя."
        return sender_user.id, ""
    if origin.__class__.__name__ == "MessageOriginHiddenUser":
        return None, ("Получатель скрыл пересылку. "
                      "Попросите прислать chat_id через /my_id.")
    sender_chat = getattr(origin, "sender_chat", None)
    if sender_chat:
        return sender_chat.id, ""
    chat = getattr(origin, "chat", None)
    if chat:
        return chat.id, ""
    return None, "Не удалось определить отправителя."


# ---- Help ----
def help_text(user_id: int) -> str:
    base = (
        "🤖 <b>X Scroller Bot</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "⚙️ <b>Первый запуск:</b>\n"
        "   <b>/add_account</b> <code>&lt;auth_token&gt; &lt;ct0&gt;</code> —\n"
        "   добавьте свой X-аккаунт (cookies).\n"
        "   <i>Без него поиск работать не будет.</i>\n"
        "   📱 С телефона? Смотрите <b>/help_mobile</b>\n\n"
        "🔎 <b>/search</b> <code>&lt;теги&gt;</code> <code>[-искл]</code> "
        "<code>[n-N]</code> <code>[lang:xx]</code> <code>[type:T]</code>\n"
        "   <code>[since:ГГГГ-ММ-ДД]</code> <code>[until:ГГГГ-ММ-ДД]</code>\n"
        "   • <code>apple sun</code> → apple <b>ИЛИ</b> sun\n"
        "   • <code>apple,sun</code> → apple <b>И</b> sun\n"
        "   • <code>-nsfw</code> → исключить\n"
        "   • <code>n-10</code> → лимит (1–100)\n"
        "   • <code>lang:en</code> → язык\n"
        "   • <code>type:photo|video|gif|text|any</code>\n"
        "   • <code>since:2026-09-26</code> → начиная с даты\n"
        "   • <code>until:2026-09-27</code> → до даты\n\n"
        "⏹ <b>/stop</b> — прервать свой поиск\n"
        "🗑 <b>/clear</b> — удалить свои сообщения бота\n"
        "♻️ <b>/dedup</b> <code>on|off</code> — вкл/выкл дедупликацию\n"
        "🧹 <b>/clear_dedup</b> — сбросить память дедупликации\n"
        "📊 <b>/my_status</b> — статус вашего X-аккаунта\n"
        "📱 <b>/help_mobile</b> — как получить cookies с телефона\n\n"
        "📨 <b>Пересылка контактам</b>\n"
        "   • <b>/add_target</b> — ответом на пересланное\n"
        "     или <code>/add_target &lt;chat_id&gt;</code>\n"
        "   • <b>/targets</b> — список\n"
        "   • <b>/remove_target</b> <code>&lt;chat_id&gt;</code>\n\n"
        "🆔 <b>/my_id</b> — ваш chat_id\n"
        "❓ <b>/help</b> — это меню\n"
    )

    if is_owner(user_id):
        base += (
            "\n━━━━━━━━━━━━━━━━━━━━━\n"
            "👑 <b>Админ-команды</b>\n"
            "   • <b>/status</b> — глобальная статистика\n"
        )

    base += (
        "\n━━━━━━━━━━━━━━━━━━━━━\n"
        "🔐 <b>Приватность:</b> ваш X-аккаунт, цели и дедупликация "
        "хранятся отдельно и не видны другим.\n"
    )
    return base


X_ACCOUNT_HELP = (
    "❌ <b>У вас нет X-аккаунта</b>\n\n"
    "<b>Как добавить:</b>\n"
    "1. Откройте x.com в браузере (где вы залогинены).\n"
    "2. Нажмите <b>F12</b> → <b>Application</b> → <b>Cookies</b> → "
    "<code>https://x.com</code>.\n"
    "3. Скопируйте два значения:\n"
    "   • <code>auth_token</code>\n"
    "   • <code>ct0</code>\n"
    "4. Отправьте боту:\n"
    "   <code>/add_account ВАШ_auth_token ВАШ_ct0</code>\n\n"
    "📱 <b>С телефона?</b> Введите <b>/help_mobile</b> — там пошаговая инструкция.\n\n"
    "<i>⚠️ Cookies — это доступ к вашему аккаунту X. "
    "Используйте свой аккаунт, не чужой.</i>"
)


MOBILE_HELP_TEXT = (
    "📱 <b>Как получить cookies X с телефона</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━\n\n"
    "<b>Способ 1 — Kiwi Browser (Android) ⭐ рекомендуем</b>\n\n"
    "1️⃣ Установите <b>Kiwi Browser</b> из Google Play.\n"
    "2️⃣ Откройте в нём Chrome Web Store и установите расширение\n"
    "    <b>Cookie-Editor</b> (от Kenny Do).\n"
    "3️⃣ Зайдите на <b>x.com</b> и войдите в свой аккаунт.\n"
    "4️⃣ Нажмите на иконку <b>Cookie-Editor</b> в браузере.\n"
    "5️⃣ Найдите в списке:\n"
    "    • <code>auth_token</code> — скопируйте значение\n"
    "    • <code>ct0</code> — скопируйте значение\n"
    "6️⃣ Отправьте боту:\n"
    "    <code>/add_account ВАШ_auth_token ВАШ_ct0</code>\n\n"
    "━━━━━━━━━━━━━━━━━━━━━\n"
    "<b>Способ 2 — Orion Browser (iOS)</b>\n\n"
    "1️⃣ Установите <b>Orion Browser</b> из App Store.\n"
    "2️⃣ Установите расширение <b>Cookie-Editor</b>\n"
    "    (Orion поддерживает расширения Chrome/Safari).\n"
    "3️⃣ Войдите в <b>x.com</b> прямо в Orion.\n"
    "4️⃣ Откройте Cookie-Editor и скопируйте\n"
    "    значения <code>auth_token</code> и <code>ct0</code>.\n"
    "5️⃣ Отправьте боту <code>/add_account</code> с этими значениями.\n\n"
    "━━━━━━━━━━━━━━━━━━━━━\n"
    "<b>Способ 3 — через ПК (если есть доступ)</b>\n\n"
    "1️⃣ Откройте x.com на компьютере (в браузере где залогинены).\n"
    "2️⃣ F12 → <b>Application</b> → <b>Cookies</b> → <code>https://x.com</code>\n"
    "3️⃣ Скопируйте <code>auth_token</code> и <code>ct0</code>.\n"
    "4️⃣ Перешлите их боту (можно с любого устройства).\n\n"
    "━━━━━━━━━━━━━━━━━━━━━\n"
    "⚠️ <b>Важно:</b>\n"
    "• <code>auth_token</code> — длинная строка (~40 символов)\n"
    "• <code>ct0</code> — длинная строка (~160 символов)\n"
    "• Копируйте <b>только значение</b>, без имени ключа\n"
    "• Бот сам удалит ваше сообщение с cookies\n\n"
    "❓ Не получилось? Попросите владельца бота помочь."
)


# ---- Handlers ----
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or ""
    chat_id = update.effective_chat.id

    if not is_allowed(user_id):
        m = await update.message.reply_text(
            "⛔ Доступ ограничен. Обратитесь к владельцу бота.\n\n"
            f"🆔 Ваш ID: <code>{user_id}</code>",
            parse_mode="HTML"
        )
        track_sent(chat_id, m)
        return

    get_user(user_id, username)
    m = await update.message.reply_text(
        f"👋 Привет, {esc(update.effective_user.first_name or 'друг')}!\n\n"
        + help_text(user_id),
        parse_mode="HTML"
    )
    track_sent(chat_id, m)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if not is_allowed(user_id):
        m = await update.message.reply_text("⛔ Доступ ограничен.")
        track_sent(chat_id, m)
        return
    get_user(user_id, update.effective_user.username or "")
    m = await update.message.reply_text(help_text(user_id), parse_mode="HTML")
    track_sent(chat_id, m)


async def help_mobile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if not is_allowed(user_id):
        m = await update.message.reply_text("⛔ Доступ ограничен.")
        track_sent(chat_id, m)
        return
    m = await update.message.reply_text(MOBILE_HELP_TEXT, parse_mode="HTML")
    track_sent(chat_id, m)


async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or "—"
    chat_id = update.effective_chat.id
    m = await update.message.reply_text(
        f"🆔 Ваш <code>chat_id</code>: <code>{user_id}</code>\n"
        f"👤 Username: @{esc(username)}",
        parse_mode="HTML"
    )
    track_sent(chat_id, m)


async def add_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if not is_allowed(user_id):
        m = await update.message.reply_text("⛔ Доступ ограничен.")
        track_sent(chat_id, m)
        return

    user = get_user(user_id, update.effective_user.username or "")

    if len(context.args) < 2:
        m = await update.message.reply_text(
            "❌ <b>Использование:</b>\n"
            "<code>/add_account &lt;auth_token&gt; &lt;ct0&gt;</code>\n\n"
            "<b>Как получить cookies:</b>\n"
            "• 📱 С телефона → <b>/help_mobile</b>\n"
            "• 💻 С ПК → F12 → Application → Cookies → x.com",
            parse_mode="HTML"
        )
        track_sent(chat_id, m)
        return

    auth_token, ct0 = context.args[0], context.args[1]

    try:
        await update.message.delete()
    except Exception:
        pass

    status_msg = await update.message.reply_text("⏳ Проверяю X-аккаунт…")
    ok = await add_x_account(user, auth_token, ct0)

    if not ok:
        await status_msg.edit_text("❌ Не удалось добавить X-аккаунт. Проверьте логи.")
        return

    try:
        api = get_x_api(user)
        accounts = await api.pool.accounts_info()
        active = [a for a in accounts if a.get("active")]
        if not active:
            await status_msg.edit_text(
                "⚠️ Cookies добавлены, но аккаунт помечен как неактивный.\n"
                "Возможно, они устарели. Попробуйте заново."
            )
            return
    except Exception as e:
        logger.error(f"[u{user.user_id}] проверка аккаунта: {e}")

    await status_msg.edit_text(
        "✅ <b>X-аккаунт добавлен!</b>\n\n"
        "Теперь можно искать: <code>/search arknights n-5</code>",
        parse_mode="HTML"
    )


async def my_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if not is_allowed(user_id):
        return
    user = get_user(user_id, update.effective_user.username or "")

    lines = ["📊 <b>Ваш статус</b>", "━━━━━━━━━━━━━━━━━━━━━"]

    if await user_has_active_x_account(user):
        try:
            accounts = await user.x_api.pool.accounts_info()
            lines.append("👤 <b>X-аккаунт:</b>")
            for acc in accounts:
                icon = "✅" if acc.get("active") else "❌"
                lines.append(f"   {icon} <code>{esc(acc.get('username', '?'))}</code>")
        except Exception:
            lines.append("👤 <b>X-аккаунт:</b> ✅ (активен)")
    else:
        lines.append("👤 <b>X-аккаунт:</b> ❌ не добавлен")
        lines.append("   <i>Используйте /add_account</i>")
        lines.append("   <i>📱 с телефона → /help_mobile</i>")

    lines.append("")
    lines.append(f"🔍 Поисков всего: <b>{user.total_searches}</b>")
    lines.append(f"📤 Отправлено постов: <b>{user.total_posts}</b>")
    lines.append(f"📨 Получателей: <b>{len(user.targets)}</b>")
    lines.append(f"♻️ Дедуп: {'🟢 вкл' if user.dedup_enabled else '🔴 выкл'} "
                 f"(<b>{len(user.sent_tweets)}</b> записей)")

    m = await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    track_sent(chat_id, m)


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if not is_owner(user_id):
        m = await update.message.reply_text("⛔ Только владелец бота.")
        track_sent(chat_id, m)
        return

    total_dedup = sum(len(u.sent_tweets) for u in USER_DATA.values())
    active_searches = sum(1 for u in USER_DATA.values() if u.search_active)
    total_posts = sum(u.total_posts for u in USER_DATA.values())
    total_searches = sum(u.total_searches for u in USER_DATA.values())

    with_x = sum(1 for u in USER_DATA.values() if u.db_path.exists())

    lines = [
        "📊 <b>Глобальная статистика</b>",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"👥 Пользователей: <b>{len(USER_DATA)}</b>",
        f"👤 Из них с X-аккаунтом: <b>{with_x}</b>",
        f"🔍 Активных поисков: <b>{active_searches}</b>",
        f"📈 Всего поисков: <b>{total_searches}</b>",
        f"📤 Отправлено постов: <b>{total_posts}</b>",
        f"♻️ Записей в дедупе: <b>{total_dedup}</b>",
        "",
        "🕒 <b>Последние 5 пользователей:</b>",
    ]
    recent = sorted(USER_DATA.values(), key=lambda u: u.last_seen, reverse=True)[:5]
    for u in recent:
        mark = "👑" if u.user_id == OWNER_USER_ID else "👤"
        has_x = "🟢" if u.db_path.exists() else "🔴"
        lines.append(
            f"   {mark} {has_x} <code>{u.user_id}</code> "
            f"(@{esc(u.username) or '—'}) — {u.total_searches}🔍"
        )

    m = await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    track_sent(chat_id, m)


async def dedup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if not is_allowed(user_id):
        return
    user = get_user(user_id, update.effective_user.username or "")

    if not context.args:
        state = "🟢 включена" if user.dedup_enabled else "🔴 выключена"
        m = await update.message.reply_text(
            f"♻️ <b>Дедупликация</b>: {state}\n"
            f"📦 Записей: <b>{len(user.sent_tweets)}</b>\n"
            f"⏱ TTL: <b>3 часа</b>\n\n"
            f"<i>Переключить:</i> <code>/dedup on</code> | <code>/dedup off</code>",
            parse_mode="HTML"
        )
        track_sent(chat_id, m)
        return

    arg = context.args[0].strip().lower()
    if arg in ("on", "1", "true", "вкл", "включить"):
        user.dedup_enabled = True
        m = await update.message.reply_text("🟢 Дедупликация включена.")
    elif arg in ("off", "0", "false", "выкл", "выключить"):
        user.dedup_enabled = False
        m = await update.message.reply_text(
            "🔴 Дедупликация выключена. Посты могут приходить повторно."
        )
    else:
        m = await update.message.reply_text("❌ Используйте: /dedup on | off")
    track_sent(chat_id, m)


async def clear_dedup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if not is_allowed(user_id):
        return
    user = get_user(user_id)
    n = len(user.sent_tweets)
    user.sent_tweets.clear()
    m = await update.message.reply_text(
        f"♻️ Память дедупликации сброшена. Удалено: <b>{n}</b>",
        parse_mode="HTML"
    )
    track_sent(chat_id, m)


async def add_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id_self = update.effective_chat.id
    if not is_allowed(user_id):
        return
    user = get_user(user_id, update.effective_user.username or "")

    target = None
    if update.message.reply_to_message:
        target, err = extract_chat_id_from_forward(update.message.reply_to_message)
        if target is None:
            m = await update.message.reply_text(
                f"❌ {err}\n\n"
                "Добавьте вручную: <code>/add_target &lt;chat_id&gt;</code>",
                parse_mode="HTML"
            )
            track_sent(chat_id_self, m)
            return
    elif context.args and context.args[0].lstrip("-").isdigit():
        target = int(context.args[0])
    else:
        m = await update.message.reply_text(
            "❌ <code>/add_target &lt;chat_id&gt;</code> или ответом на пересланное."
        )
        track_sent(chat_id_self, m)
        return

    if target == user_id:
        m = await update.message.reply_text("⚠️ Это ваш ID.")
        track_sent(chat_id_self, m)
        return
    if target in user.targets:
        m = await update.message.reply_text(f"ℹ️ <code>{target}</code> уже в списке.", parse_mode="HTML")
        track_sent(chat_id_self, m)
        return

    user.targets.append(target)

    test_ok = True
    try:
        tm = await context.bot.send_message(
            chat_id=target,
            text="🔔 Вас добавили в список получателей постов X Scroller.",
        )
        track_sent(target, tm)
    except Exception as e:
        test_ok = False
        logger.warning(f"Не доставили {target}: {e}")

    if test_ok:
        m = await update.message.reply_text(
            f"✅ Добавлен: <code>{target}</code>\n"
            f"Всего: <b>{len(user.targets)}</b>",
            parse_mode="HTML"
        )
    else:
        m = await update.message.reply_text(
            f"⚠️ Добавлен <code>{target}</code>, но бот не смог написать.\n"
            f"Попросите получателя нажать Start в боте.",
            parse_mode="HTML"
        )
    track_sent(chat_id_self, m)


async def list_targets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if not is_allowed(user_id):
        return
    user = get_user(user_id)

    if not user.targets:
        m = await update.message.reply_text("📭 У вас нет дополнительных получателей.")
        track_sent(chat_id, m)
        return

    lines = [
        "📨 <b>Ваши получатели</b>",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"👑 Вы: <code>{user_id}</code> (всегда)",
    ]
    for i, cid in enumerate(user.targets, 1):
        lines.append(f"{i}. <code>{cid}</code>")

    m = await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    track_sent(chat_id, m)


async def remove_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if not is_allowed(user_id):
        return
    user = get_user(user_id)

    if not context.args or not context.args[0].lstrip("-").isdigit():
        m = await update.message.reply_text("❌ <code>/remove_target &lt;chat_id&gt;</code>", parse_mode="HTML")
        track_sent(chat_id, m)
        return

    target = int(context.args[0])
    if target not in user.targets:
        m = await update.message.reply_text(f"🤷 <code>{target}</code> нет в списке.", parse_mode="HTML")
        track_sent(chat_id, m)
        return

    user.targets.remove(target)
    m = await update.message.reply_text(f"🗑 Удалён: <code>{target}</code>", parse_mode="HTML")
    track_sent(chat_id, m)


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if not is_allowed(user_id):
        return
    user = get_user(user_id)

    if user.search_active:
        user.stop_event.set()
        m = await update.message.reply_text("⏹ Останавливаю ваш поиск…")
    else:
        m = await update.message.reply_text("🤷 У вас сейчас нет активного поиска.")
    track_sent(chat_id, m)


async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if not is_allowed(user_id):
        return

    ids = SENT_MESSAGES.pop(chat_id, [])

    if not ids:
        m = await update.message.reply_text(
            "🤷 Нечего удалять. Список ведётся с момента запуска бота."
        )
        track_sent(chat_id, m)
        return

    try:
        await update.message.delete()
    except Exception:
        pass

    deleted = 0
    failed = 0
    for mid in ids:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=mid)
            deleted += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)

    m = await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "🗑 <b>Очистка завершена</b>\n"
            f"📨 Удалено: <b>{deleted}</b>\n"
            f"⚠️ Не удалено: <b>{failed}</b>"
        ),
        parse_mode="HTML"
    )
    track_sent(chat_id, m)


async def search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    if not is_allowed(user_id):
        m = await update.message.reply_text("⛔ Доступ ограничен.")
        track_sent(chat_id, m)
        return

    user = get_user(user_id, update.effective_user.username or "")

    if not await user_has_active_x_account(user):
        m = await update.message.reply_text(X_ACCOUNT_HELP, parse_mode="HTML")
        track_sent(chat_id, m)
        return

    if user.search_active:
        m = await update.message.reply_text("⚠️ У вас уже идёт поиск. /stop чтобы прервать.")
        track_sent(chat_id, m)
        return

    if not context.args:
        m = await update.message.reply_text(
            "❌ Укажите теги. Пример:\n"
            "<code>/search arknights since:2026-09-26 type:photo n-5 lang:en</code>",
            parse_mode="HTML"
        )
        track_sent(chat_id, m)
        return

    (groups, exclude, limit, lang, media_type,
     since, until) = parse_search_args(context.args)

    if not groups:
        m = await update.message.reply_text("❌ Не нашёл теги. Проверьте формат.")
        track_sent(chat_id, m)
        return

    user.search_active = True
    user.stop_event.clear()
    user.total_searches += 1

    tags_display = format_groups_for_display(groups)
    exclude_display = ", ".join(exclude) if exclude else "—"
    targets_info = f"👑 + {len(user.targets)} 👥" if user.targets else "👑 только вы"
    dedup_state = (
        f"🟢 вкл ({len(user.sent_tweets)} зап.)"
        if user.dedup_enabled else "🔴 выкл"
    )
    date_info = ""
    if since or until:
        date_info = f"\n📅 <b>Даты:</b> {since or '…'} → {until or 'сейчас'}"

    header = (
        "🔍 <b>Поиск запущен</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"📝 <b>Теги:</b> <code>{esc(tags_display)}</code>\n"
        f"🚫 <b>Исключить:</b> <code>{esc(exclude_display)}</code>\n"
        f"{media_type_icon(media_type)} <b>Тип:</b> <code>{media_type or 'any'}</code>\n"
        f"🌐 <b>Язык:</b> <code>{esc(lang or 'любой')}</code>\n"
        f"📊 <b>Лимит:</b> <code>{limit}</code>{date_info}\n"
        f"📨 <b>Получатели:</b> {targets_info}\n"
        f"♻️ <b>Дед.:</b> {dedup_state}"
    )
    msg = await update.message.reply_text(header, parse_mode="HTML")
    track_sent(chat_id, msg)

    asyncio.create_task(
        _do_search(user, chat_id, msg, context, groups, exclude, limit, lang,
                   media_type, since, until)
    )


async def _do_search(user: UserState, chat_id: int, msg,
                     context: ContextTypes.DEFAULT_TYPE,
                     groups, exclude, limit, lang, media_type,
                     since, until):
    try:
        group_strs = []
        for g in groups:
            group_strs.append(g[0] if len(g) == 1 else "(" + " ".join(g) + ")")

        include_part = " OR ".join(group_strs)
        parts = [f"({include_part})"]
        for w in exclude:
            parts.append(f"-{w}")
        if media_type in (None, "any", "photo", "video", "gif"):
            parts.append("filter:images")
        if lang:
            parts.append(f"lang:{lang}")
        if since:
            parts.append(f"since:{since}")
        if until:
            parts.append(f"until:{until}")

        full_query = " ".join(parts)
        logger.info(f"[u{user.user_id}] query: {full_query} (limit={limit})")

        fetch_limit = min(limit * 3 + len(exclude) * 5, 100)

        api = get_x_api(user)
        async with SEARCH_SEMAPHORE:
            tweets = await gather(api.search(full_query, limit=fetch_limit))

        logger.info(f"[u{user.user_id}] got {len(tweets)} tweets")

        if exclude:
            tweets = [t for t in tweets if not tweet_has_excluded(t, exclude)]

        if media_type:
            tweets = [t for t in tweets if tweet_matches_media_type(t, media_type)]

        skipped = 0
        if user.dedup_enabled:
            before = len(tweets)
            tweets = [t for t in tweets if not is_sent(user, t.id)]
            skipped = before - len(tweets)

        if lang:
            tweets = [t for t in tweets
                      if getattr(t, "lang", None) in (None, lang)]

        if len(tweets) > limit:
            tweets = tweets[:limit]

        if not tweets:
            txt = "😕 Ничего не найдено."
            if skipped:
                txt = f"♻️ Все {skipped} постов уже были отправлены ранее."
            await msg.edit_text(txt)
            return

        total = len(tweets)
        extra = f"\n♻️ Пропущено (дед.): <b>{skipped}</b>" if skipped else ""
        await msg.edit_text(
            f"✅ Найдено <b>{total}</b> новых постов.{extra}\n"
            f"📥 Скачиваю картинки…\n\n"
            f"<i>Чтобы остановить — /stop</i>",
            parse_mode="HTML"
        )

        recipients = [user.user_id] + user.targets

        total_photos = 0
        processed = 0
        stopped = False

        for tweet in tweets:
            if user.stop_event.is_set():
                stopped = True
                break
            cnt = await download_tweet_media(tweet, context, recipients)
            total_photos += cnt
            processed += 1
            if cnt > 0:
                mark_sent(user, tweet.id)
                user.total_posts += 1

            if processed % 5 == 0 and processed < total:
                try:
                    await msg.edit_text(
                        f"📥 <b>Прогресс:</b> {processed}/{total}\n"
                        f"🖼 Картинок: <b>{total_photos}</b>\n"
                        f"♻️ В дедупе: <b>{len(user.sent_tweets)}</b>\n\n"
                        f"<i>/stop чтобы прервать</i>",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
            await asyncio.sleep(1.5)

        tail = (
            f"📥 Обработано: <b>{processed}</b>/{total}\n"
            f"🖼 Картинок: <b>{total_photos}</b>\n"
            f"♻️ В дедупе: <b>{len(user.sent_tweets)}</b>"
        )
        if stopped:
            await msg.edit_text(
                "⏹ <b>Остановлено вами</b>\n━━━━━━━━━━━━━━━━━━━━━\n" + tail,
                parse_mode="HTML"
            )
        else:
            await msg.edit_text(
                "✅ <b>Готово!</b>\n━━━━━━━━━━━━━━━━━━━━━\n" + tail,
                parse_mode="HTML"
            )

    except Exception as e:
        logger.error(f"[u{user.user_id}] search error: {e}")
        try:
            await msg.edit_text(
                f"❌ Ошибка: <code>{esc(str(e)[:200])}</code>",
                parse_mode="HTML"
            )
        except Exception:
            pass
    finally:
        user.search_active = False
        user.stop_event.clear()


# ---- Веб-сервер ----
async def health_check(request):
    return web.Response(text="Bot is alive!")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Web server on port {port}")


# ---- Запуск ----
async def run_bot():
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("help_mobile", help_mobile))
    application.add_handler(CommandHandler("my_id", my_id))
    application.add_handler(CommandHandler("my_status", my_status))
    application.add_handler(CommandHandler("add_account", add_account))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("search", search))
    application.add_handler(CommandHandler("stop", stop))
    application.add_handler(CommandHandler("clear", clear))
    application.add_handler(CommandHandler("dedup", dedup_cmd))
    application.add_handler(CommandHandler("clear_dedup", clear_dedup))
    application.add_handler(CommandHandler("add_target", add_target))
    application.add_handler(CommandHandler("targets", list_targets))
    application.add_handler(CommandHandler("remove_target", remove_target))

    await application.initialize()
    await application.start()
    await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)

    await ensure_owner_account()

    get_user(OWNER_USER_ID, "owner")

    logger.info("Bot started. Multi-user mode.")

    try:
        await asyncio.Event().wait()
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()


async def main_async():
    global http_session
    http_session = aiohttp.ClientSession()
    try:
        await asyncio.gather(
            start_web_server(),
            run_bot(),
            dedup_cleanup_loop(),
            user_cleanup_loop(),
        )
    finally:
        await http_session.close()


if __name__ == "__main__":
    print("=== START ===", flush=True)
    print("BOT_TOKEN set:", bool(BOT_TOKEN), flush=True)
    print("OWNER_USER_ID:", OWNER_USER_ID, flush=True)

    if not BOT_TOKEN:
        logger.error("BOT_TOKEN не задан!")
    else:
        try:
            asyncio.run(main_async())
        except (KeyboardInterrupt, SystemExit):
            pass
