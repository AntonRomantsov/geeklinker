# GeekLinker bot

Питоновский порт сценария Make.com «GeekLinker» (scenario id `6612176`): мониторинг RSS-фидов
и одной страницы без RSS (через Gemini), запись новых статей в Google Sheets, уведомления в Telegram.

## Логика (1:1 с Make.com)

1. Источники берутся из листа `blog_config` (столбцы A–F: сайт, URL, тип `rss`/`scrape`,
   лист-назначение, служебный столбец, статус). Обрабатываются только строки со статусом `активен`.
   Столбец «движок» (E, в Make — `make`) больше не проверяется — скрипт берёт на себя все активные строки.
2. Дедупликация: ссылка не должна встречаться ни в листе `_Dedup`, ни в целевом листе категории.
3. `rss` → парсинг фида, элементы обрабатываются от старых к новым.
4. `scrape` → GET страницы (с заголовком `Cookie: user_lang_select_force=ru`) → Gemini
   (`extractStructuredData`-эквивалент) извлекает список статей (title/url/pubDate).
5. Новая статья → строка в `_Dedup`, строка в листе категории (A-сайт, B-гиперссылка
   `=HYPERLINK(url; title)`, C-дата в формате `ДД.ММ.ГГГГ`), сообщение в Telegram.
6. Ошибка по одному источнику не останавливает остальные; в конце — сводное сообщение об ошибках
   в Telegram и ненулевой код завершения (для алертов в Cloud Monitoring/Logging).

> Структура листов-категорий изменилась по сравнению с Make.com (там были отдельные колонки
> url/title/date). Формат дедупликации по-прежнему построен на исходном url: если в целевом листе
> ячейка B — не формула `HYPERLINK`, а обычный текст (старые строки, написанные ещё Make.com),
> скрипт считает эту ячейку самой ссылкой — обратная совместимость сохраняется.

## Локальный запуск

Требуется **Python 3.10+** (SDK `google-genai` не ставится на более старый — та же версия,
что и в Cloud Functions runtime `python312`). На этой машине по умолчанию стоит Python 3.8 —
для локального теста понадобится отдельно установленный Python 3.10+.

```bash
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env   # заполнить токены/ключи
gcloud auth application-default login   # для доступа к Google Sheets API
python main.py
```

## Деплой в Google Cloud (Cloud Function + Cloud Scheduler)

Переменные для примера (замените на свои):

```bash
PROJECT_ID=your-gcp-project
REGION=europe-west1
SA_NAME=geeklinker-bot
FUNCTION_NAME=geeklinker-bot
SCHEDULER_JOB=geeklinker-bot-daily
```

### 1. Включить нужные API

```bash
gcloud services enable \
  cloudfunctions.googleapis.com \
  cloudbuild.googleapis.com \
  cloudscheduler.googleapis.com \
  sheets.googleapis.com \
  secretmanager.googleapis.com \
  --project "$PROJECT_ID"
```

### 2. Сервисный аккаунт для функции

```bash
gcloud iam service-accounts create "$SA_NAME" \
  --display-name "GeekLinker bot" --project "$PROJECT_ID"
```

Расшарьте таблицу (`SPREADSHEET_ID`) в Google Sheets на email
`$SA_NAME@$PROJECT_ID.iam.gserviceaccount.com` с правами **Редактор** —
это заменяет OAuth-подключение, которое было у Make.com.

### 3. Секреты (токены/ключи) в Secret Manager

```bash
printf '%s' "<TELEGRAM_BOT_TOKEN>" | gcloud secrets create telegram-bot-token --data-file=- --project "$PROJECT_ID"
printf '%s' "<GEMINI_API_KEY>"    | gcloud secrets create gemini-api-key    --data-file=- --project "$PROJECT_ID"

gcloud secrets add-iam-policy-binding telegram-bot-token \
  --member="serviceAccount:$SA_NAME@$PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor" --project "$PROJECT_ID"
gcloud secrets add-iam-policy-binding gemini-api-key \
  --member="serviceAccount:$SA_NAME@$PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor" --project "$PROJECT_ID"
```

### 4. Деплой функции (2nd gen, HTTP-триггер)

```bash
gcloud functions deploy "$FUNCTION_NAME" \
  --project "$PROJECT_ID" --region "$REGION" \
  --gen2 --runtime python312 --entry-point run \
  --trigger-http --no-allow-unauthenticated \
  --service-account "$SA_NAME@$PROJECT_ID.iam.gserviceaccount.com" \
  --set-env-vars SPREADSHEET_ID=1wSKEh8clYuF4ekTMjv1w8L5l9pt8Adpzj0xlmuiEKWQ,TELEGRAM_CHAT_ID=254603322,GEMINI_MODEL=gemini-3.1-flash-lite \
  --set-secrets TELEGRAM_BOT_TOKEN=telegram-bot-token:latest,GEMINI_API_KEY=gemini-api-key:latest
```

### 5. Расписание — Cloud Scheduler (не реже раза в сутки)

```bash
FUNCTION_URL=$(gcloud functions describe "$FUNCTION_NAME" \
  --project "$PROJECT_ID" --region "$REGION" --gen2 --format='value(serviceConfig.uri)')

gcloud scheduler jobs create http "$SCHEDULER_JOB" \
  --project "$PROJECT_ID" --location "$REGION" \
  --schedule "0 21 * * *" --time-zone "Europe/Kyiv" \
  --uri "$FUNCTION_URL" --http-method POST \
  --oidc-service-account-email "$SA_NAME@$PROJECT_ID.iam.gserviceaccount.com"
```

Соответствует прежнему окну запуска в Make.com (21:00).

### Ручной запуск вне расписания

```bash
gcloud scheduler jobs run "$SCHEDULER_JOB" --project "$PROJECT_ID" --location "$REGION"
```

### Приостановить/возобновить расписание

```bash
gcloud scheduler jobs pause  "$SCHEDULER_JOB" --project "$PROJECT_ID" --location "$REGION"
gcloud scheduler jobs resume "$SCHEDULER_JOB" --project "$PROJECT_ID" --location "$REGION"
```

### Сигнализирование об ошибках

- Скрипт сам шлёт сводку по упавшим источникам в Telegram (`TELEGRAM_ALERT_CHAT_ID`, если не задан — используется `TELEGRAM_CHAT_ID`) в конце каждого запуска.
- При наличии ошибок функция завершается с ошибкой (ненулевой статус) — это попадает в Cloud Logging.
  Дополнительно можно настроить log-based alert в Cloud Monitoring на события уровня `ERROR` по функции `$FUNCTION_NAME`, чтобы получать оповещение и на email/в другой канал.

## После проверки

Сценарий `6612176` в Make.com можно отключить/удалить — весь функционал перенесён.
