import os
import asyncio
import logging
from pathlib import Path

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
    for i, photo in enumerate(photos[:10]):  # Лимит 10 фото
        url = getattr(photo, "url", None)
        if not url:
            # Резервный вариант — собрать URL из id
            pid = getattr(photo, "id", None)
            if pid:
                url = f"https://pbs.twimg.com/media/{pid}.jpg"
        if not url:
            logger.warning(f"[DEBUG] Твит {tweet.id}, фото {i}: нет URL и id")
            continue

        logger.info(f"[DEBUG] Пытаюсь скачать: {url}")
        file_path = TEMP_DIR / f"{tweet.id}_{i}.jpg"

        try:
            async with context.bot.session.get(url, headers=HEADERS) as resp:
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

    # Отправляем в Telegram
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
        text += f"• `{acc.username}` — {'✅ активен' if acc.active else '❌ неактивен'}\n"

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


# --- Точка входа ---

def main():
    """Запускает бота."""
    if not BOT_TOKEN:
        logger.error("Укажите BOT_TOKEN в переменных окружения!")
        return

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("add_account", add_account))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("search", search))

    port = int(os.environ.get("PORT", 8080))
    logger.info(f"Бот запущен. Порт: {port}")

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    print("=== СТАРТ ===", flush=True)
    print("BOT_TOKEN задан:", bool(BOT_TOKEN), flush=True)
    print("ALLOWED_USER_ID:", ALLOWED_USER_ID, flush=True)
    print("PORT:", os.environ.get("PORT"), flush=True)
    main()