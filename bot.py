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

# --- Инициализация API twscrape ---
api = API("accounts.db")

# Флаги для управления поиском
stop_event = asyncio.Event()
_search_active = False

# Общая aiohttp-сессия
http_session: aiohttp.ClientSession | None = None

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36"
}


def esc(s: str) -> str:
    return html.escape(s or "")


# --- Вспомогательные функции ---

async def add_twitter_account(auth_token: str, ct0: str):
    """Добавляет аккаунт X в пул twscrape."""
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
    """Проверяет, есть ли аккаунт X. Если нет — восстанавливает из env."""
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
        logger.info("Аккаунт не найден, восстанавливаю из переменных окружения…")
        await add_twitter_account(auth, ct0)
    else:
        logger.warning(
            "Аккаунт X не найден. Добавьте через /add_account "
            "или задайте X_AUTH_TOKEN + X_CT0 в переменных окружения Render."
        )


async def download_tweet_media(tweet: Tweet, context: ContextTypes.DEFAULT_TYPE):
    """Скачивает все картинки из твита и отправляет их в Telegram."""
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
                    logger.error(f"[DEBUG] HTTP {resp.status} при скачивании {url}")
        except Exception as e:
            logger.error(f"[DEBUG] Исключение при скачивании {url}: {e}")

    if not photos_to_send:
        return 0

    text_preview = esc(tweet.rawContent[:180]) if tweet.rawContent else ""
    caption = (
        f'🔗 <a href="https://x.com/i/status/{tweet.id}">Ссылка на пост</a>\n'
        f'📝 <i>{text_preview}</i>'
    )

    try:
        if len(photos_to_send) == 1:
            with open(photos_to_send[0], "rb") as f:
                await context.bot.send_photo(
                    chat_id=ALLOWED_USER_ID,
                    photo=f,
                    caption=caption,
                    parse_mode="HTML"
                )
        else:
            media_group = []
            for path in photos_to_send:
                with open(path, "rb") as f:
                    media_group.append(InputMediaPhoto(media=f))
            await context.bot.send_media_group(
                chat_id=ALLOWED_USER_ID, media=media_group
            )
            await context.bot.send_message(
                chat_id=ALLOWED_USER_ID,
                text=caption,
                parse_mode="HTML"
            )
    except Exception as e:
        logger.error(f"Ошибка отправки в Telegram: {e}")
    finally:
        for path in photos_to_send:
            path.unlink(missing_ok=True)

    return len(photos_to_send)


def parse_search_args(args: list) -> tuple[list, list, int, str | None]:
    """Разбирает аргументы /search.

    Логика групп:
      - Теги через ЗАПЯТУЮ (apple,sun) — AND: все слова должны быть в посте.
      - Разные аргументы через ПРОБЕЛ (apple sun) — OR: достаточно одного.
      - Слова с минусом (-nsfw) — исключение.

    Примеры:
      /search apple,sun
        → groups=[["apple", "sun"]] → запрос: (apple sun)

      /search apple,sun banana
        → groups=[["apple", "sun"], ["banana"]]
        → запрос: ((apple sun) OR banana)

      /search apple,sun banana,moon n-5 lang:en
        → groups=[["apple", "sun"], ["banana", "moon"]]
        → запрос: ((apple sun) OR (banana moon)) filter:images lang:en
    """
    include_groups = []  # список групп; OR между группами, AND внутри
    exclude = []
    limit = 20
    lang = None

    for raw in args:
        arg = raw.strip()
        if not arg:
            continue

        # n-N
        if arg.startswith("n-") and arg[2:].isdigit():
            limit = max(1, min(100, int(arg[2:])))
            continue

        # lang:xx или lang-xx
        if arg.startswith("lang:") or arg.startswith("lang-"):
            code = arg.split(":", 1)[1] if ":" in arg else arg.split("-", 1)[1]
            code = code.strip().lower()
            if 2 <= len(code) <= 5:
                lang = code
            continue

        # Исключения: -тег
        if arg.startswith("-") and not arg.startswith("--") and len(arg) > 1:
            word = arg[1:].strip().lstrip("#")
            if word:
                exclude.append(word)
            continue

        # Обычные теги. Разбиваем по запятым → группа AND
        parts = [p.strip().lstrip("#") for p in arg.split(",")]
        group = [p for p in parts if p]
        if group:
            include_groups.append(group)

    return include_groups, exclude, limit, lang


def tweet_has_excluded(tweet: Tweet, exclude: list) -> bool:
    """Клиентская страховка: проверяет исключаемые слова."""
    if not exclude:
        return False
    text = (tweet.rawContent or "").lower()
    for word in exclude:
        w = word.lower().lstrip("#")
        if w and w in text:
            return True
    return False


def format_groups_for_display(groups: list) -> str:
    """Красиво показывает группы: apple + sun | banana."""
    parts = []
    for g in groups:
        if len(g) == 1:
            parts.append(g[0])
        else:
            parts.append(" + ".join(g))
    return "  |  ".join(parts)


# --- Обработчики команд Telegram ---

HELP_TEXT = (
    "🤖 <b>X Scroller Bot</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━\n\n"
    "📌 <b>Команды</b>\n\n"
    "🔎 <b>/search</b> <code>&lt;теги&gt;</code> <code>[-искл]</code> <code>[n-N]</code> <code>[lang:xx]</code>\n\n"
    "   <b>Логика тегов:</b>\n"
    "   • <code>apple sun</code> → apple <b>ИЛИ</b> sun\n"
    "   • <code>apple,sun</code> → apple <b>И</b> sun (в одном посте)\n"
    "   • <code>-nsfw</code> → исключить посты со словом nsfw\n\n"
    "   <i>Примеры:</i>\n"
    "   • <code>/search arknights n-10</code>\n"
    "   • <code>/search arknights,nsfw n-5</code>\n"
    "   • <code>/search arknights,nsfw -guro n-5 lang:en</code>\n"
    "   • <code>/search apple,sun banana,moon n-10</code>\n\n"
    "⏹ <b>/stop</b> — прервать текущий поиск\n"
    "👤 <b>/add_account</b> <code>&lt;auth_token&gt; &lt;ct0&gt;</code> — добавить аккаунт X\n"
    "📊 <b>/status</b> — статус аккаунтов X\n"
    "❓ <b>/help</b> — это меню\n\n"
    "━━━━━━━━━━━━━━━━━━━━━\n"
    "💡 <i>Параметры поиска:</i>\n"
    "• <code>n-N</code> — сколько постов искать (1–100)\n"
    "• <code>lang:xx</code> — язык постов (en, ru, ja, ko…)\n"
    "• <code>-тег</code> — исключить посты с этим словом\n"
    "• <code>a,b</code> — AND: оба слова в посте\n"
    "• <code>a b</code> — OR: любое из слов\n"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        await update.message.reply_text("⛔ Доступ запрещён.")
        return
    await update.message.reply_text(HELP_TEXT, parse_mode="HTML")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    await update.message.reply_text(HELP_TEXT, parse_mode="HTML")


async def add_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "❌ <b>Использование:</b>\n"
            "<code>/add_account &lt;auth_token&gt; &lt;ct0&gt;</code>\n\n"
            "<b>Как получить cookies:</b>\n"
            "1. Откройте x.com в браузере\n"
            "2. F12 → Application → Cookies → https://x.com\n"
            "3. Скопируйте значения <code>auth_token</code> и <code>ct0</code>",
            parse_mode="HTML"
        )
        return

    auth_token, ct0 = args[0], args[1]
    success = await add_twitter_account(auth_token, ct0)

    if success:
        await update.message.reply_text("✅ Аккаунт X успешно добавлен.")
    else:
        await update.message.reply_text("❌ Не удалось добавить аккаунт. Проверьте логи.")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return

    accounts = await api.pool.accounts_info()
    if not accounts:
        await update.message.reply_text("📭 Нет добавленных аккаунтов X.")
        return

    lines = ["📊 <b>Статус аккаунтов</b>", "━━━━━━━━━━━━━━━━━━━━━"]
    for acc in accounts:
        username = acc.get("username", "unknown")
        is_active = acc.get("active", False)
        icon = "✅" if is_active else "❌"
        lines.append(f"{icon} <code>{esc(username)}</code>")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Прерывает текущий поиск."""
    if update.effective_user.id != ALLOWED_USER_ID:
        return

    if _search_active:
        stop_event.set()
        await update.message.reply_text("⏹ Останавливаю поиск…")
    else:
        await update.message.reply_text("🤷 Сейчас нечего останавливать.")


async def search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик /search — запускает поиск в фоне."""
    if update.effective_user.id != ALLOWED_USER_ID:
        return

    if _search_active:
        await update.message.reply_text("⚠️ Поиск уже идёт. Дождитесь или /stop.")
        return

    if not context.args:
        await update.message.reply_text(
            "❌ Укажите теги.\n"
            "<b>Пример:</b> <code>/search arknights,nsfw -guro n-5 lang:en</code>",
            parse_mode="HTML"
        )
        return

    groups, exclude, limit, lang = parse_search_args(context.args)

    if not groups:
        await update.message.reply_text(
            "❌ Не нашёл теги. Проверьте формат:\n"
            "<code>/search &lt;теги&gt; [-искл] [n-N] [lang:xx]</code>",
            parse_mode="HTML"
        )
        return

    lang_display = lang if lang else "любой"
    exclude_display = ", ".join(exclude) if exclude else "—"
    tags_display = format_groups_for_display(groups)

    header = (
        "🔍 <b>Поиск запущен</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"📝 <b>Теги:</b> <code>{esc(tags_display)}</code>\n"
        f"🚫 <b>Исключить:</b> <code>{esc(exclude_display)}</code>\n"
        f"🌐 <b>Язык:</b> <code>{esc(lang_display)}</code>\n"
        f"📊 <b>Лимит:</b> <code>{limit}</code>"
    )
    msg = await update.message.reply_text(header, parse_mode="HTML")

    asyncio.create_task(_do_search(msg, context, groups, exclude, limit, lang))


async def _do_search(msg, context, groups, exclude, limit, lang):
    """Фоновый поиск и скачивание."""
    global _search_active

    stop_event.clear()
    _search_active = True

    try:
        # Формируем запрос:
        #   группы через OR, слова внутри группы — через пробел (AND)
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

        # Компенсируем возможную потерю после клиентских фильтров
        fetch_limit = min(limit + len(exclude) * 5, 100)

        tweets = await gather(api.search(full_query, limit=fetch_limit))

        # Клиентский фильтр исключений
        if exclude:
            before = len(tweets)
            tweets = [t for t in tweets if not tweet_has_excluded(t, exclude)]
            logger.info(f"Клиентский фильтр исключений: {before} → {len(tweets)}")

        # Обрезаем до запрошенного лимита
        if len(tweets) > limit:
            tweets = tweets[:limit]

        if not tweets:
            await msg.edit_text("😕 Ничего не найдено по этим тегам.")
            return

        # Клиентская фильтрация по языку
        if lang:
            filtered = []
            for t in tweets:
                t_lang = getattr(t, "lang", None)
                if t_lang is None or t_lang == lang:
                    filtered.append(t)
            tweets = filtered

            if not tweets:
                await msg.edit_text(
                    "😕 Ничего не найдено после фильтра по языку. "
                    "Попробуйте убрать <code>lang:xx</code>.",
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


# --- Веб-сервер для Render ---

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


# --- Запуск бота ---

async def run_bot():
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("add_account", add_account))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("search", search))
    application.add_handler(CommandHandler("stop", stop))

    await application.initialize()
    await application.start()
    await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)

    await ensure_account()

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