#!/bin/bash
# Автоматическая установка и настройка проекта с GitHub

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║   Автоматическая установка avito_autoanswer_bot            ║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════════════════════════╝${NC}"
echo ""

if [ "$EUID" -ne 0 ]; then 
    echo -e "${RED}❌ Пожалуйста, запустите скрипт от root${NC}"
    exit 1
fi

# Параметры
PROJECT_DIR="/home/avito_autoanswer_bot"
SERVICE_NAME="avito_autoanswer_bot"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
PYTHON_VERSION="3.11"
GITHUB_REPO="${1:-https://github.com/Kustov-Daniil/avito_autoanswer_bot.git}"

echo -e "${YELLOW}📋 Параметры установки:${NC}"
echo "   Директория проекта: $PROJECT_DIR"
echo "   Репозиторий: $GITHUB_REPO"
echo "   Python версия: $PYTHON_VERSION"
echo ""

# Шаг 1: Обновление системы
echo -e "${YELLOW}📦 Шаг 1/8: Обновление системы...${NC}"
apt update -y
apt install -y python${PYTHON_VERSION}-venv python3-pip rsync curl git nginx certbot python3-certbot-nginx ufw

# Шаг 2: Создание директории проекта
echo -e "${YELLOW}📁 Шаг 2/8: Создание директории проекта...${NC}"
if [ -d "$PROJECT_DIR" ]; then
    echo -e "${YELLOW}⚠️  Директория $PROJECT_DIR уже существует${NC}"
    read -p "Удалить и пересоздать? (y/N): " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        rm -rf "$PROJECT_DIR"
    else
        echo -e "${YELLOW}Используем существующую директорию...${NC}"
    fi
fi

if [ ! -d "$PROJECT_DIR" ]; then
    mkdir -p "$PROJECT_DIR"
fi

# Шаг 3: Клонирование проекта
echo -e "${YELLOW}📥 Шаг 3/8: Клонирование проекта с GitHub...${NC}"
if [ -d "$PROJECT_DIR/.git" ]; then
    echo -e "${YELLOW}Обновление существующего репозитория...${NC}"
    cd "$PROJECT_DIR"
    git pull || {
        echo -e "${YELLOW}⚠️  Не удалось обновить. Пересоздаем...${NC}"
        cd /
        rm -rf "$PROJECT_DIR"
        mkdir -p "$PROJECT_DIR"
        git clone "$GITHUB_REPO" "$PROJECT_DIR"
    }
else
    git clone "$GITHUB_REPO" "$PROJECT_DIR"
fi

cd "$PROJECT_DIR"

# Шаг 4: Создание виртуального окружения
echo -e "${YELLOW}🐍 Шаг 4/8: Создание виртуального окружения...${NC}"
if [ ! -d "$PROJECT_DIR/venv" ]; then
    python${PYTHON_VERSION} -m venv "$PROJECT_DIR/venv"
fi

source "$PROJECT_DIR/venv/bin/activate"
pip install --upgrade pip
pip install -r "$PROJECT_DIR/requirements.txt"
deactivate

# Шаг 5: Проверка .env файла
echo -e "${YELLOW}⚙️  Шаг 5/8: Проверка .env файла...${NC}"
if [ ! -f "$PROJECT_DIR/.env" ]; then
    echo -e "${RED}❌ Файл .env не найден!${NC}"
    echo -e "${YELLOW}⚠️  ВАЖНО: Создайте файл .env вручную перед запуском сервиса!${NC}"
    echo ""
    echo "Создайте файл:"
    echo "  nano $PROJECT_DIR/.env"
    echo ""
    echo "И добавьте все необходимые переменные (см. config.py или DEPLOYMENT.md)"
    echo ""
    read -p "Продолжить установку без .env файла? (y/N): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo -e "${YELLOW}Установка прервана. Создайте .env файл и запустите скрипт снова.${NC}"
        exit 1
    fi
else
    echo -e "${GREEN}✅ Файл .env найден${NC}"
fi

# Шаг 6: Настройка systemd сервиса
echo -e "${YELLOW}🔧 Шаг 6/8: Настройка systemd сервиса...${NC}"
if [ -f "$PROJECT_DIR/avito_autoanswer_bot.service" ]; then
    cp "$PROJECT_DIR/avito_autoanswer_bot.service" "$SERVICE_FILE"
    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME"
    echo -e "${GREEN}✅ Сервис настроен${NC}"
else
    echo -e "${RED}❌ Файл avito_autoanswer_bot.service не найден${NC}"
fi

# Шаг 7: Настройка nginx и webhook
echo -e "${YELLOW}🌐 Шаг 7/8: Настройка nginx и webhook...${NC}"

# Получение IP или домена
if [ -z "$2" ]; then
    echo ""
    echo -e "${YELLOW}Введите ваш домен (например: bot.example.com) или нажмите Enter для использования IP:${NC}"
    read -r DOMAIN
fi

if [ -z "$DOMAIN" ]; then
    DOMAIN=$(curl -s ifconfig.me || hostname -I | awk '{print $1}')
    echo -e "${YELLOW}Используется IP адрес: $DOMAIN${NC}"
fi

# Создание конфигурации nginx
NGINX_CONF="/etc/nginx/sites-available/${SERVICE_NAME}"
cat > "$NGINX_CONF" << EOF
server {
    listen 80;
    server_name $DOMAIN;

    access_log /var/log/nginx/${SERVICE_NAME}_access.log;
    error_log /var/log/nginx/${SERVICE_NAME}_error.log;

    client_max_body_size 10M;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        
        proxy_connect_timeout 60s;
        proxy_send_timeout 60s;
        proxy_read_timeout 60s;
    }

    location /health {
        proxy_pass http://127.0.0.1:8080/health;
        access_log off;
    }
}
EOF

# Активация конфигурации
ln -sf "$NGINX_CONF" /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default

# Проверка и перезапуск nginx
nginx -t
systemctl restart nginx
systemctl enable nginx

# Обновление PUBLIC_BASE_URL в .env
PROTOCOL="http"
if [ -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ]; then
    PROTOCOL="https"
fi

PUBLIC_URL="$PROTOCOL://$DOMAIN"
if grep -q "PUBLIC_BASE_URL" "$PROJECT_DIR/.env"; then
    sed -i "s|PUBLIC_BASE_URL=.*|PUBLIC_BASE_URL=$PUBLIC_URL|" "$PROJECT_DIR/.env"
else
    echo "" >> "$PROJECT_DIR/.env"
    echo "PUBLIC_BASE_URL=$PUBLIC_URL" >> "$PROJECT_DIR/.env"
fi

echo -e "${GREEN}✅ Nginx настроен${NC}"
echo -e "${GREEN}✅ PUBLIC_BASE_URL установлен: $PUBLIC_URL${NC}"

# Настройка firewall
echo -e "${YELLOW}🔥 Настройка firewall...${NC}"
ufw allow 'Nginx Full'
ufw allow 22/tcp
ufw --force enable || true

# Шаг 8: Запуск сервиса
echo -e "${YELLOW}🚀 Шаг 8/8: Запуск сервиса...${NC}"
systemctl restart "$SERVICE_NAME"
sleep 2

# Проверка статуса
if systemctl is-active --quiet "$SERVICE_NAME"; then
    echo -e "${GREEN}✅ Сервис запущен успешно${NC}"
else
    echo -e "${RED}❌ Сервис не запустился. Проверьте логи:${NC}"
    echo "   journalctl -u $SERVICE_NAME -n 50"
fi

# Итоговая информация
echo ""
echo -e "${BLUE}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║                    Установка завершена!                      ║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "${GREEN}✅ Проект установлен в: $PROJECT_DIR${NC}"
echo -e "${GREEN}✅ Webhook URL: $PUBLIC_URL/avito/webhook${NC}"
echo ""
echo -e "${YELLOW}📋 Следующие шаги:${NC}"
echo ""
if [ ! -f "$PROJECT_DIR/.env" ]; then
    echo -e "${RED}1. Создайте .env файл и заполните все переменные:${NC}"
    echo "   nano $PROJECT_DIR/.env"
    echo ""
    echo "2. Перезапустите сервис:"
    echo "   systemctl restart $SERVICE_NAME"
else
    echo "1. Перезапустите сервис (если нужно):"
    echo "   systemctl restart $SERVICE_NAME"
fi
echo ""
echo "2. Подпишитесь на webhook Avito через Telegram бота:"
echo "   /subscribe"
echo ""
echo "3. (Опционально) Настройте SSL для HTTPS:"
echo "   certbot --nginx -d $DOMAIN"
echo "   Затем обновите PUBLIC_BASE_URL в .env на https://$DOMAIN"
echo ""
echo -e "${YELLOW}📊 Полезные команды:${NC}"
echo "   Статус сервиса: systemctl status $SERVICE_NAME"
echo "   Логи сервиса: journalctl -u $SERVICE_NAME -f"
echo "   Логи приложения: tail -f $PROJECT_DIR/data/logs/bot.log"
echo "   Логи nginx: tail -f /var/log/nginx/${SERVICE_NAME}_access.log"
echo ""
echo -e "${GREEN}🎉 Готово!${NC}"

