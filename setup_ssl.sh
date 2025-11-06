#!/bin/bash
# Скрипт для настройки SSL сертификата (Let's Encrypt)

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

if [ "$EUID" -ne 0 ]; then 
    echo -e "${RED}Пожалуйста, запустите скрипт от root${NC}"
    exit 1
fi

echo -e "${YELLOW}🔒 Настройка SSL сертификата (Let's Encrypt)${NC}"

# Проверка наличия домена
if [ -z "$1" ]; then
    echo -e "${RED}Использование: $0 <your-domain.com>${NC}"
    echo "Пример: $0 bot.example.com"
    exit 1
fi

DOMAIN=$1

# Установка certbot
echo -e "${YELLOW}📦 Установка certbot...${NC}"
apt update
apt install -y certbot python3-certbot-nginx

# Получение сертификата
echo -e "${YELLOW}🔐 Получение SSL сертификата для $DOMAIN...${NC}"
certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --email admin@$DOMAIN || {
    echo -e "${YELLOW}⚠️  Автоматическая настройка не удалась. Запустите вручную:${NC}"
    echo "   certbot --nginx -d $DOMAIN"
    exit 1
}

# Обновление конфигурации nginx для HTTPS
NGINX_CONF="/etc/nginx/sites-available/avito_autoanswer_bot"
if [ -f "$NGINX_CONF" ]; then
    # Certbot автоматически обновит конфигурацию, но проверим
    nginx -t
    systemctl reload nginx
fi

# Обновление .env файла
PROJECT_DIR="/home/avito_autoanswer_bot"
if [ -f "$PROJECT_DIR/.env" ]; then
    echo -e "${YELLOW}📝 Обновление .env файла...${NC}"
    HTTPS_URL="https://$DOMAIN"
    
    if grep -q "PUBLIC_BASE_URL" "$PROJECT_DIR/.env"; then
        sed -i "s|PUBLIC_BASE_URL=.*|PUBLIC_BASE_URL=$HTTPS_URL|" "$PROJECT_DIR/.env"
    else
        echo "" >> "$PROJECT_DIR/.env"
        echo "PUBLIC_BASE_URL=$HTTPS_URL" >> "$PROJECT_DIR/.env"
    fi
    
    echo -e "${GREEN}✅ PUBLIC_BASE_URL обновлен: $HTTPS_URL${NC}"
    
    # Перезапускаем сервис для применения изменений
    systemctl restart avito_autoanswer_bot.service
fi

# Настройка автообновления сертификата
echo -e "${YELLOW}🔄 Настройка автообновления сертификата...${NC}"
(crontab -l 2>/dev/null | grep -v "certbot renew"; echo "0 3 * * * certbot renew --quiet --post-hook 'systemctl reload nginx'") | crontab -

echo ""
echo -e "${GREEN}✅ SSL сертификат настроен!${NC}"
echo ""
echo "🌐 Ваш webhook URL: https://$DOMAIN/avito/webhook"
echo ""
echo "📋 Обновите подписку на webhook:"
echo "   - Через Telegram бота: /subscribe"
echo "   - Или через CLI: python manage.py subscribe"

