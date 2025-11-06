# Быстрый старт развертывания

## 🚀 Автоматическая установка (одна команда) - РЕКОМЕНДУЕТСЯ

Самый простой способ - использовать автоматический скрипт установки:

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

Скрипт автоматически:
- ✅ Установит все зависимости (Python, nginx, certbot и т.д.)
- ✅ Клонирует проект с GitHub
- ✅ Настроит виртуальное окружение
- ✅ Создаст шаблон `.env` файла
- ✅ Настроит systemd сервис
- ✅ Настроит nginx и webhook
- ✅ Настроит firewall
- ✅ Запустит сервис

**После установки:**
1. Создайте или скопируйте `.env` файл в `/home/avito_autoanswer_bot/.env`
   ```bash
   # С вашего локального компьютера
   scp .env root@your-server-ip:/home/avito_autoanswer_bot/.env
   
   # Или создайте вручную на сервере
   nano /home/avito_autoanswer_bot/.env
   ```
2. Заполните все переменные (токены, ключи, ID)
3. Перезапустите сервис: `systemctl restart avito_autoanswer_bot.service`
4. Подпишитесь на webhook: `/subscribe` (через Telegram бота)

---

## Ручная установка (альтернатива)

Если хотите установить вручную:

## Шаг 1: Подготовка сервера (5 минут)

Подключитесь к серверу и выполните:

```bash
ssh root@your-server-ip

# Скачайте и запустите скрипт развертывания
cd /home
git clone https://github.com/Kustov-Daniil/avito_autoanswer_bot.git avito_autoanswer_bot
cd avito_autoanswer_bot
chmod +x deploy.sh
./deploy.sh
```

Скрипт автоматически:
- ✅ Установит все необходимые пакеты
- ✅ Создаст виртуальное окружение
- ✅ Установит зависимости
- ✅ Настроит systemd сервис

## Шаг 2: Настройка .env файла (2 минуты)

```bash
cd /home/avito_autoanswer_bot
nano .env
```

Заполните все переменные (см. пример в `config.py` или `DEPLOYMENT.md`)

## Шаг 3: Настройка webhook (nginx)

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

**Важно:** Введите ваш домен (если есть) или нажмите Enter для использования IP адреса.

## Шаг 4: Запуск сервиса

```bash
systemctl start avito_autoanswer_bot.service
systemctl status avito_autoanswer_bot.service
```

## Шаг 5: Подписка на webhook Avito

После настройки webhook подпишитесь на webhook через Telegram бота:

```
/subscribe
```

Или через CLI:
```bash
cd /home/avito_autoanswer_bot
source venv/bin/activate
python manage.py subscribe
```

**Проверка webhook:**
```bash
# Проверка health endpoint
curl http://your-domain-or-ip/health
# Должен вернуть: {"status": "ok"}
```

## Шаг 6: (Опционально) Настройка SSL для HTTPS

Если у вас есть домен, настройте SSL сертификат:

```bash
cd /home/avito_autoanswer_bot
chmod +x setup_ssl.sh
./setup_ssl.sh your-domain.com
```

После настройки SSL обновите PUBLIC_BASE_URL в .env на https://your-domain.com

## Шаг 7: Настройка GitHub Actions (10 минут)

### 7.1. Генерация SSH ключа

На вашем **локальном компьютере**:

```bash
ssh-keygen -t ed25519 -C "github-actions-deploy" -f ~/.ssh/github_actions_deploy
ssh-copy-id -i ~/.ssh/github_actions_deploy.pub root@93.183.91.110
```

### 7.2. Добавление секретов в GitHub

1. GitHub → Ваш репозиторий → **Settings** → **Secrets and variables** → **Actions**
2. Добавьте два секрета:

   **SSH_PRIVATE_KEY:**
   ```bash
   cat ~/.ssh/github_actions_deploy
   ```
   Скопируйте весь вывод (включая `-----BEGIN...` и `-----END...`)

   **SERVER_HOST:**
   ```
   93.183.91.110
   ```

### 7.3. Проверка

Сделайте любой коммит:
```bash
git add .
git commit -m "Setup deployment"
git push origin main
```

Проверьте GitHub → **Actions** - должен запуститься workflow деплоя.

## Готово! 🎉

Теперь каждый push в `main` будет автоматически деплоить изменения на сервер.

## Полезные команды

```bash
# Статус сервиса
systemctl status avito_autoanswer_bot.service

# Логи
journalctl -u avito_autoanswer_bot.service -f

# Перезапуск
systemctl restart avito_autoanswer_bot.service

# Логи приложения
tail -f /home/avito_autoanswer_bot/data/logs/bot.log
```

## Подробные инструкции

- **[DEPLOYMENT.md](DEPLOYMENT.md)** - Полная инструкция по развертыванию
- **[GITHUB_ACTIONS_SETUP.md](GITHUB_ACTIONS_SETUP.md)** - Детальная настройка GitHub Actions

