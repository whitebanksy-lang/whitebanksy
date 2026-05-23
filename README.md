# Telegram Forwarder Bot — инструкция по деплою

## Содержимое архива

```
telegram_forwarder.py   — основной скрипт бота
requirements.txt        — зависимости Python
tg-forwarder.service    — systemd-сервис (автозапуск)
deploy.sh               — скрипт автоматической установки
README.md               — эта инструкция
```

---

## Быстрый старт (Ubuntu 20.04 / 22.04 / 24.04)

### Шаг 1 — Загрузить файлы на сервер

```bash
# С локальной машины (заменить user@your-server-ip)
scp telegram_forwarder.py requirements.txt tg-forwarder.service deploy.sh \
    user@your-server-ip:~/tg-forwarder/
```

### Шаг 2 — Запустить установку

```bash
ssh user@your-server-ip
cd ~/tg-forwarder
sudo bash deploy.sh
```

Скрипт автоматически:
- установит Python 3 и venv
- создаст системного пользователя `botuser`
- создаст директорию `/opt/tg-forwarder`
- установит зависимости
- запустит первичную авторизацию (нужно ввести код из Telegram)
- установит и включит systemd-сервис

---

## Ручная установка (если автоскрипт не нужен)

```bash
# 1. Зависимости
sudo apt-get install -y python3 python3-venv

# 2. Директория
sudo mkdir -p /opt/tg-forwarder
sudo cp telegram_forwarder.py requirements.txt /opt/tg-forwarder/

# 3. Виртуальное окружение
python3 -m venv /opt/tg-forwarder/venv
/opt/tg-forwarder/venv/bin/pip install -r /opt/tg-forwarder/requirements.txt

# 4. Первичная авторизация (нужна интерактивно, один раз)
cd /opt/tg-forwarder
python3 telegram_forwarder.py
# Введите код из SMS → Ctrl+C после появления строки "Бот запущен"

# 5. Сервис
sudo cp tg-forwarder.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable tg-forwarder
sudo systemctl start tg-forwarder
```

---

## Управление сервисом

```bash
# Статус
sudo systemctl status tg-forwarder

# Логи в реальном времени
sudo journalctl -u tg-forwarder -f

# Логи за последний час
sudo journalctl -u tg-forwarder --since "1 hour ago"

# Перезапуск (например, после изменения конфига)
sudo systemctl restart tg-forwarder

# Остановка
sudo systemctl stop tg-forwarder

# Отключить автозапуск
sudo systemctl disable tg-forwarder
```

---

## Файлы, которые создаёт бот в /opt/tg-forwarder/

| Файл | Описание |
|------|----------|
| `session_name.session` | Сессия Telegram (не удалять!) |
| `forwarder.db` | SQLite: история пересылок и статистика |
| `forwarder_config.json` | Текущие ключевые и стоп-слова |
| `forwarder.log` | Лог-файл бота |

---

## Команды управления (пишите себе в Telegram)

| Команда | Описание |
|---------|----------|
| `/addkeyword слово` | Добавить ключевое слово |
| `/delkeyword слово` | Удалить ключевое слово |
| `/addstop слово` | Добавить стоп-слово |
| `/delstop слово` | Удалить стоп-слово |
| `/keywords` | Список ключевых слов |
| `/stopwords` | Список стоп-слов |
| `/stats` | Статистика пересылок |
| `/status` | Статус и настройки бота |

---

## Частые проблемы

**Ошибка авторизации / протухшая сессия**
```bash
cd /opt/tg-forwarder
rm session_name.session
sudo systemctl stop tg-forwarder
python3 telegram_forwarder.py   # авторизоваться заново
sudo systemctl start tg-forwarder
```

**FloodWaitError в логах** — Telegram временно ограничил запросы, бот сам подождёт и продолжит. Норма при первом запуске с большим числом чатов.

**Бот не видит сообщения из каналов** — убедитесь что `LISTEN_CHANNELS = True` в конфиге и что вы подписаны на эти каналы.

**Проверить, что бот запущен и работает**
```bash
sudo systemctl is-active tg-forwarder   # должно быть "active"
sudo journalctl -u tg-forwarder -n 20   # последние 20 строк лога
```
