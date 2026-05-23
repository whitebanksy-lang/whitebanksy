"""
Telegram Forwarder Bot — полная версия
======================================
Функции:
  • Пересылка сообщений с ключевыми словами (поиск целых слов, кириллица + латиница)
  • Стоп-слова с приоритетом над ключевыми
  • Периодическое сканирование каждые N минут (страховка от пропусков)
  • Персистентность forwarded_ids через SQLite (дубли не пересылаются после перезапуска)
  • Автоматическая очистка старых записей в БД
  • Антиспам: не более MAX_FORWARDS_PER_CHAT сообщений из одного чата за SPAM_WINDOW секунд
  • Фильтр по типу чата: личка / группы / каналы
  • Контекст: пересылка N сообщений до/после совпадения
  • Команды управления прямо из Telegram:
      /addkeyword <слово>    — добавить ключевое слово
      /delkeyword <слово>    — удалить ключевое слово
      /addstop <слово>       — добавить стоп-слово
      /delstop <слово>       — удалить стоп-слово
      /keywords              — показать текущие ключевые слова
      /stopwords             — показать текущие стоп-слова
      /stats                 — статистика перехватов
      /status                — статус бота
  • Ссылка на оригинальное сообщение в пересылке
  • Уведомление владельца при старте и падении
"""

import asyncio
import logging
import os
import re
import sqlite3
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telethon import TelegramClient, events
from telethon.errors import ChatForwardsRestrictedError, FloodWaitError
from telethon.sessions import StringSession
from telethon.tl.types import (
    User, Chat, Channel,
    InputPeerUser, InputPeerChat, InputPeerChannel
)

# Локально читаем переменные из файла .env (он в .gitignore, в репо не попадает)
# На Scalingo .env нет — переменные берутся из Dashboard -> Environment
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ╔══════════════════════════════════════════════════════╗
# ║                    КОНФИГУРАЦИЯ                      ║
# ║  Все секреты читаются из переменных окружения.       ║
# ║  Задайте их в панели Scalingo → Environment.         ║
# ╚══════════════════════════════════════════════════════╝

def _require_env(name: str) -> str:
    val = os.environ.get(name, '').strip()
    if not val:
        print(f"❌ Переменная окружения {name} не задана. Остановка.", flush=True)
        sys.exit(1)
    return val

API_ID              = int(_require_env('API_ID'))
API_HASH            = _require_env('API_HASH')
SESSION_STRING      = _require_env('SESSION_STRING')   # строка сессии (см. README)
DESTINATION_CHAT_ID = int(_require_env('DESTINATION_CHAT_ID'))

# Интервал периодического сканирования (секунды)
SCAN_INTERVAL = 5 * 60

# Начальные ключевые слова (можно менять командами /addkeyword)
KEYWORDS_DEFAULT = [
    'бот',
    'чат-бот',
    'техспец',
    'технический специалист',
    'Getcourse',
]

# Начальные стоп-слова
STOP_WORDS_DEFAULT = [
    'помогу',
    'я работаю',
    'флуд',
    'test',
]

# Список чатов-источников (пусто = все чаты)
SOURCE_CHATS = []

# Фильтр по типу чата: True = слушаем, False = игнорируем
LISTEN_PRIVATE   = True   # личные переписки
LISTEN_GROUPS    = True   # группы и супергруппы
LISTEN_CHANNELS  = True   # каналы

# Контекст: сколько сообщений до/после пересылать вместе с совпадением
CONTEXT_BEFORE = 0   # 0 = только само сообщение
CONTEXT_AFTER  = 0

# Антиспам: максимум пересылок из одного чата за окно времени
MAX_FORWARDS_PER_CHAT = 10
SPAM_WINDOW_SECONDS   = 60

# Хранить историю пересланных ID не дольше (дней)
DB_RETENTION_DAYS = 30

# Путь к файлу БД и конфига
DB_PATH     = Path('forwarder.db')
CONFIG_PATH = Path('forwarder_config.json')

# Подробное логирование
DEBUG_MODE = False

# ╔══════════════════════════════════════════════════════╗
# ║                    ЛОГИРОВАНИЕ                       ║
# ╚══════════════════════════════════════════════════════╝

logging.basicConfig(
    format='%(asctime)s [%(levelname)s] %(message)s',
    level=logging.DEBUG if DEBUG_MODE else logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('forwarder.log', encoding='utf-8'),
    ]
)
logger = logging.getLogger(__name__)

# ╔══════════════════════════════════════════════════════╗
# ║                  БАЗА ДАННЫХ (SQLite)                ║
# ╚══════════════════════════════════════════════════════╝

def db_init():
    """Создаёт таблицы при первом запуске."""
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS forwarded (
            msg_id   INTEGER NOT NULL,
            chat_id  INTEGER NOT NULL,
            ts       INTEGER NOT NULL,
            PRIMARY KEY (msg_id, chat_id)
        );
        CREATE TABLE IF NOT EXISTS stats (
            chat_id   INTEGER NOT NULL,
            keyword   TEXT    NOT NULL,
            ts        INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_stats_ts ON stats(ts);
    """)
    con.commit()
    con.close()

def db_was_forwarded(msg_id: int, chat_id: int) -> bool:
    con = sqlite3.connect(DB_PATH)
    row = con.execute(
        "SELECT 1 FROM forwarded WHERE msg_id=? AND chat_id=?", (msg_id, chat_id)
    ).fetchone()
    con.close()
    return row is not None

def db_mark_forwarded(msg_id: int, chat_id: int):
    ts = int(datetime.now(timezone.utc).timestamp())
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT OR IGNORE INTO forwarded(msg_id, chat_id, ts) VALUES(?,?,?)",
        (msg_id, chat_id, ts)
    )
    con.commit()
    con.close()

def db_cleanup_old():
    """Удаляет записи старше DB_RETENTION_DAYS."""
    cutoff = int((datetime.now(timezone.utc) - timedelta(days=DB_RETENTION_DAYS)).timestamp())
    con = sqlite3.connect(DB_PATH)
    con.execute("DELETE FROM forwarded WHERE ts < ?", (cutoff,))
    con.execute("DELETE FROM stats WHERE ts < ?", (cutoff,))
    con.commit()
    con.close()
    logger.debug("🧹 Старые записи БД очищены.")

def db_add_stat(chat_id: int, keyword: str):
    ts = int(datetime.now(timezone.utc).timestamp())
    con = sqlite3.connect(DB_PATH)
    con.execute("INSERT INTO stats(chat_id, keyword, ts) VALUES(?,?,?)", (chat_id, keyword, ts))
    con.commit()
    con.close()

def db_get_stats() -> dict:
    con = sqlite3.connect(DB_PATH)
    total = con.execute("SELECT COUNT(*) FROM stats").fetchone()[0]
    today_ts = int(datetime.now(timezone.utc).replace(hour=0, minute=0, second=0).timestamp())
    today = con.execute("SELECT COUNT(*) FROM stats WHERE ts >= ?", (today_ts,)).fetchone()[0]
    top_kw = con.execute(
        "SELECT keyword, COUNT(*) as cnt FROM stats GROUP BY keyword ORDER BY cnt DESC LIMIT 5"
    ).fetchall()
    top_chats = con.execute(
        "SELECT chat_id, COUNT(*) as cnt FROM stats GROUP BY chat_id ORDER BY cnt DESC LIMIT 5"
    ).fetchall()
    con.close()
    return {"total": total, "today": today, "top_keywords": top_kw, "top_chats": top_chats}

# ╔══════════════════════════════════════════════════════╗
# ║             КОНФИГ (ключевые/стоп-слова)             ║
# ╚══════════════════════════════════════════════════════╝

def config_load() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, encoding='utf-8') as f:
            return json.load(f)
    return {
        "keywords":  [w.lower() for w in KEYWORDS_DEFAULT],
        "stopwords": [w.lower() for w in STOP_WORDS_DEFAULT],
    }

def config_save(cfg: dict):
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

# Глобальный конфиг и скомпилированные паттерны
_config: dict = {}
_keyword_patterns:  list[tuple[str, re.Pattern]] = []
_stopword_patterns: list[tuple[str, re.Pattern]] = []

def _make_word_pattern(phrase: str) -> re.Pattern:
    """Граница слова для кириллицы и латиницы."""
    escaped = re.escape(phrase.lower())
    return re.compile(
        r'(?<![a-zA-Zа-яёА-ЯЁ0-9])' + escaped + r'(?![a-zA-Zа-яёА-ЯЁ0-9])',
        re.IGNORECASE
    )

def patterns_rebuild():
    """Перекомпилирует паттерны после изменения конфига."""
    global _keyword_patterns, _stopword_patterns
    _keyword_patterns  = [(w, _make_word_pattern(w)) for w in _config['keywords']]
    _stopword_patterns = [(w, _make_word_pattern(w)) for w in _config['stopwords']]
    logger.debug(f"Паттерны обновлены: {len(_keyword_patterns)} ключевых, {len(_stopword_patterns)} стоп.")

def config_init():
    global _config
    _config = config_load()
    patterns_rebuild()

# ╔══════════════════════════════════════════════════════╗
# ║                  АНТИСПАМ                            ║
# ╚══════════════════════════════════════════════════════╝

# chat_id -> список timestamp последних пересылок
_spam_counters: dict[int, list[float]] = defaultdict(list)

def spam_check(chat_id: int) -> bool:
    """True = разрешено пересылать, False = лимит превышен."""
    now = datetime.now(timezone.utc).timestamp()
    window_start = now - SPAM_WINDOW_SECONDS
    timestamps = [t for t in _spam_counters[chat_id] if t > window_start]
    _spam_counters[chat_id] = timestamps
    if len(timestamps) >= MAX_FORWARDS_PER_CHAT:
        return False
    _spam_counters[chat_id].append(now)
    return True

# ╔══════════════════════════════════════════════════════╗
# ║                    ФИЛЬТРЫ                           ║
# ╚══════════════════════════════════════════════════════╝

def check_message(text: str) -> str | None:
    """
    Возвращает найденное ключевое слово или None.
    Проверяет целые слова (кириллица + латиница).
    """
    if not text:
        return None
    text_lower = text.lower()

    for word, pattern in _stopword_patterns:
        if pattern.search(text_lower):
            logger.debug(f"Стоп-слово «{word}» — пропуск.")
            return None

    for word, pattern in _keyword_patterns:
        if pattern.search(text_lower):
            logger.debug(f"Ключевое слово «{word}» — совпадение.")
            return word

    return None

def chat_type_allowed(entity) -> bool:
    """Проверяет, разрешён ли тип чата по настройкам."""
    if isinstance(entity, User):
        return LISTEN_PRIVATE
    if isinstance(entity, Chat):
        return LISTEN_GROUPS
    if isinstance(entity, Channel):
        return LISTEN_CHANNELS if entity.broadcast else LISTEN_GROUPS
    return True

# ╔══════════════════════════════════════════════════════╗
# ║                    ПЕРЕСЫЛКА                         ║
# ╚══════════════════════════════════════════════════════╝

async def get_message_link(client: TelegramClient, message) -> str:
    """Формирует ссылку на оригинальное сообщение."""
    try:
        chat = await message.get_chat()
        username = getattr(chat, 'username', None)
        if username:
            return f"https://t.me/{username}/{message.id}"
        # Для приватных чатов/групп ссылка через ID
        chat_id_str = str(message.chat_id).replace('-100', '')
        return f"https://t.me/c/{chat_id_str}/{message.id}"
    except Exception:
        return ""

async def forward_message(client: TelegramClient, message, keyword: str) -> bool:
    """
    Пересылает сообщение + контекст.
    Возвращает True при успехе.
    """
    chat_id = message.chat_id

    if db_was_forwarded(message.id, chat_id):
        return False

    if not spam_check(chat_id):
        logger.warning(f"⛔ Антиспам: слишком много пересылок из чата {chat_id}. Пропуск.")
        return False

    # Собираем контекст (сообщения до и после)
    messages_to_forward = []
    if CONTEXT_BEFORE > 0 or CONTEXT_AFTER > 0:
        try:
            ctx_msgs = await client.get_messages(
                chat_id,
                min_id=message.id - CONTEXT_BEFORE - 1,
                max_id=message.id + CONTEXT_AFTER + 1,
                limit=CONTEXT_BEFORE + CONTEXT_AFTER + 1
            )
            messages_to_forward = sorted(ctx_msgs, key=lambda m: m.id)
        except Exception:
            messages_to_forward = [message]
    else:
        messages_to_forward = [message]

    msg_link = await get_message_link(client, message)

    try:
        # Отправляем заголовок с источником и ссылкой
        chat = await message.get_chat()
        chat_title = getattr(chat, 'title', None) or getattr(chat, 'username', str(chat_id))
        sender = await message.get_sender()
        sender_name = (
            getattr(sender, 'first_name', '') or
            getattr(sender, 'username', '') or
            'Unknown'
        )
        header = (
            f"🔑 Ключевое слово: *{keyword}*\n"
            f"💬 Чат: {chat_title}\n"
            f"👤 Отправитель: {sender_name}\n"
            f"🕐 {message.date.strftime('%Y-%m-%d %H:%M UTC')}\n"
            + (f"🔗 [Перейти к сообщению]({msg_link})" if msg_link else "")
        )
        await client.send_message(DESTINATION_CHAT_ID, header, parse_mode='md', link_preview=False)

        # Пересылаем само сообщение (и контекст)
        for msg in messages_to_forward:
            try:
                await client.forward_messages(DESTINATION_CHAT_ID, msg)
            except ChatForwardsRestrictedError:
                # Пересылка запрещена — копируем текст
                if msg.text:
                    await client.send_message(DESTINATION_CHAT_ID, f"📋 _{msg.text}_", parse_mode='md')

        db_mark_forwarded(message.id, chat_id)
        db_add_stat(chat_id, keyword)
        logger.info(f"✅ Переслано: chat={chat_id}, msg_id={message.id}, keyword=«{keyword}»")
        return True

    except FloodWaitError as e:
        logger.warning(f"⏳ FloodWait {e.seconds}с.")
        await asyncio.sleep(e.seconds)
        return False
    except Exception as e:
        logger.error(f"❌ Ошибка пересылки msg_id={message.id}: {e}")
        return False

# ╔══════════════════════════════════════════════════════╗
# ║            ПЕРИОДИЧЕСКОЕ СКАНИРОВАНИЕ                ║
# ╚══════════════════════════════════════════════════════╝

async def scan_chats(client: TelegramClient, dialogs) -> None:
    since = datetime.now(timezone.utc) - timedelta(seconds=SCAN_INTERVAL)
    logger.info(f"🔍 Сканирование с {since.strftime('%H:%M:%S')} UTC ({len(dialogs)} чатов)...")
    found = 0

    for dialog in dialogs:
        entity = dialog if not hasattr(dialog, 'entity') else dialog.entity
        if not chat_type_allowed(entity):
            continue
        try:
            async for message in client.iter_messages(dialog.id, limit=100):
                if message.date and message.date < since:
                    break
                if message.text:
                    keyword = check_message(message.text)
                    if keyword:
                        if await forward_message(client, message, keyword):
                            found += 1
        except Exception as e:
            logger.error(f"Ошибка сканирования чата {dialog.id}: {e}")

    logger.info(f"🔍 Сканирование завершено. Переслано новых: {found}.")

async def periodic_scanner(client: TelegramClient) -> None:
    """Фоновая задача: сканирование + очистка БД каждые SCAN_INTERVAL секунд."""
    while True:
        await asyncio.sleep(SCAN_INTERVAL)
        try:
            dialogs = await get_target_dialogs(client)
            await scan_chats(client, dialogs)
            db_cleanup_old()
        except Exception as e:
            logger.error(f"Ошибка периодического сканирования: {e}")

# ╔══════════════════════════════════════════════════════╗
# ║              ПОЛУЧЕНИЕ СПИСКА ЧАТОВ                  ║
# ╚══════════════════════════════════════════════════════╝

async def get_target_dialogs(client: TelegramClient):
    if SOURCE_CHATS:
        dialogs = []
        for chat in SOURCE_CHATS:
            try:
                entity = await client.get_entity(chat)
                dialogs.append(entity)
            except Exception as e:
                logger.error(f"Не удалось получить чат {chat!r}: {e}")
        return dialogs
    return await client.get_dialogs()

# ╔══════════════════════════════════════════════════════╗
# ║           КОМАНДЫ УПРАВЛЕНИЯ (из Telegram)           ║
# ╚══════════════════════════════════════════════════════╝

def register_commands(client: TelegramClient, my_id: int):
    """Регистрирует обработчики команд — только от самого владельца."""

    async def only_owner(event) -> bool:
        return event.sender_id == my_id

    @client.on(events.NewMessage(pattern=r'^/addkeyword (.+)', func=only_owner))
    async def cmd_add_keyword(event):
        word = event.pattern_match.group(1).strip().lower()
        if word in _config['keywords']:
            await event.respond(f"⚠️ «{word}» уже есть в ключевых словах.")
            return
        _config['keywords'].append(word)
        config_save(_config)
        patterns_rebuild()
        await event.respond(f"✅ Ключевое слово «{word}» добавлено.")

    @client.on(events.NewMessage(pattern=r'^/delkeyword (.+)', func=only_owner))
    async def cmd_del_keyword(event):
        word = event.pattern_match.group(1).strip().lower()
        if word not in _config['keywords']:
            await event.respond(f"⚠️ «{word}» не найдено в ключевых словах.")
            return
        _config['keywords'].remove(word)
        config_save(_config)
        patterns_rebuild()
        await event.respond(f"🗑 Ключевое слово «{word}» удалено.")

    @client.on(events.NewMessage(pattern=r'^/addstop (.+)', func=only_owner))
    async def cmd_add_stop(event):
        word = event.pattern_match.group(1).strip().lower()
        if word in _config['stopwords']:
            await event.respond(f"⚠️ «{word}» уже есть в стоп-словах.")
            return
        _config['stopwords'].append(word)
        config_save(_config)
        patterns_rebuild()
        await event.respond(f"✅ Стоп-слово «{word}» добавлено.")

    @client.on(events.NewMessage(pattern=r'^/delstop (.+)', func=only_owner))
    async def cmd_del_stop(event):
        word = event.pattern_match.group(1).strip().lower()
        if word not in _config['stopwords']:
            await event.respond(f"⚠️ «{word}» не найдено в стоп-словах.")
            return
        _config['stopwords'].remove(word)
        config_save(_config)
        patterns_rebuild()
        await event.respond(f"🗑 Стоп-слово «{word}» удалено.")

    @client.on(events.NewMessage(pattern=r'^/keywords$', func=only_owner))
    async def cmd_keywords(event):
        kws = '\n'.join(f"  • {w}" for w in _config['keywords']) or '  (пусто)'
        await event.respond(f"🔑 *Ключевые слова:*\n{kws}", parse_mode='md')

    @client.on(events.NewMessage(pattern=r'^/stopwords$', func=only_owner))
    async def cmd_stopwords(event):
        sws = '\n'.join(f"  • {w}" for w in _config['stopwords']) or '  (пусто)'
        await event.respond(f"🚫 *Стоп-слова:*\n{sws}", parse_mode='md')

    @client.on(events.NewMessage(pattern=r'^/stats$', func=only_owner))
    async def cmd_stats(event):
        s = db_get_stats()
        top_kw = '\n'.join(f"  {kw}: {cnt}" for kw, cnt in s['top_keywords']) or '  —'
        top_ch = '\n'.join(f"  {cid}: {cnt}" for cid, cnt in s['top_chats']) or '  —'
        text = (
            f"📊 *Статистика*\n\n"
            f"Всего пересылок: *{s['total']}*\n"
            f"Сегодня: *{s['today']}*\n\n"
            f"Топ ключевых слов:\n{top_kw}\n\n"
            f"Топ чатов-источников:\n{top_ch}"
        )
        await event.respond(text, parse_mode='md')

    @client.on(events.NewMessage(pattern=r'^/status$', func=only_owner))
    async def cmd_status(event):
        kw_count = len(_config['keywords'])
        sw_count = len(_config['stopwords'])
        await event.respond(
            f"🟢 *Бот активен*\n\n"
            f"Ключевых слов: {kw_count}\n"
            f"Стоп-слов: {sw_count}\n"
            f"Сканирование каждые: {SCAN_INTERVAL // 60} мин.\n"
            f"Контекст: {CONTEXT_BEFORE} до / {CONTEXT_AFTER} после\n"
            f"Антиспам: макс. {MAX_FORWARDS_PER_CHAT} за {SPAM_WINDOW_SECONDS}с.",
            parse_mode='md'
        )

# ╔══════════════════════════════════════════════════════╗
# ║                  ОСНОВНАЯ ЛОГИКА                     ║
# ╚══════════════════════════════════════════════════════╝

async def main():
    # Инициализация
    db_init()
    config_init()

    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

    await client.start()

    me = await client.get_me()
    my_id = me.id
    logger.info(f"🚀 Запущен как: {me.first_name} (ID: {my_id})")

    # Регистрируем команды управления
    register_commands(client, my_id)

    # Уведомление о старте
    try:
        await client.send_message(
            DESTINATION_CHAT_ID,
            f"🟢 *Бот запущен*\n"
            f"Ключевых слов: {len(_config['keywords'])}\n"
            f"Стоп-слов: {len(_config['stopwords'])}\n"
            f"Сканирование каждые {SCAN_INTERVAL // 60} мин.",
            parse_mode='md'
        )
    except Exception as e:
        logger.warning(f"Не удалось отправить уведомление о старте: {e}")

    dialogs = await get_target_dialogs(client)
    logger.info(f"📋 Отслеживается чатов: {len(dialogs)}")

    # Обработчик новых сообщений в реальном времени
    @client.on(events.NewMessage(chats=SOURCE_CHATS if SOURCE_CHATS else None))
    async def handler(event):
        if event.sender_id == my_id:
            return

        # Проверяем тип чата
        try:
            entity = await event.get_chat()
            if not chat_type_allowed(entity):
                return
        except Exception:
            pass

        if event.message.text:
            keyword = check_message(event.message.text)
            if keyword:
                await forward_message(client, event.message, keyword)

    # Фоновое периодическое сканирование
    scanner_task = asyncio.ensure_future(periodic_scanner(client))
    logger.info(f"⏱️  Периодическое сканирование каждые {SCAN_INTERVAL // 60} мин. запущено.")

    try:
        await client.run_until_disconnected()
    except (KeyboardInterrupt, SystemExit):
        logger.info("🛑 Остановка бота...")
    except Exception as e:
        logger.critical(f"💥 Критическая ошибка: {e}")
        # Уведомляем владельца о падении
        try:
            await client.send_message(
                DESTINATION_CHAT_ID,
                f"🔴 *Бот упал!*\nОшибка: `{e}`",
                parse_mode='md'
            )
        except Exception:
            pass
        raise
    finally:
        scanner_task.cancel()
        try:
            await client.send_message(DESTINATION_CHAT_ID, "🔴 Бот остановлен.")
        except Exception:
            pass
        await client.disconnect()
        logger.info("✅ Бот остановлен.")


if __name__ == '__main__':
    asyncio.run(main())
