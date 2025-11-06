# Настройка GitHub Actions для автоматического деплоя

## Быстрая настройка

### 1. Генерация SSH ключа

На вашем локальном компьютере выполните:

```bash
# Генерируем SSH ключ для деплоя
ssh-keygen -t ed25519 -C "github-actions-deploy" -f ~/.ssh/github_actions_deploy

# Копируем публичный ключ на сервер
ssh-copy-id -i ~/.ssh/github_actions_deploy.pub root@93.183.91.110
```

### 2. Добавление секретов в GitHub

1. Откройте ваш репозиторий на GitHub
2. Перейдите в **Settings** → **Secrets and variables** → **Actions**
3. Нажмите **"New repository secret"** и добавьте два секрета:

#### Секрет 1: SSH_PRIVATE_KEY
- **Name:** `SSH_PRIVATE_KEY`
- **Value:** Содержимое приватного ключа (скопируйте весь файл):

```bash
cat ~/.ssh/github_actions_deploy
```

Скопируйте весь вывод, включая строки `-----BEGIN OPENSSH PRIVATE KEY-----` и `-----END OPENSSH PRIVATE KEY-----`

#### Секрет 2: SERVER_HOST
- **Name:** `SERVER_HOST`
- **Value:** IP адрес или домен вашего сервера (например: `93.183.91.110`)

### 3. Проверка подключения

Проверьте, что SSH ключ работает:

```bash
ssh -i ~/.ssh/github_actions_deploy root@93.183.91.110
```

Если подключение успешно, выйдите (`exit`) и переходите к следующему шагу.

### 4. Тестирование деплоя

1. Сделайте любой коммит и пуш в ветку `main`:
```bash
git add .
git commit -m "Test deployment"
git push origin main
```

2. Перейдите в репозиторий на GitHub → вкладка **Actions**
3. Дождитесь завершения workflow "Deploy avito_autoanswer_bot Telegram Bot"
4. Проверьте логи - если все зеленое, деплой работает!

### 5. Ручной запуск деплоя

Если нужно запустить деплой вручную:
1. GitHub → **Actions** → **Deploy avito_autoanswer_bot Telegram Bot**
2. Нажмите **"Run workflow"**
3. Выберите ветку `main`
4. Нажмите **"Run workflow"**

## Устранение проблем

### ⚠️ Ошибка "Permission denied (publickey,password)" - ЧАСТАЯ ПРОБЛЕМА

**Причина:** SSH ключ не настроен правильно или не добавлен в GitHub Secrets.

**Быстрое решение:**

1. **Проверьте SSH ключ локально:**
```bash
ssh -i ~/.ssh/github_actions_deploy root@your-server-ip
```

Если не работает, скопируйте ключ на сервер:
```bash
ssh-copy-id -i ~/.ssh/github_actions_deploy.pub root@your-server-ip
chmod 600 ~/.ssh/github_actions_deploy
```

2. **Проверьте GitHub Secrets:**
   - GitHub → Settings → Secrets and variables → Actions
   - Убедитесь, что `SSH_PRIVATE_KEY` добавлен
   - Убедитесь, что `SERVER_HOST` добавлен

3. **Проверьте формат SSH_PRIVATE_KEY:**
   - Должен начинаться с `-----BEGIN OPENSSH PRIVATE KEY-----`
   - Должен заканчиваться `-----END OPENSSH PRIVATE KEY-----`
   - Все строки между должны быть скопированы

4. **Пересоздайте ключ, если нужно:**
```bash
rm ~/.ssh/github_actions_deploy ~/.ssh/github_actions_deploy.pub
ssh-keygen -t ed25519 -C "github-actions-deploy" -f ~/.ssh/github_actions_deploy
# На оба запроса пароля нажмите Enter
ssh-copy-id -i ~/.ssh/github_actions_deploy.pub root@your-server-ip
cat ~/.ssh/github_actions_deploy  # Скопируйте и обновите в GitHub Secrets
```

### Ошибка "Permission denied (publickey)"

1. Убедитесь, что публичный ключ добавлен на сервер:
```bash
ssh-copy-id -i ~/.ssh/github_actions_deploy.pub root@93.183.91.110
```

2. Проверьте права на ключ:
```bash
chmod 600 ~/.ssh/github_actions_deploy
```

3. Убедитесь, что приватный ключ правильно скопирован в GitHub Secrets (включая все строки)

### Ошибка "Host key verification failed"

Это нормально при первом подключении. GitHub Actions автоматически добавит сервер в known_hosts.

### Ошибка "Connection refused"

1. Проверьте, что сервер доступен:
```bash
ping 93.183.91.110
```

2. Проверьте, что SSH порт открыт (обычно 22)

3. Убедитесь, что на сервере запущен SSH сервис:
```bash
ssh root@93.183.91.110 "systemctl status ssh"
```

### Ошибка при выполнении команд на сервере

Проверьте логи GitHub Actions - там будет подробная информация об ошибке.

## Дополнительные проблемы и решения

### Ошибка "rsync: connection unexpectedly closed"

**Причина:** Проблемы с синхронизацией файлов.

**Решение:**
1. Проверьте, что директория существует на сервере:
```bash
ssh root@your-server-ip "ls -la /home/avito_autoanswer_bot"
```

2. Проверьте права доступа:
```bash
ssh root@your-server-ip "chmod -R 755 /home/avito_autoanswer_bot"
```

### Ошибка "Host key verification failed"

**Решение:** Это нормально при первом подключении. GitHub Actions автоматически добавит сервер в known_hosts через `ssh-keyscan` в workflow.

### Ошибка "Connection refused"

**Решение:**
1. Проверьте доступность сервера: `ping your-server-ip`
2. Проверьте SSH порт: `telnet your-server-ip 22`
3. Проверьте SSH сервис: `ssh root@your-server-ip "systemctl status ssh"`
4. Проверьте firewall: `ssh root@your-server-ip "ufw status"`

### Ошибка при выполнении команд на сервере

**Решение:**
1. Проверьте логи GitHub Actions
2. Выполните команды вручную на сервере:
```bash
ssh root@your-server-ip
cd /home/avito_autoanswer_bot
source venv/bin/activate
pip install -r requirements.txt
systemctl restart avito_autoanswer_bot.service
```

## Безопасность

⚠️ **Важно:**
- Никогда не коммитьте приватные SSH ключи в репозиторий
- Используйте отдельный SSH ключ только для деплоя
- Регулярно ротируйте SSH ключи
- Ограничьте доступ SSH ключа только необходимыми командами (можно настроить через `authorized_keys`)

