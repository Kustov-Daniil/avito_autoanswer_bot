#!/bin/bash
# Скрипт для настройки webhook на VDS

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

if [ "$EUID" -ne 0 ]; then 
    echo -e "${RED}Пожалуйста, запустите скрипт от root${NC}"
    exit 1
fi

echo -e "${YELLOW}🌐 Настройка webhook на VDS${NC}"

# Установка nginx
echo -e "${YELLOW}📦 Установка nginx...${NC}"
apt update
apt install -y nginx

# Получение IP адреса или домена
echo ""
echo -e "${YELLOW}Введите ваш домен (например: bot.example.com) или нажмите Enter для использования IP:${NC}"
read -r DOMAIN

if [ -z "$DOMAIN" ]; then
    # Используем IP адрес
    DOMAIN=$(curl -s ifconfig.me || hostname -I | awk '{print $1}')
    echo -e "${YELLOW}Используется IP адрес: $DOMAIN${NC}"
fi

PROJECT_DIR="/home/avito_autoanswer_bot"
NGINX_CONF="/etc/nginx/sites-available/avito_autoanswer_bot"

# Создание конфигурации nginx
echo -e "${YELLOW}⚙️  Создание конфигурации nginx...${NC}"
cat > "$NGINX_CONF" << EOF
server {
    listen 80;
    server_name $DOMAIN;

    access_log /var/log/nginx/avito_autoanswer_bot_access.log;
    error_log /var/log/nginx/avito_autoanswer_bot_error.log;

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
echo -e "${YELLOW}🔗 Активация конфигурации nginx...${NC}"
ln -sf "$NGINX_CONF" /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default  # Удаляем дефолтную конфигурацию

# Проверка конфигурации
echo -e "${YELLOW}✅ Проверка конфигурации nginx...${NC}"
nginx -t

# Перезапуск nginx
echo -e "${YELLOW}🔄 Перезапуск nginx...${NC}"
systemctl restart nginx
systemctl enable nginx

# Настройка firewall (если установлен ufw)
if command -v ufw &> /dev/null; then
    echo -e "${YELLOW}🔥 Настройка firewall...${NC}"
    ufw allow 'Nginx Full'
    ufw allow 22/tcp  # SSH
    ufw --force enable || true
fi

# Обновление .env файла
if [ -f "$PROJECT_DIR/.env" ]; then
    echo -e "${YELLOW}📝 Обновление .env файла...${NC}"
    
    # Определяем протокол (HTTP или HTTPS)
    PROTOCOL="http"
    if [ -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ]; then
        PROTOCOL="https"
    fi
    
    PUBLIC_URL="$PROTOCOL://$DOMAIN"
    
    # Обновляем или добавляем PUBLIC_BASE_URL
    if grep -q "PUBLIC_BASE_URL" "$PROJECT_DIR/.env"; then
        sed -i "s|PUBLIC_BASE_URL=.*|PUBLIC_BASE_URL=$PUBLIC_URL|" "$PROJECT_DIR/.env"
    else
        echo "" >> "$PROJECT_DIR/.env"
        echo "PUBLIC_BASE_URL=$PUBLIC_URL" >> "$PROJECT_DIR/.env"
    fi
    
    echo -e "${GREEN}✅ PUBLIC_BASE_URL установлен: $PUBLIC_URL${NC}"
else
    echo -e "${YELLOW}⚠️  Файл .env не найден. Создайте его вручную и добавьте:${NC}"
    echo "   PUBLIC_BASE_URL=http://$DOMAIN"
fi

echo ""
echo -e "${GREEN}✅ Webhook настроен!${NC}"
echo ""
echo "🌐 Ваш webhook URL: http://$DOMAIN/avito/webhook"
echo ""
echo "📋 Следующие шаги:"
echo "   1. Обновите PUBLIC_BASE_URL в .env файле: http://$DOMAIN"
echo "   2. Подпишитесь на webhook через Telegram бота: /subscribe"
echo "   3. (Опционально) Настройте SSL сертификат для HTTPS"
echo ""
echo "🔒 Для настройки SSL (HTTPS) выполните:"
echo "   certbot --nginx -d $DOMAIN"

