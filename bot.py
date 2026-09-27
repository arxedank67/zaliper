import os
import asyncio
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

# Общая aiohttp-сессия для скачивания картинок
http_session: aiohttp.ClientSession | None = None

# User-Agent, чтобы Twitter не блокировал скачивание картинок с Render
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36"
}


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


async def download_tweet_media(tweet: Tweet, context: ContextTypes.DEFAULT_TYPE):
    """Скачивает все картинки из твита и отправляет их в Telegram."""
    media_list = tweet.media

    if not media_list:
        logger.info(f"[DEBUG] Твит {tweet.id}: media_list пустой")
        return 0

    photos = getattr(media_list, "photos", None)
    if not photos:
        logger.info(f"[DEBUG] Твит {tweet.id}: photos пустой или отсутствует")
        return 0

    logger.info(f"[DEBUG] Твит {tweet.id}: найдено {len(photos)} фото")

    photos_to_send = []
    for i, photo in enumerate(photos[:10]):
        url = getattr(photo, "url", None)
        if not url:
            pid = getattr(photo, "id", None)
            if pid:
                url = f"https://pbs.twimg.com/media/{pid}.jpg"
        if not url:
            logger.warning(f"[DEBUG] Твит {tweet.id}, фото {i}: нет URL и id")
            continue

        logger.info(f"[DEBUG] Пытаюсь скачать: {url}")
        file_path = TEMP_DIR / f"{tweet.id}_{i}.jpg"

        try:
            # Используем собственную aiohttp-сессию, а не bot.session
            async with http_session.get(url, headers=HEADERS) as resp:
                if resp.status == 200:
                    with open(file_path, "wb") as f:
                        f.write(await resp.read())
                    photos_to_send.append(file_path)
                    logger.info(f"[DEBUG] Успешно скачано: {file_path}")
                else:
                    logger.error(f"[DEBUG] HTTP {resp.status} при скачивании {url}")
        except Exception as e:
            logger.error(f"[DEBUG] Исключение при скачивании {url}: {e}")

    if not photos_to_send:
        logger.warning(f"[DEBUG] Твит {tweet.id}: ни одна картинка не скачалась")
        return 0

    try:
        caption = (
            f"🔗 [Ссылка на пост](https://x.com/i/status/{tweet.id})\n"
            f"📝 {tweet.rawContent[:200]}"
        )

        if len(photos_to_send) == 1:
            with open(photos_to_send[0], "rb") as f:
                await context.bot.send_photo(
                    chat_id=ALLOWED_USER_ID,
                    photo=f,
                    caption=caption,
                    parse_mode="Markdown"
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
                parse_mode="Markdown"
            )
    except Exception as e:
        logger.error(f"Ошибка отправки в Telegram: {e}")
    finally:
        for path in photos_to_send:
            path.unlink(missing_ok=True)

    return len(photos_to_send)


# --- Обработчики команд Telegram ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start."""
    if update.effective_user.id != ALLOWED_USER_ID:
        await update.message.reply_text("⛔ Доступ запрещён.")
        return

    await update.message.reply_text(
        "👋 Привет! Я бот для поиска постов в X.\n\n"
        "**Команды:**\n"
        "`/search <теги>` — Найти посты по тегам\n"
        "`/add_account <auth_token> <ct0>` — Добавить аккаунт X\n"
        "`/status` — Проверить статус аккаунтов X\n"
        "`/help` — Показать это сообщение",
        parse_mode="Markdown"
    )


async def add_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Добавление аккаунта X по cookies."""
    if update.effective_user.id != ALLOWED_USER_ID:
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "❌ Использование: `/add_account <auth_token> <ct0>`\n\n"
            "Как получить cookies:\n"
            "1. Откройте x.com в браузере\n"
            "2. F12 → Application → Cookies → https://x.com\n"
            "3. Скопируйте значения `auth_token` и `ct0`",
            parse_mode="Markdown"
        )
        return

    auth_token, ct0 = args[0], args[1]
    success = await add_twitter_account(auth_token, ct0)

    if success:
        await update.message.reply_text("✅ Аккаунт X успешно добавлен!")
    else:
        await update.message.reply_text("❌ Не удалось добавить аккаунт. Проверьте логи.")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Проверка статуса аккаунтов."""
    if update.effective_user.id != ALLOWED_USER_ID:
        return

    accounts = await api.pool.accounts_info()
    if not accounts:
        await update.message.reply_text("📭 Нет добавленных аккаунтов X.")
        return

    text = "📊 **Статус аккаунтов:**\n"
    for acc in accounts:
        # accounts_info возвращает список словарей, а не объектов
        username = acc.get("username", "unknown")
        is_active = acc.get("active", False)
        text += f"• `{username}` — {'✅ активен' if is_active else '❌ неактивен'}\n"

    await update.message.reply_text(text, parse_mode="Markdown")


async def search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Поиск постов по тегам."""
    if update.effective_user.id != ALLOWED_USER_ID:
        return

    if not context.args:
        await update.message.reply_text(
            "❌ Укажите теги через запятую.\n"
            "Пример: `/search Zone-tan, #GenshinImpact`",
            parse_mode="Markdown"
        )
        return

    query = " OR ".join(context.args)
    full_query = f"({query}) filter:images"

    msg = await update.message.reply_text(
        f"🔍 Ищу посты: `{query}`...", parse_mode="Markdown"
    )

    try:
        tweets = await gather(api.search(full_query, limit=20))

        if not tweets:
            await msg.edit_text("😕 Ничего не найдено по этим тегам.")
            return

        await msg.edit_text(
            f"✅ Найдено {len(tweets)} постов. Начинаю скачивать картинки..."
        )

        total_photos = 0
        for tweet in tweets:
            count = await download_tweet_media(tweet, context)
            total_photos += count
            await asyncio.sleep(1.5)

        await msg.edit_text(
            f"✅ Готово! Скачано {total_photos} картинок из {len(tweets)} постов."
        )

    except Exception as e:
        logger.error(f"Ошибка поиска: {e}")
        await msg.edit_text(f"❌ Ошибка: {str(e)[:200]}")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает справку."""
    await start(update, context)


# --- Веб-сервер для Render ---

async def health_check(request):
    """Отвечает 'OK' на пинги Render и UptimeRobot."""
    return web.Response(text="Bot is alive!")


async def start_web_server():
    """Запускает мини-сервер на порту, который требует Render."""
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
    """Запускает Telegram-бота в режиме polling в текущем event loop."""
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("add_account", add_account))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("search", search))

    await application.initialize()
    await application.start()
    await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)

    logger.info("Telegram-бот запущен.")

    try:
        await asyncio.Event().wait()
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()


async def main_async():
    """Запускает и веб-сервер, и бота одновременно."""
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
