# Настройка Webhook на VDS

## Быстрая настройка

### 1. Автоматическая настройка (рекомендуется)

```bash
cd /home/avito_autoanswer_bot
chmod +x setup_webhook.sh
./setup_webhook.sh
```

Скрипт автоматически:
- ✅ Установит nginx
- ✅ Создаст конфигурацию
- ✅ Настроит firewall
- ✅ Обновит PUBLIC_BASE_URL в .env

**В процессе выполнения:**
- Введите ваш домен (например: `bot.example.com`) или нажмите Enter для использования IP адреса

### 2. Проверка работы

```bash
# Проверка health endpoint
curl http://your-domain-or-ip/health
# Должен вернуть: {"status": "ok"}

# Проверка статуса nginx
systemctl status nginx

# Просмотр логов nginx
tail -f /var/log/nginx/avito_autoanswer_bot_access.log
```

### 3. Подписка на webhook Avito

**Через Telegram бота:**
```
/subscribe
```

**Или через CLI:**
```bash
cd /home/avito_autoanswer_bot
source venv/bin/activate
python manage.py subscribe
```

**Ваш webhook URL:** `http://your-domain-or-ip/avito/webhook`

## Настройка SSL (HTTPS) - опционально

Если у вас есть домен, рекомендуется настроить SSL сертификат:

```bash
cd /home/avito_autoanswer_bot
chmod +x setup_ssl.sh
./setup_ssl.sh your-domain.com
```

После настройки SSL:
1. Обновите PUBLIC_BASE_URL в .env:
```bash
nano /home/avito_autoanswer_bot/.env
# Измените: PUBLIC_BASE_URL=https://your-domain.com
```

2. Перезапустите сервис:
```bash
systemctl restart avito_autoanswer_bot.service
```

3. Обновите подписку на webhook:
```
/subscribe
```

## Ручная настройка

Если автоматическая настройка не подходит, можно настроить вручную:

### 1. Установка nginx

```bash
apt update
apt install -y nginx
```

### 2. Создание конфигурации

```bash
# Копируем шаблон конфигурации
cp nginx/avito_autoanswer_bot.conf /etc/nginx/sites-available/avito_autoanswer_bot

# Редактируем конфигурацию (замените your-domain.com на ваш домен или IP)
nano /etc/nginx/sites-available/avito_autoanswer_bot
```

### 3. Активация конфигурации

```bash
# Создаем символическую ссылку
ln -s /etc/nginx/sites-available/avito_autoanswer_bot /etc/nginx/sites-enabled/

# Удаляем дефолтную конфигурацию
rm /etc/nginx/sites-enabled/default

# Проверяем конфигурацию
nginx -t

# Перезапускаем nginx
systemctl restart nginx
systemctl enable nginx
```

### 4. Настройка firewall

```bash
# Если используется ufw
ufw allow 'Nginx Full'
ufw allow 22/tcp  # SSH
ufw --force enable

# Если используется iptables
iptables -A INPUT -p tcp --dport 80 -j ACCEPT
iptables -A INPUT -p tcp --dport 443 -j ACCEPT
```

### 5. Обновление .env

```bash
nano /home/avito_autoanswer_bot/.env
```

Добавьте или обновите:
```
PUBLIC_BASE_URL=http://your-domain-or-ip
```

Или для HTTPS:
```
PUBLIC_BASE_URL=https://your-domain.com
```

## Проверка работы webhook

### 1. Проверка health endpoint

```bash
curl http://your-domain-or-ip/health
# Должен вернуть: {"status": "ok"}
```

### 2. Проверка webhook endpoint

```bash
curl -X POST http://your-domain-or-ip/avito/webhook \
  -H "Content-Type: application/json" \
  -d '{"test": "data"}'
```

### 3. Просмотр логов

```bash
# Логи nginx
tail -f /var/log/nginx/avito_autoanswer_bot_access.log
tail -f /var/log/nginx/avito_autoanswer_bot_error.log

# Логи приложения
tail -f /home/avito_autoanswer_bot/data/logs/bot.log

# Логи systemd
journalctl -u avito_autoanswer_bot.service -f
```

## Устранение проблем

### nginx не запускается

```bash
# Проверьте конфигурацию
nginx -t

# Проверьте логи
tail -f /var/log/nginx/error.log

# Проверьте, что порт 80 не занят
netstat -tulpn | grep :80
```

### Webhook не получает запросы

1. Проверьте, что nginx запущен:
```bash
systemctl status nginx
```

2. Проверьте, что Flask приложение запущено:
```bash
systemctl status avito_autoanswer_bot.service
```

3. Проверьте firewall:
```bash
ufw status
# или
iptables -L -n
```

4. Проверьте, что PUBLIC_BASE_URL правильно настроен в .env

5. Проверьте логи nginx и приложения

### Ошибка 502 Bad Gateway

Это означает, что nginx не может подключиться к Flask приложению.

1. Проверьте, что Flask приложение запущено на порту 8080:
```bash
netstat -tulpn | grep :8080
```

2. Проверьте, что в конфигурации nginx указан правильный порт:
```bash
grep proxy_pass /etc/nginx/sites-available/avito_autoanswer_bot
# Должно быть: proxy_pass http://127.0.0.1:8080;
```

3. Проверьте логи nginx:
```bash
tail -f /var/log/nginx/avito_autoanswer_bot_error.log
```

### SSL сертификат не работает

1. Проверьте, что домен указывает на ваш IP:
```bash
dig your-domain.com
# или
nslookup your-domain.com
```

2. Проверьте, что порт 443 открыт:
```bash
ufw status
# или
netstat -tulpn | grep :443
```

3. Проверьте конфигурацию nginx:
```bash
nginx -t
```

4. Проверьте сертификат:
```bash
certbot certificates
```

## Миграция с ngrok на VDS

Если вы использовали ngrok локально, вот что нужно изменить:

### Локально (ngrok):
```
PUBLIC_BASE_URL=https://xxxx-xx-xx-xx-xx.ngrok-free.app
```

### На VDS:
```
PUBLIC_BASE_URL=http://your-domain-or-ip
# или для HTTPS:
PUBLIC_BASE_URL=https://your-domain.com
```

### Шаги миграции:

1. Настройте webhook на VDS (см. выше)
2. Обновите PUBLIC_BASE_URL в .env на сервере
3. Перезапустите сервис:
```bash
systemctl restart avito_autoanswer_bot.service
```
4. Обновите подписку на webhook:
```
/subscribe
```

## Безопасность

⚠️ **Важно:**
- Используйте HTTPS для production (настройте SSL)
- Ограничьте доступ к webhook endpoint (можно добавить проверку IP или токена)
- Регулярно обновляйте nginx и систему
- Настройте rate limiting для защиты от DDoS

