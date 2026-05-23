import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from telethon import TelegramClient, events
from telethon.errors import ChatForwardsRestrictedError, FloodWaitError

# ================= КОНФИГУРАЦИЯ =================
API_ID = 21365620
API_HASH = 'acc1684eac294ea198631be3c9b14aa9'
PHONE_NUMBER = '+1 645 226 1496'

# ID чата, КУДА пересылать сообщения (должен быть int!)
DESTINATION_CHAT_ID = 8734055326

# Интервал периодического сканирования (секунды)
SCAN_INTERVAL = 5 * 60 # 5 минут

# Список ключевых слов
KEYWORDS = [
    'бот',
    'чат-бот',
    'техспец',
    'технический специалист',
    'Getcourse'
]

# Список стоп-слов
STOP_WORDS = [
    'помогу',
    'я работаю',
    'флуд',
    'test'
]

# Список чатов, ГДЕ слушать (пусто = все чаты)
# Можно указывать username, ссылку или числовой ID
SOURCE_CHATS = []

# Включить подробное логирование
DEBUG_MODE = False

# ================= ЛОГИРОВАНИЕ =================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.DEBUG if DEBUG_MODE else logging.INFO
)
logger = logging.getLogger(__name__)

# ================= СОСТОЯНИЕ =================
forwarded_ids: set[int] = set()   # защита от дублей

# ================= ФИЛЬТРЫ =================
def _make_word_pattern(phrase: str) -> re.Pattern:
    """
    Компилирует паттерн для поиска фразы как отдельного слова/словосочетания.
    Стандартный \\b не работает с кириллицей, поэтому используем явные
    символьные классы: граница = не буква и не цифра (кириллица + латиница).

    Примеры для слова «бот»:
      ✅ «нужен бот»  ✅ «бот?»  ✅ «[бот]»  ✅ «чат-бот»
      ❌ «ботинок»   ❌ «робота»
    """
    escaped = re.escape(phrase.lower())
    return re.compile(
        r'(?<![a-zA-Zа-яёА-ЯЁ0-9])' + escaped + r'(?![a-zA-Zа-яёА-ЯЁ0-9])',
        re.IGNORECASE
    )

# Предкомпилируем паттерны один раз при старте
_KEYWORD_PATTERNS  = [_make_word_pattern(kw) for kw in KEYWORDS]
_STOPWORD_PATTERNS = [_make_word_pattern(sw) for sw in STOP_WORDS]

def check_message(text: str) -> bool:
    """
    Возвращает True, если текст содержит ключевое слово КАК ОТДЕЛЬНОЕ СЛОВО
    и НЕ содержит стоп-слово как отдельное слово.

    Пример: слово «бот» НЕ сработает на «работа», «робот», «ботинок»,
    но сработает на «нужен бот», «бот?», «[бот]», «БОТ».
    """
    if not text:
        return False

    text_lower = text.lower()

    for i, pattern in enumerate(_STOPWORD_PATTERNS):
        if pattern.search(text_lower):
            logger.debug(f"Стоп-слово «{STOP_WORDS[i]}» найдено как отдельное слово — пропуск.")
            return False

    for i, pattern in enumerate(_KEYWORD_PATTERNS):
        if pattern.search(text_lower):
            logger.debug(f"Ключевое слово «{KEYWORDS[i]}» найдено как отдельное слово — подходит.")
            return True

    return False

# ================= ПЕРЕСЫЛКА =================
async def forward_message(client: TelegramClient, message) -> None:
    """Пересылает сообщение; при запрете — отправляет текстовую копию."""
    if message.id in forwarded_ids:
        return

    try:
        await client.forward_messages(DESTINATION_CHAT_ID, message)
        forwarded_ids.add(message.id)
        logger.info(f"✅ Переслано: chat={message.chat_id}, msg_id={message.id}")

    except ChatForwardsRestrictedError:
        # Пересылка запрещена — отправляем текстовую копию
        chat = await message.get_chat()
        chat_title = getattr(chat, 'title', None) or getattr(chat, 'username', str(message.chat_id))
        sender = await message.get_sender()
        sender_name = getattr(sender, 'first_name', '') or getattr(sender, 'username', 'Unknown')

        fallback = (
            f"📨 *Сообщение из «{chat_title}»*\n"
            f"👤 От: {sender_name}\n"
            f"🕐 {message.date.strftime('%Y-%m-%d %H:%M:%S UTC')}\n\n"
            f"{message.text}"
        )
        await client.send_message(DESTINATION_CHAT_ID, fallback, parse_mode='md')
        forwarded_ids.add(message.id)
        logger.info(f"📋 Скопировано (пересылка запрещена): chat={message.chat_id}, msg_id={message.id}")

    except FloodWaitError as e:
        logger.warning(f"⏳ FloodWait: ждём {e.seconds} сек.")
        await asyncio.sleep(e.seconds)

    except Exception as e:
        logger.error(f"❌ Ошибка при пересылке msg_id={message.id}: {e}")

# ================= ПЕРИОДИЧЕСКОЕ СКАНИРОВАНИЕ =================
async def scan_chats(client: TelegramClient, dialogs) -> None:
    """Сканирует историю всех целевых чатов за последние SCAN_INTERVAL секунд."""
    since = datetime.now(timezone.utc) - timedelta(seconds=SCAN_INTERVAL)
    logger.info(f"🔍 Сканирование сообщений с {since.strftime('%H:%M:%S')} UTC...")

    for dialog in dialogs:
        try:
            async for message in client.iter_messages(dialog.id, offset_date=None, reverse=False, limit=100):
                # Пропускаем старые сообщения
                if message.date and message.date < since:
                    break
                if message.text and check_message(message.text):
                    await forward_message(client, message)
        except Exception as e:
            logger.error(f"Ошибка сканирования чата {dialog.id}: {e}")

async def periodic_scanner(client: TelegramClient) -> None:
    """Фоновая задача: сканирует чаты каждые SCAN_INTERVAL секунд."""
    while True:
        await asyncio.sleep(SCAN_INTERVAL)
        try:
            dialogs = await get_target_dialogs(client)
            await scan_chats(client, dialogs)
        except Exception as e:
            logger.error(f"Ошибка в периодическом сканировании: {e}")

# ================= ПОЛУЧЕНИЕ СПИСКА ЧАТОВ =================
async def get_target_dialogs(client: TelegramClient):
    """Возвращает список диалогов для мониторинга."""
    if SOURCE_CHATS:
        dialogs = []
        for chat in SOURCE_CHATS:
            try:
                entity = await client.get_entity(chat)
                dialogs.append(entity)
            except Exception as e:
                logger.error(f"Не удалось получить чат {chat!r}: {e}")
        return dialogs
    else:
        return await client.get_dialogs()

# ================= ОСНОВНАЯ ЛОГИКА =================
async def main():
    client = TelegramClient('session_name', API_ID, API_HASH)

    await client.start(phone=PHONE_NUMBER)
    logger.info("🚀 Бот запущен и авторизован!")

    me = await client.get_me()
    my_id = me.id
    logger.info(f"👤 Авторизован как: {me.first_name} (ID: {my_id})")

    dialogs = await get_target_dialogs(client)
    logger.info(f"📋 Отслеживается чатов: {len(dialogs)}")

    # --- Обработчик новых сообщений в реальном времени ---
    @client.on(events.NewMessage(chats=SOURCE_CHATS if SOURCE_CHATS else None))
    async def handler(event):
        # Игнорируем собственные сообщения
        if event.sender_id == my_id:
            return

        if event.message.text and check_message(event.message.text):
            await forward_message(client, event.message)

    # --- Запускаем периодическое сканирование фоном ---
    scanner_task = asyncio.ensure_future(periodic_scanner(client))
    logger.info(f"⏱️ Периодическое сканирование каждые {SCAN_INTERVAL // 60} мин. активно.")

    try:
        await client.run_until_disconnected()
    except (KeyboardInterrupt, SystemExit):
        logger.info("🛑 Остановка бота...")
    finally:
        scanner_task.cancel()
        await client.disconnect()
        logger.info("✅ Бот остановлен.")

if __name__ == '__main__':
    asyncio.run(main())