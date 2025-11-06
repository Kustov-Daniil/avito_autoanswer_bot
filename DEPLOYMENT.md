# Инструкция по развертыванию на VDS

## Подготовка сервера

### 1. Подключение к серверу

```bash
ssh root@93.183.91.110
```

### 2. Установка необходимых пакетов

```bash
# Обновление системы
apt update && apt upgrade -y

# Установка Python 3 и pip
apt install -y python3 python3-pip python3-venv git

# Установка дополнительных зависимостей
apt install -y build-essential python3-dev
```

### 3. Создание пользователя и директории (опционально, для безопасности)

```bash
# Создаем пользователя для приложения
useradd -m -s /bin/bash avito_bot

# Создаем директорию для проекта
mkdir -p /home/avito_autoanswer_bot
chown root:root /home/avito_autoanswer_bot
```

### 4. Клонирование репозитория

```bash
cd /home/avito_autoanswer_bot
git clone https://github.com/Kustov-Daniil/avito_autoanswer_bot.git .
```

### 5. Создание виртуального окружения

```bash
cd /home/avito_autoanswer_bot
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 6. Настройка переменных окружения

```bash
cd /home/avito_autoanswer_bot
cp .env.example .env  # Если есть пример
nano .env  # Или используйте ваш любимый редактор
```

Заполните `.env` файл со всеми необходимыми переменными:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_MANAGER_ID` или `MANAGERS`
- `ADMINS`
- `AVITO_CLIENT_ID`
- `AVITO_CLIENT_SECRET`
- `AVITO_ACCOUNT_ID`
- `OPENAI_API_KEY`
- `LLM_MODEL`
- `TEMPERATURE`
- `PUBLIC_BASE_URL`
- `MANAGER_COST_PER_HOUR`
- `USD_RATE`
- `COOLDOWN_MINUTES_AFTER_MANAGER`

### 7. Создание необходимых директорий

```bash
cd /home/avito_autoanswer_bot
mkdir -p data/logs
chmod 755 data
chmod 755 data/logs
```

### 8. Установка systemd service

```bash
# Копируем service файл
cp avito_autoanswer_bot.service /etc/systemd/system/

# Перезагружаем systemd
systemctl daemon-reload

# Включаем автозапуск
systemctl enable avito_autoanswer_bot.service

# Запускаем сервис
systemctl start avito_autoanswer_bot.service

# Проверяем статус
systemctl status avito_autoanswer_bot.service
```

### 9. Настройка webhook (nginx)

Для работы webhook от Avito необходимо настроить nginx для проксирования запросов на Flask приложение.

#### Вариант 1: Автоматическая настройка (рекомендуется)

```bash
cd /home/avito_autoanswer_bot
chmod +x setup_webhook.sh
./setup_webhook.sh
```

Скрипт автоматически:
- Установит nginx
- Создаст конфигурацию
- Настроит firewall
- Обновит PUBLIC_BASE_URL в .env

#### Вариант 2: Ручная настройка

```bash
# Установка nginx
apt install -y nginx

# Копирование конфигурации
cp nginx/avito_autoanswer_bot.conf /etc/nginx/sites-available/avito_autoanswer_bot

# Редактирование конфигурации (замените your-domain.com на ваш домен или IP)
nano /etc/nginx/sites-available/avito_autoanswer_bot

# Активация конфигурации
ln -s /etc/nginx/sites-available/avito_autoanswer_bot /etc/nginx/sites-enabled/
rm /etc/nginx/sites-enabled/default

# Проверка и перезапуск
nginx -t
systemctl restart nginx
systemctl enable nginx
```

#### Настройка SSL (HTTPS) - опционально

Если у вас есть домен, рекомендуется настроить SSL сертификат:

```bash
cd /home/avito_autoanswer_bot
chmod +x setup_ssl.sh
./setup_ssl.sh your-domain.com
```

Или вручную:

```bash
apt install -y certbot python3-certbot-nginx
certbot --nginx -d your-domain.com
```

После настройки SSL обновите PUBLIC_BASE_URL в .env:
```bash
nano /home/avito_autoanswer_bot/.env
# Измените: PUBLIC_BASE_URL=https://your-domain.com
systemctl restart avito_autoanswer_bot.service
```

### 10. Настройка логирования

```bash
# Просмотр логов systemd
journalctl -u avito_autoanswer_bot.service -f

# Просмотр последних 100 строк
journalctl -u avito_autoanswer_bot.service -n 100

# Просмотр логов nginx
tail -f /var/log/nginx/avito_autoanswer_bot_access.log
tail -f /var/log/nginx/avito_autoanswer_bot_error.log

# Просмотр логов приложения
tail -f /home/avito_autoanswer_bot/data/logs/bot.log
```

## Настройка GitHub Actions для автоматического деплоя

### 1. Генерация SSH ключа для деплоя

На вашем локальном компьютере:

```bash
# Генерируем SSH ключ специально для деплоя
ssh-keygen -t ed25519 -C "github-actions-deploy" -f ~/.ssh/github_actions_deploy

# Копируем публичный ключ на сервер
ssh-copy-id -i ~/.ssh/github_actions_deploy.pub root@93.183.91.110
```

### 2. Добавление SSH ключа в GitHub Secrets

1. Откройте репозиторий на GitHub
2. Перейдите в Settings → Secrets and variables → Actions
3. Нажмите "New repository secret"
4. Добавьте секрет:
   - **Name:** `SSH_PRIVATE_KEY`
   - **Value:** Содержимое файла `~/.ssh/github_actions_deploy` (приватный ключ)

```bash
# Показать приватный ключ для копирования
cat ~/.ssh/github_actions_deploy
```

### 3. Проверка GitHub Actions workflow

Убедитесь, что файл `.github/workflows/main.yml` содержит правильные настройки:
- Правильный IP адрес сервера
- Правильный путь к директории проекта
- Правильное имя systemd сервиса

## Автоматический деплой

После настройки, каждый push в ветку `main` будет автоматически:
1. Копировать код на сервер через rsync
2. Перезапускать systemd сервис

### Ручной запуск деплоя

Если нужно запустить деплой вручную:
1. Перейдите в репозиторий на GitHub
2. Откройте вкладку "Actions"
3. Выберите workflow "Deploy avito_autoanswer_bot Telegram Bot"
4. Нажмите "Run workflow"

## Управление сервисом

```bash
# Запуск
systemctl start avito_autoanswer_bot.service

# Остановка
systemctl stop avito_autoanswer_bot.service

# Перезапуск
systemctl restart avito_autoanswer_bot.service

# Статус
systemctl status avito_autoanswer_bot.service

# Просмотр логов
journalctl -u avito_autoanswer_bot.service -f

# Отключить автозапуск
systemctl disable avito_autoanswer_bot.service

# Включить автозапуск
systemctl enable avito_autoanswer_bot.service
```

## Обновление проекта вручную

```bash
cd /home/avito_autoanswer_bot
git pull origin main
source venv/bin/activate
pip install -r requirements.txt
systemctl restart avito_autoanswer_bot.service
```

## Проверка работы

1. Проверьте статус сервиса:
```bash
systemctl status avito_autoanswer_bot.service
```

2. Проверьте логи systemd:
```bash
journalctl -u avito_autoanswer_bot.service -n 50
```

3. Проверьте файл логов приложения:
```bash
tail -f /home/avito_autoanswer_bot/data/logs/bot.log
```

4. Проверьте nginx:
```bash
systemctl status nginx
tail -f /var/log/nginx/avito_autoanswer_bot_access.log
```

5. Проверьте health endpoint:
```bash
curl http://your-domain-or-ip/health
# Должен вернуть: {"status": "ok"}
```

6. Проверьте, что бот отвечает в Telegram

7. Проверьте webhook Avito:
   - Отправьте тестовое сообщение в Avito
   - Проверьте логи nginx и приложения
   - Убедитесь, что webhook получает запросы

## Устранение проблем

### Сервис не запускается

```bash
# Проверьте статус
systemctl status avito_autoanswer_bot.service

# Проверьте логи
journalctl -u avito_autoanswer_bot.service -n 100

# Проверьте синтаксис Python файлов
cd /home/avito_autoanswer_bot
source venv/bin/activate
python3 -m py_compile main.py
```

### Проблемы с правами доступа

```bash
# Убедитесь, что все файлы принадлежат правильному пользователю
chown -R root:root /home/avito_autoanswer_bot

# Убедитесь, что директория data доступна для записи
chmod -R 755 /home/avito_autoanswer_bot/data
```

### Проблемы с виртуальным окружением

```bash
cd /home/avito_autoanswer_bot
rm -rf venv
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### Проблемы с GitHub Actions

1. Проверьте, что SSH ключ добавлен в Secrets
2. Проверьте, что публичный ключ добавлен на сервер
3. Проверьте логи GitHub Actions в разделе "Actions"

## Безопасность

1. **Не храните `.env` файл в репозитории** - добавьте его в `.gitignore`
2. **Используйте отдельного пользователя** для запуска приложения (не root)
3. **Настройте firewall** для ограничения доступа
4. **Регулярно обновляйте систему** и зависимости
5. **Используйте сильные пароли** для SSH

## Мониторинг

Рекомендуется настроить мониторинг:
- Логи через `journalctl`
- Файл логов: `/home/avito_autoanswer_bot/data/logs/bot.log`
- Статус сервиса через `systemctl status`

