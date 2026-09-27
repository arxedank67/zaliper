import os
import asyncio
import html
import logging
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
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "1660547849"))

TEMP_DIR = Path("temp_images")
TEMP_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

api = API("accounts.db")

stop_event = asyncio.Event()
_search_active = False
EXTRA_TARGETS: list[int] = []

# Список ID сообщений, отправленных ботом, по chat_id
# Формат: {chat_id: [message_id, message_id, ...]}
SENT_MESSAGES: dict[int, list[int]] = {}
MAX_TRACKED_PER_CHAT = 3000

http_session: aiohttp.ClientSession | None = None

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36"
}


def esc(s: str) -> str:
    return html.escape(s or "")


def track_sent(chat_id: int, *messages):
    """Запоминает ID отправленных ботом сообщений для будущего /clear."""
    if chat_id not in SENT_MESSAGES:
        SENT_MESSAGES[chat_id] = []

    for m in messages:
        if m is None:
            continue
        # send_media_group возвращает list — уже распакован вызывающим
        mid = getattr(m, "message_id", None)
        if mid is not None:
            SENT_MESSAGES[chat_id].append(mid)

    # Не даём списку расти бесконечно
    if len(SENT_MESSAGES[chat_id]) > MAX_TRACKED_PER_CHAT:
        SENT_MESSAGES[chat_id] = SENT_MESSAGES[chat_id][-MAX_TRACKED_PER_CHAT // 2:]


def load_targets_from_env():
    raw = os.environ.get("TARGET_CHAT_IDS", "").strip()
    if not raw:
        return
    for part in raw.split(","):
        p = part.strip()
        if p.lstrip("-").isdigit():
            cid = int(p)
            if cid != ALLOWED_USER_ID and cid not in EXTRA_TARGETS:
                EXTRA_TARGETS.append(cid)
    if EXTRA_TARGETS:
        logger.info(f"Загружено получателей из env: {EXTRA_TARGETS}")


# --- Twitter helpers ---

async def add_twitter_account(auth_token: str, ct0: str):
    try:
        await api.pool.add_account_cookies(
            "main_account",
            f"auth_token={auth_token}; ct0={ct0}"
        )
        logger.info("Аккаунт X успешно добавлен.")
        return True
    except Exception as e:
        logger.error(f"Ошибка добавления аккаунта: {e}")
        return False


async def ensure_account():
    try:
        accounts = await api.pool.accounts_info()
        if accounts and any(acc.get("active") for acc in accounts):
            logger.info(f"Активных аккаунтов X: {len(accounts)}")
            return
    except Exception as e:
        logger.warning(f"Не удалось прочитать пул аккаунтов: {e}")

    auth = os.environ.get("X_AUTH_TOKEN")
    ct0 = os.environ.get("X_CT0")
    if auth and ct0:
        logger.info("Аккаунт не найден, восстанавливаю из env…")
        await add_twitter_account(auth, ct0)
    else:
        logger.warning(
            "Аккаунт X не найден. Добавьте через /add_account "
            "или задайте X_AUTH_TOKEN + X_CT0."
        )


async def send_tweet_to(chat_id: int, photos_to_send: list, caption: str,
                        context: ContextTypes.DEFAULT_TYPE):
    """Отправляет один пост одному получателю. Трекает message_id для /clear."""
    if len(photos_to_send) == 1:
        with open(photos_to_send[0], "rb") as f:
            m = await context.bot.send_photo(
                chat_id=chat_id,
                photo=f,
                caption=caption,
                parse_mode="HTML"
            )
            track_sent(chat_id, m)
    else:
        media_group = []
        for path in photos_to_send:
            with open(path, "rb") as f:
                media_group.append(InputMediaPhoto(media=f))
        sent = await context.bot.send_media_group(chat_id=chat_id, media=media_group)
        # sent — list[Message]
        track_sent(chat_id, *sent)
        m = await context.bot.send_message(
            chat_id=chat_id,
            text=caption,
            parse_mode="HTML"
        )
        track_sent(chat_id, m)


async def download_tweet_media(tweet: Tweet, context: ContextTypes.DEFAULT_TYPE):
    media_list = tweet.media
    if not media_list:
        return 0

    photos = getattr(media_list, "photos", None)
    if not photos:
        return 0

    photos_to_send = []
    for i, photo in enumerate(photos[:10]):
        url = getattr(photo, "url", None)
        if not url:
            pid = getattr(photo, "id", None)
            if pid:
                url = f"https://pbs.twimg.com/media/{pid}.jpg"
        if not url:
            continue

        file_path = TEMP_DIR / f"{tweet.id}_{i}.jpg"
        try:
            async with http_session.get(url, headers=HEADERS) as resp:
                if resp.status == 200:
                    with open(file_path, "wb") as f:
                        f.write(await resp.read())
                    photos_to_send.append(file_path)
                else:
                    logger.error(f"[DEBUG] HTTP {resp.status} для {url}")
        except Exception as e:
            logger.error(f"[DEBUG] Исключение при скачивании {url}: {e}")

    if not photos_to_send:
        return 0

    text_preview = esc(tweet.rawContent[:180]) if tweet.rawContent else ""
    caption = (
        f'🔗 <a href="https://x.com/i/status/{tweet.id}">Ссылка на пост</a>\n'
        f'📝 <i>{text_preview}</i>'
    )

    recipients = [ALLOWED_USER_ID] + EXTRA_TARGETS

    for chat_id in recipients:
        try:
            await send_tweet_to(chat_id, photos_to_send, caption, context)
            if len(recipients) > 1:
                await asyncio.sleep(0.7)
        except Exception as e:
            logger.error(f"Ошибка отправки в {chat_id}: {e}")

    for path in photos_to_send:
        path.unlink(missing_ok=True)

    return len(photos_to_send)


# --- Парсер ---

def parse_search_args(args: list) -> tuple[list, list, int, str | None]:
    include_groups = []
    exclude = []
    limit = 20
    lang = None

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

        if arg.startswith("-") and not arg.startswith("--") and len(arg) > 1:
            word = arg[1:].strip().lstrip("#")
            if word:
                exclude.append(word)
            continue

        parts = [p.strip().lstrip("#") for p in arg.split(",")]
        group = [p for p in parts if p]
        if group:
            include_groups.append(group)

    return include_groups, exclude, limit, lang


def tweet_has_excluded(tweet: Tweet, exclude: list) -> bool:
    if not exclude:
        return False
    text = (tweet.rawContent or "").lower()
    for word in exclude:
        w = word.lower().lstrip("#")
        if w and w in text:
            return True
    return False


def format_groups_for_display(groups: list) -> str:
    parts = []
    for g in groups:
        if len(g) == 1:
            parts.append(g[0])
        else:
            parts.append(" + ".join(g))
    return "  |  ".join(parts)


def extract_chat_id_from_forward(message) -> tuple[int | None, str]:
    origin = getattr(message, "forward_origin", None)
    if not origin:
        return None, "Сообщение не переслано."

    origin_type = origin.__class__.__name__
    logger.info(f"forward_origin type: {origin_type}")

    sender_user = getattr(origin, "sender_user", None)
    if sender_user:
        if getattr(sender_user, "is_bot", False):
            return None, (
                "Это сообщение переслано от <b>бота</b>, а не от человека.\n\n"
                "Перешли сообщение <u>от получателя</u>, а не мой ответ ему."
            )
        return sender_user.id, ""

    if origin_type == "MessageOriginHiddenUser":
        return None, (
            "Получатель <b>скрыл пересылку</b> своих сообщений.\n\n"
            "Попросите его прислать вам его <code>chat_id</code> "
            "(он может узнать через <code>/my_id</code>) "
            "и добавьте вручную: <code>/add_target &lt;chat_id&gt;</code>."
        )

    sender_chat = getattr(origin, "sender_chat", None)
    if sender_chat:
        return sender_chat.id, ""

    chat = getattr(origin, "chat", None)
    if chat:
        return chat.id, ""

    return None, "Не удалось определить отправителя пересланного сообщения."


# --- Команды ---

OWNER_HELP = (
    "🤖 <b>X Scroller Bot</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━\n\n"
    "🔎 <b>/search</b> <code>&lt;теги&gt;</code> <code>[-искл]</code> <code>[n-N]</code> <code>[lang:xx]</code>\n"
    "   • <code>apple sun</code> → apple <b>ИЛИ</b> sun\n"
    "   • <code>apple,sun</code> → apple <b>И</b> sun\n"
    "   • <code>-nsfw</code> → исключить\n"
    "   • <code>n-10</code> → лимит 10\n"
    "   • <code>lang:en</code> → англ. язык\n\n"
    "⏹ <b>/stop</b> — прервать поиск\n"
    "🗑 <b>/clear</b> — удалить все сообщения бота и временные файлы\n\n"
    "📨 <b>Пересылка контактам</b>\n"
    "   • <b>/add_target</b> — <i>ответом на пересланное сообщение от получателя</i>,\n"
    "     либо <code>/add_target &lt;chat_id&gt;</code>\n"
    "   • <b>/targets</b> — список получателей\n"
    "   • <b>/remove_target</b> <code>&lt;chat_id&gt;</code> — убрать\n\n"
    "👤 <b>/add_account</b> <code>&lt;auth_token&gt; &lt;ct0&gt;</code>\n"
    "📊 <b>/status</b> — статус аккаунтов X\n"
    "🆔 <b>/my_id</b> — ваш chat_id\n"
    "❓ <b>/help</b> — это меню\n"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if user_id == ALLOWED_USER_ID:
        m = await update.message.reply_text(OWNER_HELP, parse_mode="HTML")
        track_sent(chat_id, m)
        return

    m = await update.message.reply_text(
        "👋 Привет! Этот бот — <b>приватный</b>.\n\n"
        f"🆔 Ваш <code>chat_id</code>: <code>{user_id}</code>\n\n"
        "Передайте его владельцу бота, чтобы получать от него посты.\n"
        "<i>Команды поиска доступны только владельцу.</i>",
        parse_mode="HTML"
    )
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


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        await my_id(update, context)
        return
    chat_id = update.effective_chat.id
    m = await update.message.reply_text(OWNER_HELP, parse_mode="HTML")
    track_sent(chat_id, m)


async def add_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return

    chat_id = update.effective_chat.id
    args = context.args
    if len(args) < 2:
        m = await update.message.reply_text(
            "❌ <b>Использование:</b>\n"
            "<code>/add_account &lt;auth_token&gt; &lt;ct0&gt;</code>",
            parse_mode="HTML"
        )
        track_sent(chat_id, m)
        return

    auth_token, ct0 = args[0], args[1]
    if await add_twitter_account(auth_token, ct0):
        m = await update.message.reply_text("✅ Аккаунт X успешно добавлен.")
    else:
        m = await update.message.reply_text("❌ Не удалось добавить. Проверьте логи.")
    track_sent(chat_id, m)


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return

    chat_id = update.effective_chat.id
    accounts = await api.pool.accounts_info()
    if not accounts:
        m = await update.message.reply_text("📭 Нет добавленных аккаунтов X.")
        track_sent(chat_id, m)
        return

    lines = ["📊 <b>Статус аккаунтов</b>", "━━━━━━━━━━━━━━━━━━━━━"]
    for acc in accounts:
        username = acc.get("username", "unknown")
        is_active = acc.get("active", False)
        icon = "✅" if is_active else "❌"
        lines.append(f"{icon} <code>{esc(username)}</code>")

    m = await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    track_sent(chat_id, m)


async def add_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return

    chat_id_self = update.effective_chat.id
    chat_id = None

    if update.message.reply_to_message:
        chat_id, error = extract_chat_id_from_forward(update.message.reply_to_message)
        if chat_id is None:
            m = await update.message.reply_text(
                f"❌ {error}\n\n"
                "━━━━━━━━━━━━━━━━━━━━━\n"
                "<b>Как правильно добавить получателя:</b>\n"
                "1. Попросите контакта открыть бота и нажать <b>Start</b> "
                "(или отправить <code>/my_id</code>).\n"
                "2. Получатель узнает свой <code>chat_id</code> и передаст его вам.\n"
                "3. Добавьте его вручную:\n"
                "   <code>/add_target &lt;chat_id&gt;</code>",
                parse_mode="HTML"
            )
            track_sent(chat_id_self, m)
            return

    elif context.args:
        raw = context.args[0].strip()
        if raw.lstrip("-").isdigit():
            chat_id = int(raw)
        else:
            m = await update.message.reply_text(
                "❌ Не похоже на chat_id. Укажите число.\n"
                "Например: <code>/add_target 123456789</code>",
                parse_mode="HTML"
            )
            track_sent(chat_id_self, m)
            return

    else:
        m = await update.message.reply_text(
            "<b>Как добавить получателя:</b>\n\n"
            "1. Попросите получателя нажать <b>Start</b> в боте.\n"
            "2. Он получит свой <code>chat_id</code> и передаст вам.\n"
            "3. Вы вводите: <code>/add_target &lt;chat_id&gt;</code>",
            parse_mode="HTML"
        )
        track_sent(chat_id_self, m)
        return

    if chat_id == ALLOWED_USER_ID:
        m = await update.message.reply_text("⚠️ Это ваш собственный ID.")
        track_sent(chat_id_self, m)
        return

    if chat_id in EXTRA_TARGETS:
        m = await update.message.reply_text(f"ℹ️ <code>{chat_id}</code> уже в списке.", parse_mode="HTML")
        track_sent(chat_id_self, m)
        return

    EXTRA_TARGETS.append(chat_id)
    logger.info(f"Добавлен получатель: {chat_id}")

    test_ok = True
    try:
        tm = await context.bot.send_message(
            chat_id=chat_id,
            text="🔔 Вас добавили в список получателей постов X Scroller.",
        )
        track_sent(chat_id, tm)
    except Exception as e:
        test_ok = False
        logger.warning(f"Не удалось отправить тестовое сообщение {chat_id}: {e}")

    if test_ok:
        m = await update.message.reply_text(
            f"✅ Получатель добавлен: <code>{chat_id}</code>\n"
            f"📨 Проверочное сообщение отправлено — значит, бот сможет ему писать.\n\n"
            f"Всего получателей: <b>{len(EXTRA_TARGETS)}</b>",
            parse_mode="HTML"
        )
    else:
        m = await update.message.reply_text(
            f"⚠️ Получатель <code>{chat_id}</code> добавлен в список, "
            f"но бот <b>не смог ему написать</b>.\n\n"
            f"<b>Что делать:</b> попросите получателя нажать <b>Start</b> в боте.",
            parse_mode="HTML"
        )
    track_sent(chat_id_self, m)


async def list_targets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return

    chat_id = update.effective_chat.id
    if not EXTRA_TARGETS:
        m = await update.message.reply_text(
            "📭 Дополнительных получателей нет.\n\nДобавьте через /add_target.",
            parse_mode="HTML"
        )
        track_sent(chat_id, m)
        return

    lines = [
        "📨 <b>Дополнительные получатели</b>",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"👑 Владелец: <code>{ALLOWED_USER_ID}</code> (всегда)",
    ]
    for i, cid in enumerate(EXTRA_TARGETS, 1):
        lines.append(f"{i}. <code>{cid}</code>")
    lines.append("")
    lines.append("<i>Убрать: /remove_target &lt;chat_id&gt;</i>")

    m = await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    track_sent(chat_id, m)


async def remove_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return

    chat_id = update.effective_chat.id
    if not context.args or not context.args[0].lstrip("-").isdigit():
        m = await update.message.reply_text(
            "❌ Использование: <code>/remove_target &lt;chat_id&gt;</code>",
            parse_mode="HTML"
        )
        track_sent(chat_id, m)
        return

    target = int(context.args[0])
    if target not in EXTRA_TARGETS:
        m = await update.message.reply_text(
            f"🤷 <code>{target}</code> нет в списке.",
            parse_mode="HTML"
        )
        track_sent(chat_id, m)
        return

    EXTRA_TARGETS.remove(target)
    logger.info(f"Удалён получатель: {target}")

    m = await update.message.reply_text(
        f"🗑 Получатель удалён: <code>{target}</code>\n"
        f"Осталось получателей: <b>{len(EXTRA_TARGETS)}</b>",
        parse_mode="HTML"
    )
    track_sent(chat_id, m)


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return

    chat_id = update.effective_chat.id
    if _search_active:
        stop_event.set()
        m = await update.message.reply_text("⏹ Останавливаю поиск…")
    else:
        m = await update.message.reply_text("🤷 Сейчас нечего останавливать.")
    track_sent(chat_id, m)


async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удаляет все сообщения, отправленные ботом в этом чате, и чистит temp_images."""
    if update.effective_user.id != ALLOWED_USER_ID:
        return

    chat_id = update.effective_chat.id
    ids = SENT_MESSAGES.pop(chat_id, [])

    if not ids:
        # Ничего не трекали — возможно, после перезапуска
        m = await update.message.reply_text(
            "🤷 Нечего удалять.\n\n"
            "<i>Примечание: список трекается только с момента запуска бота. "
            "После перезапуска старые сообщения удалить не получится.</i>",
            parse_mode="HTML"
        )
        # Эту же плашку трекаем, чтобы следующий /clear её убрал
        track_sent(chat_id, m)
        return

    # Пробуем удалить команду /clear саму
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
        except Exception as e:
            failed += 1
            logger.debug(f"Не удалось удалить {mid}: {e}")
        # Telegram: ~30 удалений в секунду. 0.05s = 20/сек — безопасно.
        await asyncio.sleep(0.05)

    # Чистим временные файлы
    files_removed = 0
    for f in TEMP_DIR.glob("*"):
        try:
            if f.is_file():
                f.unlink()
                files_removed += 1
        except Exception:
            pass

    # Отправляем подтверждение (трекаем — уберётся следующим /clear)
    m = await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "🗑 <b>Очистка завершена</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"📨 Удалено сообщений: <b>{deleted}</b>\n"
            f"⚠️ Не удалось удалить: <b>{failed}</b>\n"
            f"📁 Удалено файлов: <b>{files_removed}</b>\n\n"
            "<i>Сообщения старше 48 часов Telegram не даёт удалять боту — "
            "они могли попасть в «не удалось».</i>"
        ),
        parse_mode="HTML"
    )
    track_sent(chat_id, m)


async def search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return

    if _search_active:
        chat_id = update.effective_chat.id
        m = await update.message.reply_text("⚠️ Поиск уже идёт. Дождитесь или /stop.")
        track_sent(chat_id, m)
        return

    if not context.args:
        chat_id = update.effective_chat.id
        m = await update.message.reply_text(
            "❌ Укажите теги.\n"
            "<b>Пример:</b> <code>/search arknights,nsfw -guro n-5 lang:en</code>",
            parse_mode="HTML"
        )
        track_sent(chat_id, m)
        return

    groups, exclude, limit, lang = parse_search_args(context.args)

    if not groups:
        chat_id = update.effective_chat.id
        m = await update.message.reply_text(
            "❌ Не нашёл теги. Проверьте формат:\n"
            "<code>/search &lt;теги&gt; [-искл] [n-N] [lang:xx]</code>",
            parse_mode="HTML"
        )
        track_sent(chat_id, m)
        return

    lang_display = lang if lang else "любой"
    exclude_display = ", ".join(exclude) if exclude else "—"
    tags_display = format_groups_for_display(groups)
    targets_info = (
        f"👑 + {len(EXTRA_TARGETS)} 👥" if EXTRA_TARGETS else "👑 только вы"
    )

    header = (
        "🔍 <b>Поиск запущен</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"📝 <b>Теги:</b> <code>{esc(tags_display)}</code>\n"
        f"🚫 <b>Исключить:</b> <code>{esc(exclude_display)}</code>\n"
        f"🌐 <b>Язык:</b> <code>{esc(lang_display)}</code>\n"
        f"📊 <b>Лимит:</b> <code>{limit}</code>\n"
        f"📨 <b>Получатели:</b> {targets_info}"
    )
    msg = await update.message.reply_text(header, parse_mode="HTML")
    track_sent(update.effective_chat.id, msg)

    asyncio.create_task(_do_search(msg, context, groups, exclude, limit, lang))


async def _do_search(msg, context, groups, exclude, limit, lang):
    global _search_active

    stop_event.clear()
    _search_active = True

    try:
        group_strs = []
        for g in groups:
            if len(g) == 1:
                group_strs.append(g[0])
            else:
                group_strs.append("(" + " ".join(g) + ")")

        include_part = " OR ".join(group_strs)
        query_parts = [f"({include_part})"]
        for word in exclude:
            query_parts.append(f"-{word}")
        query_parts.append("filter:images")
        if lang:
            query_parts.append(f"lang:{lang}")

        full_query = " ".join(query_parts)
        logger.info(f"Ищу: {full_query} (limit={limit})")

        fetch_limit = min(limit + len(exclude) * 5, 100)
        tweets = await gather(api.search(full_query, limit=fetch_limit))

        if exclude:
            before = len(tweets)
            tweets = [t for t in tweets if not tweet_has_excluded(t, exclude)]
            logger.info(f"Фильтр исключений: {before} → {len(tweets)}")

        if len(tweets) > limit:
            tweets = tweets[:limit]

        if not tweets:
            await msg.edit_text("😕 Ничего не найдено по этим тегам.")
            return

        if lang:
            filtered = []
            for t in tweets:
                t_lang = getattr(t, "lang", None)
                if t_lang is None or t_lang == lang:
                    filtered.append(t)
            tweets = filtered
            if not tweets:
                await msg.edit_text(
                    "😕 Ничего не найдено после фильтра по языку.",
                    parse_mode="HTML"
                )
                return

        total = len(tweets)
        await msg.edit_text(
            f"✅ Найдено <b>{total}</b> постов.\n"
            f"📥 Скачиваю картинки…\n\n"
            f"<i>Чтобы остановить — отправьте</i> /stop",
            parse_mode="HTML"
        )

        total_photos = 0
        processed = 0
        stopped = False

        for tweet in tweets:
            if stop_event.is_set():
                stopped = True
                break

            count = await download_tweet_media(tweet, context)
            total_photos += count
            processed += 1

            if processed % 5 == 0 and processed < total:
                try:
                    await msg.edit_text(
                        f"📥 <b>Прогресс:</b> {processed} / {total}\n"
                        f"🖼 Скачано картинок: <b>{total_photos}</b>\n\n"
                        f"<i>Чтобы остановить — /stop</i>",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

            await asyncio.sleep(1.5)

        if stopped:
            await msg.edit_text(
                "⏹ <b>Остановлено пользователем</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━\n"
                f"📥 Обработано постов: <b>{processed}</b> / {total}\n"
                f"🖼 Скачано картинок: <b>{total_photos}</b>",
                parse_mode="HTML"
            )
        else:
            await msg.edit_text(
                "✅ <b>Готово!</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━\n"
                f"📥 Обработано постов: <b>{total}</b>\n"
                f"🖼 Скачано картинок: <b>{total_photos}</b>",
                parse_mode="HTML"
            )

    except Exception as e:
        logger.error(f"Ошибка поиска: {e}")
        try:
            await msg.edit_text(
                f"❌ Ошибка: <code>{esc(str(e)[:200])}</code>",
                parse_mode="HTML"
            )
        except Exception:
            pass
    finally:
        _search_active = False
        stop_event.clear()


# --- Веб-сервер ---

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
    logger.info(f"Веб-сервер для Render запущен на порту {port}")


# --- Запуск ---

async def run_bot():
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("my_id", my_id))
    application.add_handler(CommandHandler("add_account", add_account))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("search", search))
    application.add_handler(CommandHandler("stop", stop))
    application.add_handler(CommandHandler("clear", clear))
    application.add_handler(CommandHandler("add_target", add_target))
    application.add_handler(CommandHandler("targets", list_targets))
    application.add_handler(CommandHandler("remove_target", remove_target))

    await application.initialize()
    await application.start()
    await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)

    await ensure_account()
    load_targets_from_env()

    logger.info("Telegram-бот запущен.")

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
            run_bot()
        )
    finally:
        await http_session.close()


if __name__ == "__main__":
    print("=== СТАРТ ===", flush=True)
    print("BOT_TOKEN задан:", bool(BOT_TOKEN), flush=True)
    print("ALLOWED_USER_ID:", ALLOWED_USER_ID, flush=True)
    print("PORT:", os.environ.get("PORT"), flush=True)

    if not BOT_TOKEN:
        logger.error("Укажите BOT_TOKEN в переменных окружения!")
    else:
        try:
            asyncio.run(main_async())
        except (KeyboardInterrupt, SystemExit):
            pass