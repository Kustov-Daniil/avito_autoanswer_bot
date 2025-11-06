#!/bin/bash
# Скрипт для первоначальной настройки сервера

set -e

echo "🚀 Начало развертывания Avito Autoanswer Bot"

# Цвета для вывода
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Проверка, что скрипт запущен от root
if [ "$EUID" -ne 0 ]; then 
    echo -e "${RED}Пожалуйста, запустите скрипт от root${NC}"
    exit 1
fi

PROJECT_DIR="/home/avito_autoanswer_bot"
REPO_URL="https://github.com/Kustov-Daniil/avito_autoanswer_bot.git"

echo -e "${YELLOW}📦 Установка необходимых пакетов...${NC}"
apt update
apt install -y python3 python3-pip python3-venv git build-essential python3-dev

echo -e "${YELLOW}📁 Создание директории проекта...${NC}"
mkdir -p "$PROJECT_DIR"
cd "$PROJECT_DIR"

if [ -d ".git" ]; then
    echo -e "${YELLOW}📥 Обновление репозитория...${NC}"
    git pull origin main
else
    echo -e "${YELLOW}📥 Клонирование репозитория...${NC}"
    git clone "$REPO_URL" .
fi

echo -e "${YELLOW}🐍 Создание виртуального окружения...${NC}"
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi

source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo -e "${YELLOW}📝 Создание необходимых директорий...${NC}"
mkdir -p data/logs
chmod 755 data
chmod 755 data/logs

echo -e "${YELLOW}⚙️  Настройка systemd сервиса...${NC}"
if [ -f "avito_autoanswer_bot.service" ]; then
    cp avito_autoanswer_bot.service /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable avito_autoanswer_bot.service
    echo -e "${GREEN}✅ Сервис установлен и включен${NC}"
else
    echo -e "${RED}❌ Файл avito_autoanswer_bot.service не найден${NC}"
    exit 1
fi

echo -e "${YELLOW}📋 Проверка .env файла...${NC}"
if [ ! -f ".env" ]; then
    echo -e "${YELLOW}⚠️  Файл .env не найден. Создайте его вручную:${NC}"
    echo "   nano $PROJECT_DIR/.env"
    echo ""
    echo "Необходимые переменные:"
    echo "  - TELEGRAM_BOT_TOKEN"
    echo "  - TELEGRAM_MANAGER_ID или MANAGERS"
    echo "  - ADMINS"
    echo "  - AVITO_CLIENT_ID"
    echo "  - AVITO_CLIENT_SECRET"
    echo "  - AVITO_ACCOUNT_ID"
    echo "  - OPENAI_API_KEY"
    echo "  - PUBLIC_BASE_URL (будет настроен после настройки webhook)"
else
    echo -e "${GREEN}✅ Файл .env найден${NC}"
fi

echo ""
echo -e "${YELLOW}🌐 Настройка webhook (nginx)...${NC}"
echo "Для настройки webhook выполните:"
echo "   chmod +x setup_webhook.sh"
echo "   ./setup_webhook.sh"
echo ""
echo "После настройки webhook URL будет: http://your-domain-or-ip/avito/webhook"

echo -e "${YELLOW}🔄 Запуск сервиса...${NC}"
systemctl restart avito_autoanswer_bot.service
sleep 2

echo -e "${YELLOW}📊 Статус сервиса:${NC}"
systemctl status avito_autoanswer_bot.service --no-pager

echo ""
echo -e "${GREEN}✅ Развертывание завершено!${NC}"
echo ""
echo "Полезные команды:"
echo "  systemctl status avito_autoanswer_bot.service  # Статус сервиса"
echo "  systemctl restart avito_autoanswer_bot.service  # Перезапуск"
echo "  journalctl -u avito_autoanswer_bot.service -f  # Логи в реальном времени"
echo "  tail -f $PROJECT_DIR/data/logs/bot.log         # Логи приложения"

