# Устранение проблем с GitHub Actions деплоем

## Ошибка: "Permission denied (publickey,password)"

Эта ошибка означает, что SSH ключ не настроен правильно или не добавлен в GitHub Secrets.

### Шаг 1: Проверка SSH ключа локально

Проверьте, что SSH ключ работает локально:

```bash
# Проверьте, что ключ существует
ls -la ~/.ssh/github_actions_deploy*

# Проверьте подключение
ssh -i ~/.ssh/github_actions_deploy root@your-server-ip
```

Если подключение не работает, выполните:

```bash
# Скопируйте публичный ключ на сервер
ssh-copy-id -i ~/.ssh/github_actions_deploy.pub root@your-server-ip

# Проверьте права на ключ
chmod 600 ~/.ssh/github_actions_deploy
chmod 644 ~/.ssh/github_actions_deploy.pub
```

### Шаг 2: Проверка GitHub Secrets

1. Откройте репозиторий на GitHub: `https://github.com/Kustov-Daniil/avito_autoanswer_bot`
2. Перейдите: **Settings** → **Secrets and variables** → **Actions**
3. Проверьте наличие секретов:
   - `SSH_PRIVATE_KEY` - должен быть добавлен
   - `SERVER_HOST` - должен быть добавлен

### Шаг 3: Проверка формата SSH_PRIVATE_KEY

**Важно:** Приватный ключ должен быть скопирован полностью, включая:
- `-----BEGIN OPENSSH PRIVATE KEY-----`
- Все строки между
- `-----END OPENSSH PRIVATE KEY-----`

Проверьте содержимое ключа:

```bash
cat ~/.ssh/github_actions_deploy
```

Убедитесь, что:
- Нет лишних пробелов в начале/конце
- Все строки скопированы
- Включая BEGIN и END строки

### Шаг 4: Пересоздание SSH ключа

Если ключ не работает, пересоздайте его:

```bash
# Удалите старый ключ
rm ~/.ssh/github_actions_deploy ~/.ssh/github_actions_deploy.pub

# Создайте новый ключ БЕЗ пароля
ssh-keygen -t ed25519 -C "github-actions-deploy" -f ~/.ssh/github_actions_deploy
# На оба запроса пароля просто нажмите Enter

# Скопируйте публичный ключ на сервер
ssh-copy-id -i ~/.ssh/github_actions_deploy.pub root@your-server-ip

# Проверьте подключение
ssh -i ~/.ssh/github_actions_deploy root@your-server-ip
```

### Шаг 5: Обновление GitHub Secrets

1. Скопируйте новый приватный ключ:
```bash
cat ~/.ssh/github_actions_deploy
```

2. Обновите секрет `SSH_PRIVATE_KEY` в GitHub:
   - GitHub → Settings → Secrets and variables → Actions
   - Найдите `SSH_PRIVATE_KEY`
   - Нажмите "Update"
   - Вставьте весь ключ (включая BEGIN и END)
   - Сохраните

3. Проверьте секрет `SERVER_HOST`:
   - Должен содержать только IP или домен (без http:// или https://)
   - Например: `93.183.91.110` или `bot.example.com`

### Шаг 6: Проверка на сервере

Проверьте, что публичный ключ добавлен на сервер:

```bash
ssh root@your-server-ip
cat ~/.ssh/authorized_keys | grep github-actions-deploy
```

Если ключа нет, добавьте его вручную:

```bash
# На сервере
echo "ваш_публичный_ключ" >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

### Шаг 7: Проверка прав доступа на сервере

Проверьте права доступа:

```bash
ssh root@your-server-ip
ls -la ~/.ssh/
# Должно быть:
# -rw------- authorized_keys
# drwx------ .ssh
```

Если права неправильные:

```bash
chmod 700 ~/.ssh
chmod 600 ~/.ssh/authorized_keys
```

### Шаг 8: Тестирование деплоя

После исправления всех проблем:

1. Сделайте тестовый коммит:
```bash
git commit --allow-empty -m "Тест деплоя после исправления SSH"
git push
```

2. Проверьте GitHub Actions:
   - GitHub → Actions
   - Дождитесь завершения workflow
   - Проверьте логи

## Ошибка: "rsync: connection unexpectedly closed"

Эта ошибка обычно связана с проблемами SSH подключения.

**Решение:**
1. Выполните все шаги из раздела "Permission denied"
2. Проверьте, что директория существует на сервере:
```bash
ssh root@your-server-ip "ls -la /home/avito_autoanswer_bot"
```

3. Проверьте права доступа:
```bash
ssh root@your-server-ip "chmod -R 755 /home/avito_autoanswer_bot"
```

## Ошибка: "Host key verification failed"

Эта ошибка означает, что сервер не добавлен в known_hosts.

**Решение:** Это должно решаться автоматически через `ssh-keyscan` в workflow. Если ошибка повторяется, проверьте workflow файл - там должна быть строка:
```yaml
ssh-keyscan -H ${{ secrets.SERVER_HOST }} >> ~/.ssh/known_hosts || true
```

## Ошибка: "Connection refused"

**Причина:** Сервер недоступен или SSH порт закрыт.

**Решение:**
1. Проверьте доступность сервера:
```bash
ping your-server-ip
```

2. Проверьте SSH порт:
```bash
telnet your-server-ip 22
```

3. Проверьте SSH сервис на сервере:
```bash
ssh root@your-server-ip "systemctl status ssh"
```

4. Проверьте firewall:
```bash
ssh root@your-server-ip "ufw status"
# Должен быть открыт порт 22
```

## Быстрая диагностика

Выполните эту команду для проверки всех настроек:

```bash
# Проверка ключа
ls -la ~/.ssh/github_actions_deploy*

# Проверка подключения
ssh -i ~/.ssh/github_actions_deploy -v root@your-server-ip

# Проверка содержимого ключа
cat ~/.ssh/github_actions_deploy | head -1
cat ~/.ssh/github_actions_deploy | tail -1
# Должно быть:
# -----BEGIN OPENSSH PRIVATE KEY-----
# -----END OPENSSH PRIVATE KEY-----
```

## Полезные команды для отладки

```bash
# Подробный вывод SSH подключения
ssh -i ~/.ssh/github_actions_deploy -v root@your-server-ip

# Проверка формата ключа
ssh-keygen -l -f ~/.ssh/github_actions_deploy.pub

# Тест rsync локально
rsync -avz --dry-run -e "ssh -i ~/.ssh/github_actions_deploy" ./ root@your-server-ip:/home/avito_autoanswer_bot/
```

