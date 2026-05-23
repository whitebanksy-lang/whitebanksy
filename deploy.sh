#!/usr/bin/env bash
# =============================================================
#  deploy.sh — установка Telegram Forwarder Bot на Ubuntu/Debian
#  Запускать от root:  sudo bash deploy.sh
# =============================================================
set -euo pipefail

APP_DIR="/opt/tg-forwarder"
SERVICE="tg-forwarder"
BOT_USER="botuser"

echo "==> [1/7] Обновление пакетов и установка Python..."
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv

echo "==> [2/7] Создание системного пользователя $BOT_USER..."
id "$BOT_USER" &>/dev/null || useradd --system --no-create-home --shell /usr/sbin/nologin "$BOT_USER"

echo "==> [3/7] Создание директории $APP_DIR..."
mkdir -p "$APP_DIR"
cp telegram_forwarder.py "$APP_DIR/"
cp requirements.txt      "$APP_DIR/"
chown -R "$BOT_USER":"$BOT_USER" "$APP_DIR"

echo "==> [4/7] Создание виртуального окружения и установка зависимостей..."
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

echo "==> [5/7] Первичная авторизация Telegram (введите код из SMS)..."
echo "    После успешного входа файл session_name.session будет создан в $APP_DIR."
echo "    Нажмите Enter для продолжения или Ctrl+C для пропуска (если сессия уже есть)."
read -r _
cd "$APP_DIR"
sudo -u "$BOT_USER" "$APP_DIR/venv/bin/python" telegram_forwarder.py &
BOT_PID=$!
echo "    Бот запущен (PID $BOT_PID). После авторизации нажмите Ctrl+C."
wait "$BOT_PID" || true
cd - > /dev/null

echo "==> [6/7] Установка systemd-сервиса..."
cp tg-forwarder.service /etc/systemd/system/"$SERVICE".service
systemctl daemon-reload
systemctl enable "$SERVICE"
systemctl restart "$SERVICE"

echo "==> [7/7] Готово!"
echo ""
echo "  Статус:       systemctl status $SERVICE"
echo "  Логи:         journalctl -u $SERVICE -f"
echo "  Перезапуск:   systemctl restart $SERVICE"
echo "  Остановка:    systemctl stop $SERVICE"
