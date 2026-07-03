# Telegram AI Daily Digest

Система для окремого Telegram-акаунта: читає всі чати/канали/групи, зберігає текст у SQLite і раз на день робить AI-summary через Gemini. Готовий звіт приходить у твій BotFather-бот.

## Що потрібно

1. `TELEGRAM_API_ID` і `TELEGRAM_API_HASH` з https://my.telegram.org
2. Номер окремого Telegram-акаунта: `TELEGRAM_PHONE`
3. BotFather bot token: `BOT_TOKEN`
4. Твій private chat id: `OWNER_CHAT_ID`
5. Gemini API key: `GEMINI_API_KEY`

## Швидкий запуск на Mac

```bash
cd telegram-ai-digest
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env
python -m src.main
```

На першому запуску Telethon попросить код входу в Telegram. Код прийде в Telegram на окремий акаунт. Якщо є 2FA пароль — введи його.

Після успішного входу створиться файл сесії:

```text
data/telegram_user.session
```

Не кидай його нікому. Це фактично збережений вхід в Telegram-акаунт.

## Запуск через Docker / VPS

```bash
cd telegram-ai-digest
cp .env.example .env
nano .env
docker compose up -d --build
```

Логи:

```bash
docker logs -f telegram-ai-digest
```

Перший логін у Docker може попросити код у консолі. Якщо незручно, спочатку запусти локально, створи `.session`, потім перенеси папку `data` на VPS.

## Команди в боті

Писати в `@kuro_digest_bot` з основного Telegram:

```text
/start
/status
/summary
/top_chats
/search Indonesia
/ask що писали про Таїланд за останній тиждень?
```

Бот відповідає тільки `OWNER_CHAT_ID`.

## Як воно працює

- окремий Telegram-акаунт читається через Telethon;
- береться тільки текст і captions;
- фото, відео, файли не скачуються;
- SQLite база зберігається в `data/digest.sqlite3`;
- кожні `FETCH_INTERVAL_SECONDS` секунд робиться добір повідомлень за 24 години;
- о `SUMMARY_HOUR:SUMMARY_MINUTE` за `TIMEZONE` приходить денний звіт;
- якщо повідомлень дуже багато, summary робиться частинами, потім фінальний summary.

## Важливо по безпеці

Не публікуй і не кидай нікому:

```text
.env
BOT_TOKEN
TELEGRAM_API_HASH
GEMINI_API_KEY
GROQ_API_KEY
data/telegram_user.session
```

Якщо токен випадково засвітився — перевипусти його в BotFather.

## Налаштування вартості

`GEMINI_MODEL=gemini-2.5-flash` — нормальний стартовий безплатний варіант для тесту. Якщо повідомлень дуже багато, зменш `MAX_INPUT_CHARS`, або підключи більше фільтрів.

Можна підключити кілька Gemini-ключів. Бот пробує їх по черзі, а якщо Gemini вперся в ліміти, переходить на Groq fallback:

```env
GEMINI_API_KEY=...
GEMINI_API_KEY_2=...
GEMINI_API_KEY_3=...
GEMINI_API_KEY_4=...
GEMINI_API_KEY_5=...
GEMINI_MODEL=gemini-2.5-flash

GEMINI_MIN_REQUEST_INTERVAL=30
GEMINI_MAX_RETRIES=5
GEMINI_RETRY_DELAY=60
GEMINI_THINKING_BUDGET=0

GROQ_API_KEY=...
GROQ_MODEL=llama-3.3-70b-versatile
GROQ_FALLBACK_MODEL=llama-3.1-8b-instant
GROQ_MAX_OUTPUT_TOKENS=8192

CHATGPT_MEDIA_BUYING_URL=https://chatgpt.com/
CLAUDE_MEDIA_BUYING_URL=https://claude.ai/
GEMINI_MEDIA_BUYING_URL=https://gemini.google.com/
IGNORED_FOLDERS=Партнери
```

`GEMINI_MIN_REQUEST_INTERVAL=30` не знижує якість відповіді, тільки робить запити повільнішими, щоб free tier рідше ловив 429.
`GEMINI_THINKING_BUDGET=0` для Gemini 2.5 Flash вимикає зайві thinking-токени, щоб короткі відповіді не обрізались і ліміт витрачався економніше.

Gemini-ключі з Google AI Studio можуть мати формат `AIza...` або `AQ...`. Якщо ключ явно неправильного формату, бот пропустить його і піде на Groq fallback, щоб не витрачати час на повторні 400 `API_KEY_INVALID`.

## Автопошук чатів

У меню `⚙️ Налаштування` є `🤖 Автопошук чатів`.

- `▶️ Увімкнути` — бот сам перевіряє нові живі публічні арбітражні/affiliate чати протягом дня.
- `📊 Статус` — показує ліміт, останній запуск і чи ввімкнена автопідписка.
- `🔍 Запустити зараз` — ручна перевірка без очікування фонового запуску.
- `✅ Автопідписка ON/OFF` — окремий перемикач. За замовчуванням вимкнено: бот тільки кидає список, а ти сам підтверджуєш.
- `⚙️ Налаштування автопошуку` — денний ліміт 3/5/10/20 чатів, але при старті бот притискає фоновий автопошук до безпечних 3 чатів/день.

Фоновий автопошук запускається максимум 1 раз на день. Автопідписка вимкнена за замовчуванням. Якщо її увімкнути, бот вступає не більше ніж у 3 чати/день і робить паузу 60 секунд між вступами, щоб зменшити ризик Telegram flood/spam-limit.

## Типові проблеми

### `Missing required env var`

Не заповнив `.env`.

### Бот не відповідає

Перевір:

- ти написав `/start` саме своєму боту;
- `OWNER_CHAT_ID` правильний;
- `BOT_TOKEN` актуальний після перевипуску.

### Нема повідомлень у базі

Перевір:

- окремий Telegram-акаунт реально підписаний на канали/чати;
- перший login через Telethon пройшов;
- у логах нема помилок.

## Оновлення меню / важливості

У цій версії:

- кнопка `❓ Ask` прибрана з меню;
- додана кнопка `⭐ Important` і команда `/important` — витягує тільки топ важливих повідомлень;
- `/search` тепер не просто шукає текст, а оцінює знайдене по промпту важливості 5/5–1/5;
- у summary додаються джерела: назва чату і посилання на повідомлення, якщо Telegram дозволяє зробити link;
- звіти можна робити 2 рази на день через `.env`:

```env
SUMMARY_TIMES=09:00,21:00
```

Для приватних каналів/супергруп link має вигляд `https://t.me/c/...` і відкривається тільки якщо твій акаунт має доступ до цього чату.
