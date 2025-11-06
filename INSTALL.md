# Автоматическая установка проекта

## Быстрая установка (одна команда)

```bash
ssh root@your-server-ip
curl -O https://raw.githubusercontent.com/Kustov-Daniil/avito_autoanswer_bot/main/auto_install.sh && chmod +x auto_install.sh && ./auto_install.sh
```

Или клонируйте репозиторий и запустите скрипт:

```bash
ssh root@your-server-ip
git clone https://github.com/Kustov-Daniil/avito_autoanswer_bot.git /tmp/avito_bot
cd /tmp/avito_bot
chmod +x auto_install.sh
./auto_install.sh
```

## Что делает скрипт автоматической установки

Скрипт `auto_install.sh` полностью автоматизирует процесс установки:

### 1. Обновление системы
- Обновляет пакеты системы
- Устанавливает необходимые зависимости:
  - Python 3.11 и venv
  - pip
  - git
  - nginx
  - certbot (для SSL)
  - rsync
  - curl
  - ufw (firewall)

### 2. Клонирование проекта
- Клонирует репозиторий с GitHub в `/home/avito_autoanswer_bot`
- Или обновляет существующий репозиторий

### 3. Настройка виртуального окружения
- Создает виртуальное окружение Python
- Устанавливает все зависимости из `requirements.txt`

### 4. Проверка .env файла
- Проверяет наличие `.env` файла
- Если файл отсутствует, предупреждает и предлагает создать его вручную
- **Важно:** Вы должны создать `.env` файл вручную перед запуском сервиса

### 5. Настройка systemd сервиса
- Копирует файл `avito_autoanswer_bot.service` в `/etc/systemd/system/`
- Настраивает автозапуск сервиса
- Включает сервис

### 6. Настройка nginx и webhook
- Создает конфигурацию nginx
- Настраивает проксирование на Flask приложение (порт 8080)
- Настраивает health endpoint
- Запрашивает домен или использует IP адрес
- Обновляет `PUBLIC_BASE_URL` в `.env` файле

### 7. Настройка firewall
- Открывает порты для nginx (80, 443)
- Открывает порт SSH (22)
- Включает firewall

### 8. Запуск сервиса
- Запускает systemd сервис
- Проверяет статус запуска

## После установки

### 1. Создание .env файла

**Важно:** Если `.env` файл еще не создан, создайте его вручную:

```bash
nano /home/avito_autoanswer_bot/.env
```

Или скопируйте готовый `.env` файл на сервер:

```bash
# С вашего локального компьютера
scp .env root@your-server-ip:/home/avito_autoanswer_bot/.env
```

Заполните все переменные в `.env` файле:

**Обязательные переменные:**
- `TELEGRAM_BOT_TOKEN` - токен Telegram бота
- `ADMINS` - ID администраторов (через запятую)
- `MANAGERS` - ID менеджеров (через запятую)
- `AVITO_CLIENT_ID` - Client ID из Avito API
- `AVITO_CLIENT_SECRET` - Client Secret из Avito API
- `AVITO_ACCOUNT_ID` - Account ID из Avito API
- `OPENAI_API_KEY` - API ключ OpenAI

**Опциональные переменные:**
- `LLM_MODEL` - модель LLM (по умолчанию: gpt-4o)
- `COOLDOWN_MINUTES_AFTER_MANAGER` - время паузы после ответа менеджера (по умолчанию: 15)
- `MANAGER_COST_PER_HOUR` - стоимость работы менеджера в час (для статистики)
- `USD_RATE` - курс доллара (для статистики)

### 2. Перезапуск сервиса

После редактирования `.env` файла перезапустите сервис:

```bash
systemctl restart avito_autoanswer_bot.service
systemctl status avito_autoanswer_bot.service
```

### 3. Подписка на webhook Avito

Подпишитесь на webhook через Telegram бота:

```
/subscribe
```

Или через CLI:

```bash
cd /home/avito_autoanswer_bot
source venv/bin/activate
python manage.py subscribe
```

### 4. (Опционально) Настройка SSL для HTTPS

Если у вас есть домен, настройте SSL сертификат:

```bash
certbot --nginx -d your-domain.com
```

После настройки SSL обновите `PUBLIC_BASE_URL` в `.env`:

```bash
nano /home/avito_autoanswer_bot/.env
# Измените: PUBLIC_BASE_URL=https://your-domain.com
systemctl restart avito_autoanswer_bot.service
```

## Проверка работы

### 1. Проверка статуса сервиса

```bash
systemctl status avito_autoanswer_bot.service
```

### 2. Проверка health endpoint

```bash
curl http://your-domain-or-ip/health
# Должен вернуть: {"status": "ok"}
```

### 3. Просмотр логов

```bash
# Логи systemd
journalctl -u avito_autoanswer_bot.service -f

# Логи приложения
tail -f /home/avito_autoanswer_bot/data/logs/bot.log

# Логи nginx
tail -f /var/log/nginx/avito_autoanswer_bot_access.log
```

### 4. Проверка webhook

- Отправьте тестовое сообщение в Avito
- Проверьте логи nginx и приложения
- Убедитесь, что webhook получает запросы

## Обновление проекта

Для обновления проекта с GitHub:

```bash
cd /home/avito_autoanswer_bot
git pull
source venv/bin/activate
pip install -r requirements.txt
deactivate
systemctl restart avito_autoanswer_bot.service
```

Или используйте GitHub Actions для автоматического деплоя (см. [GITHUB_ACTIONS_SETUP.md](GITHUB_ACTIONS_SETUP.md))

## Устранение проблем

### Сервис не запускается

```bash
# Проверьте логи
journalctl -u avito_autoanswer_bot.service -n 50

# Проверьте .env файл
cat /home/avito_autoanswer_bot/.env

# Проверьте права доступа
ls -la /home/avito_autoanswer_bot
```

### Webhook не работает

```bash
# Проверьте nginx
systemctl status nginx
nginx -t

# Проверьте health endpoint
curl http://your-domain-or-ip/health

# Проверьте PUBLIC_BASE_URL в .env
grep PUBLIC_BASE_URL /home/avito_autoanswer_bot/.env
```

### Ошибки подключения

```bash
# Проверьте firewall
ufw status

# Проверьте порты
netstat -tulpn | grep :80
netstat -tulpn | grep :8080
```

## Полезные команды

```bash
# Перезапуск сервиса
systemctl restart avito_autoanswer_bot.service

# Остановка сервиса
systemctl stop avito_autoanswer_bot.service

# Просмотр статуса
systemctl status avito_autoanswer_bot.service

# Просмотр логов в реальном времени
journalctl -u avito_autoanswer_bot.service -f

# Перезагрузка nginx
systemctl reload nginx

# Проверка конфигурации nginx
nginx -t
```

