"""
Нейропостинг-бот.

Слушает список Telegram-каналов через userbot (Telethon). Каждый новый пост
(в том числе альбом из нескольких фото/видео) сразу переписывается через
Groq API и сохраняется как "текущий кандидат на публикацию". Раз в
PUBLISH_INTERVAL_SECONDS (по умолчанию 30 минут) бот публикует именно этого,
самого свежего кандидата — если за интервал пришло несколько постов, более
старые из них просто отбрасываются и в канал не попадают.
"""
import asyncio
import logging
import os

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telegram import Bot, InputMediaPhoto, InputMediaVideo
from telegram.constants import ParseMode

from db import init_db, is_duplicate, mark_processed
from rewriter import rewrite_post

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("neuropost")

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
TARGET_CHANNEL = os.environ["TARGET_CHANNEL"]
SOURCE_CHANNELS = [c.strip() for c in os.environ["SOURCE_CHANNELS"].split(",")]

# Интервал между публикациями в секундах. 1800 = 30 минут.
PUBLISH_INTERVAL_SECONDS = int(os.environ.get("PUBLISH_INTERVAL_SECONDS", "30"))

# Подпись, добавляемая в конец каждого опубликованного поста
CHANNEL_SIGNATURE = (
    '\n\n<a href="https://t.me/+UE0bYyh4qUxhZGEx">Инфач! Подписаться!</a>'
)

# userbot-клиент для чтения источников (сессия сохранится в файл userbot.session)
userbot = TelegramClient("jr3ewdqwdqwdqwdqwgije", API_ID, API_HASH)

# обычный бот для публикации в свой канал
publish_bot = Bot(token=BOT_TOKEN)

# Слот с самым свежим готовым к публикации постом.
# Каждый новый пост перезаписывает предыдущего "кандидата" — старые версии
# просто теряются и в канал не публикуются.
#
# Структура:
# {
#   "text": str,
#   "preview": str,
#   "media_type": None | "photo" | "video" | "animation" | "audio" | "voice"
#                 | "document" | "album",
#   "media": bytes | None,                     # для одиночного медиа
#   "album_items": list[(str, bytes)] | None,   # для альбома: [(тип, байты), ...]
# }
_pending_lock = asyncio.Lock()
_pending_post: dict | None = None


def _detect_media_type(message) -> str | None:
    if message.photo:
        return "photo"
    if message.video:
        return "video"
    if message.gif:
        return "animation"
    if message.audio:
        return "audio"
    if message.voice:
        return "voice"
    if message.document:
        return "document"
    return None


async def _store_pending(text: str, preview: str, media_type, media=None, album_items=None):
    global _pending_post
    async with _pending_lock:
        replaced = _pending_post is not None
        _pending_post = {
            "text": text,
            "preview": preview,
            "media_type": media_type,
            "media": media,
            "album_items": album_items,
        }
    if replaced:
        log.info("Новый кандидат заменил предыдущий (тот отброшен): %s", preview)
    else:
        log.info("Новый кандидат на публикацию: %s", preview)


async def process_single_message(event):
    """Обрабатывает обычный пост (не альбом): текст + максимум один медиафайл."""
    message = event.message

    # Сообщения, входящие в альбом, обрабатываются отдельным хендлером
    # events.Album — здесь их пропускаем, чтобы не задвоить публикацию.
    if message.grouped_id is not None:
        return

    original_text = message.text or message.raw_text

    if not original_text or len(original_text.strip()) < 20:
        # пропускаем пустые посты / посты без текста (только стикеры и т.п.)
        return

    if is_duplicate(original_text):
        log.info("Дубликат, пропускаю: %s", original_text[:60])
        return

    try:
        rewritten = await rewrite_post(original_text)
    except Exception:
        log.exception("Ошибка при переписывании текста")
        return

    final_text = rewritten + CHANNEL_SIGNATURE

    media_type = _detect_media_type(message)
    media_bytes = None
    if media_type:
        media_bytes = await userbot.download_media(message, file=bytes)

    mark_processed(message.chat_id, message.id, original_text)
    await _store_pending(final_text, rewritten[:60], media_type, media=media_bytes)


async def process_album(event):
    """Обрабатывает альбом (несколько фото/видео в одном посте)."""
    messages = event.messages

    # Подпись обычно прикреплена только к одному сообщению из альбома
    original_text = None
    for m in messages:
        if m.text or m.raw_text:
            original_text = m.text or m.raw_text
            break

    if not original_text or len(original_text.strip()) < 20:
        return

    if is_duplicate(original_text):
        log.info("Дубликат (альбом), пропускаю: %s", original_text[:60])
        return

    try:
        rewritten = await rewrite_post(original_text)
    except Exception:
        log.exception("Ошибка при переписывании текста альбома")
        return

    final_text = rewritten + CHANNEL_SIGNATURE

    album_items = []
    for m in messages:
        item_type = _detect_media_type(m)
        if not item_type:
            continue
        item_bytes = await userbot.download_media(m, file=bytes)
        album_items.append((item_type, item_bytes))

    if not album_items:
        return

    first_message = messages[0]
    mark_processed(first_message.chat_id, first_message.id, original_text)
    await _store_pending(
        final_text, rewritten[:60], "album", album_items=album_items
    )


async def publisher_loop():
    """Раз в PUBLISH_INTERVAL_SECONDS публикует самый свежий накопленный пост."""
    global _pending_post
    while True:
        await asyncio.sleep(PUBLISH_INTERVAL_SECONDS)

        async with _pending_lock:
            post = _pending_post
            _pending_post = None

        if post is None:
            log.info("Нет свежих постов для публикации в этом интервале.")
            continue

        try:
            await _publish(post)
            log.info("Опубликовано (%s): %s", post["media_type"] or "текст", post["preview"])
        except Exception:
            log.exception("Ошибка при публикации в канал")


async def _publish(post: dict):
    media_type = post["media_type"]
    text = post["text"]

    if media_type == "album":
        await _publish_album(post["album_items"], text)
        return

    media = post["media"]

    if media_type == "photo":
        await publish_bot.send_photo(
            chat_id=TARGET_CHANNEL, photo=media,
            caption=text, parse_mode=ParseMode.HTML,
        )
    elif media_type == "video":
        await publish_bot.send_video(
            chat_id=TARGET_CHANNEL, video=media,
            caption=text, parse_mode=ParseMode.HTML,
        )
    elif media_type == "animation":
        await publish_bot.send_animation(
            chat_id=TARGET_CHANNEL, animation=media,
            caption=text, parse_mode=ParseMode.HTML,
        )
    elif media_type == "audio":
        await publish_bot.send_audio(
            chat_id=TARGET_CHANNEL, audio=media,
            caption=text, parse_mode=ParseMode.HTML,
        )
    elif media_type == "voice":
        await publish_bot.send_voice(
            chat_id=TARGET_CHANNEL, voice=media,
            caption=text, parse_mode=ParseMode.HTML,
        )
    elif media_type == "document":
        await publish_bot.send_document(
            chat_id=TARGET_CHANNEL, document=media,
            caption=text, parse_mode=ParseMode.HTML,
        )
    else:
        await publish_bot.send_message(
            chat_id=TARGET_CHANNEL,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=False,
        )


async def _publish_album(album_items: list, text: str):
    """
    Публикует альбом. Bot API поддерживает send_media_group только для
    комбинаций фото/видео. Если в альбоме есть другие типы (документы,
    аудио) — публикуем их отдельными сообщениями следом за альбомом.
    """
    photo_video_items = [(t, b) for t, b in album_items if t in ("photo", "video")]
    other_items = [(t, b) for t, b in album_items if t not in ("photo", "video")]

    if photo_video_items:
        media_group = []
        for i, (item_type, item_bytes) in enumerate(photo_video_items):
            kwargs = {}
            if i == 0:
                # подпись и текст поста прикрепляются только к первому элементу
                kwargs = {"caption": text, "parse_mode": ParseMode.HTML}
            if item_type == "photo":
                media_group.append(InputMediaPhoto(media=item_bytes, **kwargs))
            else:
                media_group.append(InputMediaVideo(media=item_bytes, **kwargs))
        await publish_bot.send_media_group(chat_id=TARGET_CHANNEL, media=media_group)
        # текст уже ушёл подписью к первому элементу альбома
        text_already_sent = True
    else:
        text_already_sent = False

    for item_type, item_bytes in other_items:
        caption = None if text_already_sent else text
        text_already_sent = True
        if item_type == "audio":
            await publish_bot.send_audio(
                chat_id=TARGET_CHANNEL, audio=item_bytes,
                caption=caption, parse_mode=ParseMode.HTML if caption else None,
            )
        elif item_type == "voice":
            await publish_bot.send_voice(
                chat_id=TARGET_CHANNEL, voice=item_bytes,
                caption=caption, parse_mode=ParseMode.HTML if caption else None,
            )
        elif item_type == "document":
            await publish_bot.send_document(
                chat_id=TARGET_CHANNEL, document=item_bytes,
                caption=caption, parse_mode=ParseMode.HTML if caption else None,
            )

    if not text_already_sent:
        await publish_bot.send_message(
            chat_id=TARGET_CHANNEL, text=text, parse_mode=ParseMode.HTML,
        )


@userbot.on(events.Album(chats=SOURCE_CHANNELS))
async def album_handler(event):
    await process_album(event)


@userbot.on(events.NewMessage(chats=SOURCE_CHANNELS))
async def message_handler(event):
    await process_single_message(event)


async def main():
    init_db()
    await userbot.start()  # при первом запуске попросит номер телефона и код из Telegram
    log.info(
        "Бот запущен. Слушаю источники: %s. Публикация раз в %d сек (только самый свежий пост).",
        SOURCE_CHANNELS,
        PUBLISH_INTERVAL_SECONDS,
    )
    asyncio.create_task(publisher_loop())
    await userbot.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())