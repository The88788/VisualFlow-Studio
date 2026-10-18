# VisualFlow Studio: production-развёртывание

[English](DEPLOYMENT.md) · [Описание проекта](README.ru.md)

Инструкция рассчитана на Ubuntu 24.04 LTS и основной провайдер `gflow`.

## Граница production

```text
Интернет → Nginx :443 → авторизация → 127.0.0.1:4000 → Docker
                                                   ├── SQLite/медиа
                                                   └── закрытый профиль Chrome
```

Не открывайте порт 4000 наружу. Панель управляет платной генерацией и должна
быть защищена авторизацией.

## Пакеты сервера

```bash
apt update
apt install -y docker.io docker-compose-v2 nginx certbot \
  python3-certbot-nginx apache2-utils fail2ban
```

Рекомендуется 4 CPU, 8 ГБ RAM и место под выбранный срок хранения результатов.

## Каталоги

```text
/opt/visualflow/
├── исходный код
├── config/             # закрытый
├── data/               # SQLite и медиа
├── logs/               # закрытый
├── browser_profiles/   # legacy-профили
└── gflow_profiles/     # авторизованные профили Flow
```

```bash
install -d -m 0750 \
  /opt/visualflow/config \
  /opt/visualflow/data \
  /opt/visualflow/logs \
  /opt/visualflow/browser_profiles \
  /opt/visualflow/gflow_profiles
cp config/setting.example.toml config/setting.toml
```

Укажите `provider_mode = "gflow"`, имя профиля и UUID существующего проекта
Flow. Runtime-конфигурацию нельзя коммитить.

## Профиль Flow

В графической сессии выполните:

```bash
gflow auth login --profile client --browser chrome
gflow auth status --profile client
```

Нужно завершить вход именно в приложение Flow. Перед копированием или
монтированием профиля корректно остановите Chrome. Один профиль не должен
одновременно использоваться несколькими процессами Chrome.

Профиль содержит повторно используемую сессию. Ограничьте права, исключите его
из Git и Docker build context, шифруйте резервные копии.

## Сборка и запуск

```bash
docker compose -f docker-compose.prod.yml config
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml up -d
docker compose -f docker-compose.prod.yml ps
curl --fail http://127.0.0.1:4000/health
```

Dockerfile применяет к закреплённой версии gflow-cli патчи с проверкой исходного
кода. Несовпадение должно останавливать сборку — не обходите его без анализа
изменений зависимости.

## Nginx и HTTPS

```bash
htpasswd -cB /etc/nginx/.htpasswd visualflow
chown root:www-data /etc/nginx/.htpasswd
chmod 0640 /etc/nginx/.htpasswd
```

Возьмите `deploy/nginx/myflow.conf.example` за основу. Сохраните:

- upstream `http://127.0.0.1:4000`;
- Basic Auth либо более сильную авторизацию;
- `client_max_body_size 100m`;
- увеличенные тайм-ауты генерации;
- запрет доверия клиентским forwarding-заголовкам.

```bash
nginx -t
systemctl reload nginx
certbot --nginx -d visualflow.example.com
```

## Firewall и Fail2ban

```bash
ufw allow OpenSSH
ufw allow 'Nginx Full'
ufw default deny incoming
ufw default allow outgoing
ufw --force enable
```

Порт 4000 не должен быть открыт наружу. Примеры Fail2ban находятся в
`deploy/fail2ban/`.

## Резервные копии

Нельзя копировать работающую SQLite напрямую в WAL-режиме. Используйте
systemd-таймер и `scripts/backup-myflow.sh` либо online backup API SQLite.
Полная копия включает:

- согласованный snapshot SQLite;
- результаты внутри срока хранения;
- runtime-конфигурацию;
- отдельно сохранённый зашифрованный профиль.

Регулярно проверяйте восстановление. Никогда не публикуйте backup.

## Безопасное обновление

1. Сделайте backup базы и конфигурации.
2. Получите точный commit.
3. Экспортируйте его через `git archive` в новый staging-каталог.
4. Соберите отдельный тестовый образ.
5. Запустите тесты и проверьте патчи совместимости.
6. Сохраните текущий production-образ для rollback.
7. Синхронизируйте код, исключив runtime-каталоги.
8. Пересоберите сервис и дождитесь успешного `/health`.
9. Проверьте панель и одну недорогую реальную генерацию.
10. Удалите staging и rollback только после успеха.

Не переносите в production `.git`, профили, ключи, базы или пользовательские
медиа из рабочей копии.

## Диагностика

| Симптом | Действие |
|---|---|
| Профиль заблокирован | Остановить Chrome этого профиля; удалять только проверенные устаревшие singleton-маркеры |
| Авторизация истекла | Повторить интерактивный вход и безопасно заменить mounted-профиль |
| Guard отклонил промпт/референсы | Оставить задачу failed и изучить технические подробности |
| Медиа создано, но файла нет | Использовать восстановление каталога gflow или browser-download fallback |
| Flow изменил интерфейс | Сравнить новую версию gflow, обновить селекторы/патчи и выполнить smoke test |
| Нет кредитов | Дождаться сброса или использовать отдельно авторизованный аккаунт; не спамить повторами |

## Проверка перед публикацией

- вся история Git проверена на секреты;
- ранее засвеченные данные отозваны;
- отслеживаются только примеры конфигурации;
- нет клиентских медиа, базы, логов, HAR, incident bundles и профилей;
- добавлена MIT и проверены сторонние лицензии;
- перед открытием панели включена авторизация reverse proxy.
