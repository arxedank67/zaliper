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


def parse_search_args(args: list) -> tuple[list, int, str | None]:
    """Разбирает аргументы /search."""
    tags = []
    limit = 20
    lang = None

    for raw in args:
        arg = raw.strip().rstrip(",")
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

        tags.append(arg)

    return tags, limit, lang


# --- Обработчики команд Telegram ---

HELP_TEXT = (
    "🤖 <b>X Scroller Bot</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━\n\n"
    "📌 <b>Команды</b>\n\n"
    "🔎 <b>/search</b> <code>&lt;теги&gt;</code> <code>[n-N]</code> <code>[lang:xx]</code>\n"
    "   Поиск постов с картинками\n"
    "   <i>Примеры:</i>\n"
    "   • <code>/search Arknights</code>\n"
    "   • <code>/search Arknights n-10</code>\n"
    "   • <code>/search Arknights n-10 lang:en</code>\n"
    "   • <code>/search Zone-tan, Paimon n-5</code>\n\n"
    "⏹ <b>/stop</b> — прервать текущий поиск\n"
    "👤 <b>/add_account</b> <code>&lt;auth_token&gt; &lt;ct0&gt;</code> — добавить аккаунт X\n"
    "📊 <b>/status</b> — статус аккаунтов X\n"
    "❓ <b>/help</b> — это меню\n\n"
    "━━━━━━━━━━━━━━━━━━━━━\n"
    "💡 <i>Параметры поиска:</i>\n"
    "• <code>n-N</code> — сколько постов искать (1–100)\n"
    "• <code>lang:xx</code> — язык постов (en, ru, ja, ko…)\n"
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
            "<b>Пример:</b> <code>/search Arknights n-10 lang:en</code>",
            parse_mode="HTML"
        )
        return

    tags, limit, lang = parse_search_args(context.args)

    if not tags:
        await update.message.reply_text(
            "❌ Не нашёл теги. Проверьте формат:\n"
            "<code>/search &lt;теги&gt; [n-N] [lang:xx]</code>",
            parse_mode="HTML"
        )
        return

    lang_display = lang if lang else "любой"
    header = (
        "🔍 <b>Поиск запущен</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"📝 <b>Теги:</b> <code>{esc(', '.join(tags))}</code>\n"
        f"🌐 <b>Язык:</b> <code>{esc(lang_display)}</code>\n"
        f"📊 <b>Лимит:</b> <code>{limit}</code>"
    )
    msg = await update.message.reply_text(header, parse_mode="HTML")

    # Запускаем в фоне, чтобы /stop мог сработать
    asyncio.create_task(_do_search(msg, context, tags, limit, lang))


async def _do_search(msg, context, tags, limit, lang):
    """Фоновый поиск и скачивание."""
    global _search_active

    stop_event.clear()
    _search_active = True

    try:
        query = " OR ".join(tags)
        full_query = f"({query}) filter:images"
        if lang:
            full_query += f" lang:{lang}"

        logger.info(f"Ищу: {full_query} (limit={limit})")
        tweets = await gather(api.search(full_query, limit=limit))

        # ФИКС: twscrape может вернуть больше, чем просили — режем вручную
        if len(tweets) > limit:
            logger.info(f"twscrape вернул {len(tweets)}, обрезаю до {limit}")
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
        .concurrent_updates(True)  # ← ФИКС: разрешаем параллельную обработку команд
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

    # Проверим/восстановим аккаунт X
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
