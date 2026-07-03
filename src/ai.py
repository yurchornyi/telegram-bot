from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time

import httpx

logger = logging.getLogger("ai_service")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

TELEGRAM_LIMIT = 4096
TRUNCATED_WARNING = "⚠️ Відповідь обірвана через ліміт довжини. Частину даних може бути не показано."


class GeminiRateLimitError(RuntimeError):
    pass


class GroqRateLimitError(RuntimeError):
    pass


class AIProviderUnavailableError(RuntimeError):
    pass


MEMORY_EXTRACT_SYSTEM = """Ти витягуєш довгострокову пам'ять про користувача для Telegram-бота медіабаїнгу/арбітражу.

Завдання: з одного повідомлення користувача знайти тільки стабільні факти, які варто пам'ятати в майбутніх відповідях.

Записуй тільки якщо це явно стосується користувача, його роботи, критеріїв, стилю, обмежень або вподобань.
Не записуй одноразові питання, тимчасові команди, технічні баги, випадкові згадки, цитати з чужих повідомлень.

Дозволені ключі:
- role
- verticals
- geo
- partners
- traffic_sources
- budget
- priorities
- excludes
- style
- job_criteria
- workflow

Відповідай тільки JSON object. Значення - короткі рядки українською.
Якщо нічого важливого немає, поверни {}.
"""

SUMMARY_SYSTEM = """Ти робиш короткий практичний дайджест Telegram-повідомлень для медіабаєра.

Головне: не великий звіт, а зрозуміла вижимка.

Показуй тільки те, що реально може вплинути на роботу:
- офер/GEO/цифри: CPA, AR, CR, EPC, payout, approve, price;
- зміна умов офера, ленд/преленд, клоака;
- проблеми Facebook/Google/TikTok Ads, акаунти, БМ, ФП, оплата, бан;
- конкретна дія на сьогодні-завтра.

Не показуй:
- вакансії/hiring/job/media buyer/team lead/bizdev;
- рекламу сервісів, баз, курсів, кол-центрів, VoIP;
- загальні новини без конкретної дії;
- побутові продажі/покупки, будматеріали, гараж, нерухомість, особисті оголошення;
- повідомлення про підписку на канал, правила доступу, капчу, обмеження участі в чаті;
- флуд, питання без відповіді, "дякую", "хто знає", меми;
- службові повідомлення модерації чату.

Жорсткі правила:
- максимум 6 пунктів у всьому дайджесті;
- один пункт = 1-2 рядки;
- не вигадуй офери, GEO, цифри або висновки;
- якщо цифр немає, не пиши CPA/AR/CR;
- не дублюй одне й те саме в різних секціях;
- кожен пункт має мати `Джерело: ...`;
- `Джерело` бери тільки з `link=` у вхідному повідомленні;
- якщо `link=посилання недоступне`, пиши `Джерело: без посилання`;
- зовнішні URL з тексту можна згадати в самому пункті, але не замість `Джерело`;
- не пиши `link=...`, пиши тільки `Джерело: ...`;
- пиши українською, без таблиць і без довгих пояснень.
"""

SUMMARY_PROMPT = """Зроби короткий дайджест по повідомленнях.

Формат:
📌 Звіт

1. Що: ...
   Чому важливо: ...
   Джерело: ...

2. Що: ...
   Чому важливо: ...
   Джерело: ...

✅ Що робити:
- ...
- ...
- ...

Правила:
- максимум 6 пунктів;
- якщо реально важливого немає, напиши: "Суттєвих сигналів немає.";
- не додавай секції 5/5, 4/5, 3/5;
- не додавай "Офери/GEO", "Перспективно", "Що ігнорувати";
- не показуй вакансії;
- не вигадуй дані.

Повідомлення:
{messages}
"""

MERGE_SYSTEM = """Зведи часткові дайджести в один короткий фінальний дайджест.

Правила:
- максимум 6 пунктів усього;
- прибери дублікати;
- не показуй вакансії;
- не додавай оцінки 5/5, 4/5, 3/5;
- не додавай зайві секції;
- кожен пункт має мати `Джерело: ...`;
- якщо джерела немає, пиши `Джерело: без посилання`;
- не вигадуй цифри або факти;
- в кінці дай 2-3 короткі дії.
"""

ASK_SYSTEM = """Ти відповідаєш по базі повідомлень Telegram для медіабаєра. Відповідай українською. Якщо в повідомленнях нема відповіді — прямо скажи, що нема даних. Не вигадуй. Додавай джерела/посилання, якщо вони є.

Якщо тобі надіслали тільки ЧАСТИНУ бази повідомлень (одна частина з кількох) — відповідай тільки по тому, що бачиш у цій частині, і не стверджуй, що це вся база."""

ASK_MERGE_SYSTEM = """Ти зводиш кілька часткових відповідей на одне питання (кожна відповідь базувалась на своїй частині бази повідомлень) в одну фінальну відповідь.
Об'єднай факти, прибери повтори і суперечності (якщо є суперечність — зазнач обидва варіанти і вкажи джерела).
Не вигадуй нічого нового понад те, що було в часткових відповідях.
Пиши українською, коротко і по суті."""

SEARCH_SYSTEM = """РОЛЬ:
Ти аналізуєш результати пошуку по Telegram-базі партнерів медіабаїнгу. Твоя задача — не просто знайти слово, а відсортувати знайдені повідомлення за реальною цінністю для медіабаєра.

Поле `Джерело` можна брати ТІЛЬКИ з поля `link=` у вхідному повідомленні.
Якщо там `посилання недоступне`, пиши `Джерело: без посилання`.
Зовнішні URL з самого тексту повідомлення можна згадувати окремо, але не замість `Джерело`.

Критерії:
5/5 — офер + GEO + конкретні цифри: AR / CR / EPC / payout / CPA / approve / price, або пряма зміна умов від партнера: новий ленд/преленд, зміна апруву, офер закрили/відкрили.
4/5 — конкретне GEO або вертикаль без повних цифр; проблема з Facebook Ads, оплатою, картою; акаунти/бан/ФП; клоака/ленд/преленд; є дія на сьогодні-завтра.
3/5 — загальна порада, обговорення без цифр, думка без конкретної дії, але тільки якщо є практична користь.
2/5 — загальні розмови, повтори, мотивація, думки без конкретики.
1/5 — сміття: привітання, меми, флуд, реклама без цифр, не по темі.

Не пиши "топ" або "перспективно", якщо немає мінімум 2 з 4: GEO, офер/вертикаль, цифри, конкретна дія/факт.

Вакансії показуй тільки якщо є мінімум 3 з 4: посада/вертикаль/GEO, реальна ЗП, умови, контакт. Відсіюй HYIP/MLM/легкі гроші.

Не обмежуй кількість важливих результатів:
- показуй усі 5/5;
- показуй усі 4/5;
- 3/5 показуй тільки якщо є практична користь;
- 2/5 і 1/5 не показуй.

Якщо тобі надіслали тільки ЧАСТИНУ знайдених результатів (одна частина з кількох) — обробляй тільки те, що бачиш, не вигадуй, що це всі результати.

Пиши коротко українською без Markdown-сміття.
"""

SEARCH_PROMPT = """Запит користувача: {query}

Оціни знайдені повідомлення по критеріях 5/5–1/5.
Не добивай відповідь слабкими результатами. Якщо знайдене слабке — прямо скажи.

Формат:
🔎 Пошук: {query}

🔥 Критично важливе 5/5:
- покажи всі 5/5, якщо вони є.

⭐ Важливе 4/5:
- покажи всі 4/5, якщо вони є.

🟡 Корисне 3/5:
- показуй тільки якщо є практична користь.

💰 Офери / GEO / цифри:
- тільки конкретика. Якщо цифр нема — так і пиши.

💼 Вакансії:
- тільки якщо проходять критерії. Якщо нема — "Якісних вакансій не знайдено".

🗑️ Що не варте уваги:
- коротко, які знайдені повідомлення слабкі/сміттєві.

✅ Що робити:
- 1–3 конкретні дії, якщо є підстава.

Для кожного пункту:
Оцінка: X/5
Чат: назва чату
Джерело: тільки Telegram-link з поля `link=` або "без посилання"
Що було: коротко
Чому важливо: 1 речення
Що зробити: конкретна дія, якщо вона є

Повідомлення:
{messages}
"""

SEARCH_MERGE_SYSTEM = """Ти зводиш кілька часткових звітів пошуку по Telegram-базі в один фінальний звіт.
Збережи всі 5/5 і всі 4/5 з усіх часткових звітів. 3/5 залиш тільки якщо є практична користь.
Прибери дублікати (однакове повідомлення з однаковим джерелом). Не скорочуй штучно, якщо результатів багато.
Збережи структуру кожного пункту: Оцінка / Чат / Джерело / Що було / Чому важливо / Що зробити.
Пиши українською, без Markdown-сміття."""

IMPORTANT_SYSTEM = """Ти робиш короткий дайджест важливих Telegram-повідомлень для медіабаєра.
Показуй ТІЛЬКИ 5/5 і 4/5.
Не показуй 3/5, 2/5, 1/5.

НЕ ПОКАЗУЙ у цьому дайджесті:
- вакансії / hiring / job / шукаємо media buyer / team lead / designer / farmer;
- продаж баз даних, лідів, акаунтів, VoIP, кол-центрів, автодозвону, розсилок;
- рекламу сервісів, підрядів, агентств, навчання, курсів;
- загальні новини Google/Meta/TikTok без прямої дії для запуску кампаній сьогодні;
- службові повідомлення модерації Telegram-чату: користувача обмежили/замʼютили/забанили/розбанили/видалили;
- будь-які повідомлення, які краще віднести в розділ "Вакансії".

5/5: офер + GEO + цифри або конкретна зміна умов: новий ленд/преленд, зміна апруву, офер закрили/відкрили.
4/5: конкретне GEO/вертикаль без повних цифр; Facebook Ads/оплата/карта; акаунти/бан/ФП; клоака/ленд/преленд; дія на сьогодні-завтра.

Пиши українською. Не вигадуй. Якщо немає 5/5 або 4/5 — напиши рівно: "Немає важливих 5/5 або 4/5 повідомлень."

Формат короткий:
Оцінка: X/5
Що сталося: ...
GEO/офер/цифри: ...
Чат: ...
Посилання: ...
"""

EVALUATE_CHAT_SYSTEM = """Ти оцінюєш, чи варто медіабаєру стежити за публічним Telegram-чатом.

Оціни останні повідомлення чату за користю:
✅ Варто стежити — якщо є хоча б одне 5/5 або кілька 4/5.
⚠️ Можна перевірити — якщо є корисні обговорення, але мало цифр.
❌ Не варто — якщо флуд, реклама, скам, немає конкретики.

Критерії 5/5 і 4/5 такі самі: офер, GEO, цифри, апрув, payout/CPA, ленди/преленди, Facebook Ads, акаунти, бан, оплата, конкретні дії.
Вакансії враховуй тільки якщо це реальна вакансія з посадою/умовами/ЗП/контактом, а не HYIP/MLM/легкі гроші.

Відповідай українською коротко:
Вердикт: ✅ Варто стежити / ⚠️ Можна перевірити / ❌ Не варто
Чому: 1-2 речення
Сигнали: короткий список конкретики
"""

QUICK_ALERT_SYSTEM = """Ти перевіряєш одне Telegram-повідомлення для миттєвого алерта медіабаєру.

Поверни алерт ТІЛЬКИ якщо повідомлення має рівень 5/5:
- є офер + GEO + конкретні цифри: AR / CR / EPC / payout / CPA / approve / price;
- або є конкретна зміна умов: новий ленд/преленд, зміна апруву, офер закрили/відкрили.

НЕ роби цей алерт по вакансіях, hiring, пошуку media buyer / affiliate manager / bizdev / team lead. Для цього є окремий модуль "💼 Вакансії".

НЕ роби алерт по службових діях модерації Telegram-чату:
- користувача обмежили / замʼютили / забанили / розбанили / видалили;
- адмін змінив статус користувача;
- це не зміна умов офера, навіть якщо в тексті є слова approve/апрув/ban.

Якщо це не 5/5 — поверни рівно: NO_ALERT

Якщо це 5/5 — формат:
🔥 Нове 5/5 повідомлення

Що: ...
Чат: ...
Посилання: ...
Чому важливо: ...
"""

JOB_ALERT_SYSTEM = """Ти перевіряєш одне Telegram-повідомлення на релевантну вакансію для користувача.

Дані користувача/критерії будуть у запиті. Враховуй їх як головний фільтр.

ЖОРСТКЕ ПРАВИЛО ПРО ВЕРТИКАЛІ:
- Якщо в даних користувача вказані конкретні вертикалі, вакансія має підходити саме під них.
- Не пропонуй "розглядаємо з інших вертикалей", якщо користувач цього не просив.
- Якщо вакансія по gambling/betting/dating/crypto/finance/ecom, а в профілі користувача вказана тільки nutra/health або інша конкретна вертикаль — поверни рівно NO_JOB.
- Якщо вертикалі в профілі не вказані або написано "розглядаю інші" — тоді можна оцінювати ширше.

Показуй вакансію тільки якщо це реальна робота/позиція:
- є посада або роль;
- є умови/формат/обов'язки або стек;
- бажано є зарплата/вилка/ставка/%;
- є контакт або спосіб відгуку.

Відсіюй і поверни рівно NO_JOB, якщо:
- це скам/MLM/HYIP/пасивний дохід/легкі гроші;
- немає конкретики;
- вакансія явно не підходить під профіль користувача;
- це просто реклама курсу/каналу/команди без умов.

Якщо вакансія підходить, формат:
💼 Вакансія під твої критерії

Посада: ...
Чому підходить: ...
Умови: ...
Контакт: ...
Чат: ...
Посилання: ...

Відповідь має бути короткою, максимум 900 символів. Не додавай попередження про обрізання.
"""


def contains_important_report(text: str) -> bool:
    lowered = (text or "").lower()
    negative_markers = [
        "немає важливих 5/5 або 4/5",
        "критичних 5/5 повідомлень не було",
        "важливих 4/5 повідомлень не було",
        "за цей період повідомлень не знайдено",
    ]
    if any(marker in lowered for marker in negative_markers) and "оцінка: 5/5" not in lowered and "оцінка: 4/5" not in lowered:
        return False
    return "оцінка: 5/5" in lowered or "оцінка: 4/5" in lowered or "5/5" in lowered or "4/5" in lowered


def _format_one_line(m: dict) -> str:
    link = m.get("message_link") or "посилання недоступне"
    text = (m.get("text") or "").strip()
    return (
        f"[{m.get('date')}] "
        f"chat={m.get('chat_title') or m.get('chat_id')} | "
        f"sender={m.get('sender_name') or ''} | "
        f"link={link} | "
        f"text={text}"
    )


def _is_obvious_noise(text: str) -> bool:
    normalized = " ".join((text or "").strip().lower().split())
    if not normalized:
        return True
    if normalized in {
        "ок", "ok", "дякую", "спасибо", "thanks", "thank you", "привіт",
        "hello", "hi", "+", "++", "👍", "🔥", "?", "??", "???",
    }:
        return True
    return len(normalized) <= 2


def _describe_chunk(chunk: list[dict]) -> str:
    """
    Короткий опис чанку: дати і чати, які в нього потрапили.
    Використовується і в логах, і в самому промпті для моделі,
    щоб було видно, до якого шматка даних відноситься відповідь
    (а не просто "частина 2 з 5" без жодного контексту).
    """
    if not chunk:
        return "порожній чанк"

    dates = [str(m.get("date")) for m in chunk if m.get("date")]
    chats = sorted({str(m.get("chat_title") or m.get("chat_id")) for m in chunk})

    date_range = f"{dates[0]} — {dates[-1]}" if dates else "дати невідомі"
    chats_preview = ", ".join(chats[:5]) + (f" і ще {len(chats) - 5}" if len(chats) > 5 else "")

    return f"{len(chunk)} повідомлень, дати: {date_range}, чати: {chats_preview}"


def chunk_messages(messages: list[dict], max_chars: int) -> list[list[dict]]:
    """
    Ділить повідомлення на чанки, рахуючи довжину ТОЧНО за тим форматом,
    який реально піде в модель (_format_one_line). Це єдине джерело правди
    про довжину рядка — раніше довжина рахувалась по одній формулі,
    а форматування в запит йшло по іншій, через що частина повідомлень
    мовчки відкидалась всередині format_messages.

    Жодне повідомлення з текстом не губиться: якщо один рядок сам по собі
    довший за max_chars, він все одно потрапляє в окремий чанк
    (а не відкидається).
    """
    chunks: list[list[dict]] = []
    current: list[dict] = []
    current_chars = 0

    for m in messages:
        text = (m.get("text") or "").strip()
        if _is_obvious_noise(text):
            continue

        line = _format_one_line(m)
        line_len = len(line) + 1

        if current and current_chars + line_len > max_chars:
            chunks.append(current)
            current = []
            current_chars = 0

        current.append(m)
        current_chars += line_len

    if current:
        chunks.append(current)

    return chunks


def format_messages(messages: list[dict]) -> str:
    """
    Форматує вже готовий (нарізаний по чанках) список повідомлень.
    Більше не ріже по max_chars сам — нарізка відбувається заздалегідь
    в chunk_messages, тут ми тільки серіалізуємо. Це прибирає ризик
    подвійного, неконсистентного обрізання.
    """
    parts = []
    for m in messages:
        text = (m.get("text") or "").strip()
        if _is_obvious_noise(text):
            continue
        parts.append(_format_one_line(m))
    return "\n".join(parts)


SOURCE_LINE_RE = re.compile(r"^(?P<label>\s*(?:Джерело|Посилання)\s*:\s*)(?P<value>.*)$", re.IGNORECASE)
INLINE_SOURCE_RE = re.compile(r"(?P<label>\b(?:Джерело|Посилання)\s*:\s*)(?P<value>.*)$", re.IGNORECASE)
TELEGRAM_URL_RE = re.compile(r"https?://(?:t\.me|telegram\.me|telegram\.dog)/[^\s)>\]]+", re.IGNORECASE)
WORD_RE = re.compile(r"[a-zа-яіїєґ0-9]{3,}", re.IGNORECASE)

SOURCE_STOPWORDS = {
    "що", "чому", "важливо", "джерело", "без", "посилання", "звіт", "робити",
    "для", "про", "это", "это", "как", "the", "and", "with", "буде", "може",
    "может", "вплинути", "работы", "роботи", "інструмент", "инструмент",
}


def clean_report_links(text: str) -> str:
    """
    Keep external URLs in the report body, but make source lines point only
    to the original Telegram message link.
    """
    cleaned_lines = []
    for line in (text or "").splitlines():
        match = SOURCE_LINE_RE.match(line)
        if not match:
            inline = INLINE_SOURCE_RE.search(line)
            if not inline:
                cleaned_lines.append(line)
                continue
            telegram_link = TELEGRAM_URL_RE.search(inline.group("value").strip())
            replacement = f"{inline.group('label')}{telegram_link.group(0) if telegram_link else 'без посилання'}"
            cleaned_lines.append(line[:inline.start()] + replacement)
            continue
        value = match.group("value").strip()
        telegram_link = TELEGRAM_URL_RE.search(value)
        if telegram_link:
            cleaned_lines.append(f"{match.group('label')}{telegram_link.group(0)}")
        else:
            cleaned_lines.append(f"{match.group('label')}без посилання")
    return "\n".join(cleaned_lines)


def _source_tokens(text: str) -> set[str]:
    return {
        token.casefold()
        for token in WORD_RE.findall(text or "")
        if token.casefold() not in SOURCE_STOPWORDS
    }


def fill_missing_report_sources(report: str, messages: list[dict]) -> str:
    source_items = []
    for msg in messages:
        link = msg.get("message_link")
        if not link:
            continue
        tokens = _source_tokens(msg.get("text") or "")
        if tokens:
            source_items.append((tokens, link))

    if not source_items:
        return report

    lines = (report or "").splitlines()
    blocks: list[tuple[int, int]] = []
    current_start = None
    for i, line in enumerate(lines):
        if re.match(r"^\s*\d+\.\s+", line):
            if current_start is not None:
                blocks.append((current_start, i))
            current_start = i
    if current_start is not None:
        blocks.append((current_start, len(lines)))

    for start, end in reversed(blocks):
        block_text = "\n".join(lines[start:end])
        if TELEGRAM_URL_RE.search(block_text):
            continue
        block_tokens = _source_tokens(block_text)
        if not block_tokens:
            continue
        best_link = None
        best_score = 0
        for tokens, link in source_items:
            score = len(block_tokens & tokens)
            if score > best_score:
                best_score = score
                best_link = link
        if not best_link or best_score < 2:
            continue

        replaced = False
        for i in range(start, end):
            if SOURCE_LINE_RE.match(lines[i]) or INLINE_SOURCE_RE.search(lines[i]):
                lines[i] = SOURCE_LINE_RE.sub(lambda m: f"{m.group('label')}{best_link}", lines[i])
                inline = INLINE_SOURCE_RE.search(lines[i])
                if inline:
                    lines[i] = lines[i][:inline.start()] + f"{inline.group('label')}{best_link}"
                replaced = True
                break
        if not replaced:
            lines.insert(end, f"   Джерело: {best_link}")

    return "\n".join(lines)


def split_for_telegram(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """
    Ріже фінальний текст на частини, які влізуть у одне Telegram-повідомлення
    (ліміт 4096 символів). Ріже по межах рядків, щоб не розривати пункт
    звіту посередині (Оцінка/Чат/Джерело/Що було/...) там, де це можливо.

    Бот має відправляти кожен елемент цього списку окремим повідомленням,
    одне за одним.
    """
    if len(text) <= limit:
        return [text]

    parts: list[str] = []
    lines = text.split("\n")
    current = ""

    for line in lines:
        # один рядок сам по собі довший за ліміт - ріжемо його жорстко
        if len(line) > limit:
            if current:
                parts.append(current)
                current = ""
            for i in range(0, len(line), limit):
                parts.append(line[i:i + limit])
            continue

        candidate = f"{current}\n{line}" if current else line

        if len(candidate) > limit:
            parts.append(current)
            current = line
        else:
            current = candidate

    if current:
        parts.append(current)

    return parts


VERTICAL_KEYWORDS = {
    "nutra": {"nutra", "нутра", "health", "здоров", "суглоб", "joints", "parasite", "паразит", "diabetes", "діабет", "диабет", "weight loss", "похуд", "схуд"},
    "gambling": {"gambling", "гембл", "казино", "casino", "betting", "бетт", "igaming", "sportsbook"},
    "dating": {"dating", "дейтинг", "adult", "адалт", "cams"},
    "crypto": {"crypto", "крипт", "forex", "форекс", "finance", "финанс", "фінанс"},
    "ecom": {"ecom", "ecommerce", "еcom", "cod", "товарк", "leadgen", "lead generation"},
}


def _mentioned_verticals(text: str) -> set[str]:
    lowered = (text or "").casefold()
    found = set()
    for vertical, keywords in VERTICAL_KEYWORDS.items():
        if any(keyword.casefold() in lowered for keyword in keywords):
            found.add(vertical)
    return found


def _job_profile_allows_message(profile: str, message_text: str) -> bool:
    profile_verticals = _mentioned_verticals(profile)
    message_verticals = _mentioned_verticals(message_text)
    if not profile_verticals or not message_verticals:
        return True
    return bool(profile_verticals & message_verticals)


def job_profile_allows_message(profile: str, message_text: str) -> bool:
    return _job_profile_allows_message(profile, message_text)


def _clean_quick_output(text: str, limit: int = 1800) -> str:
    cleaned = (text or "").replace(TRUNCATED_WARNING, "").strip()
    if len(cleaned) > limit:
        return cleaned[:limit].rstrip() + "\n\n..."
    return cleaned


def _has_successful_partials(partials: list[str]) -> bool:
    return any(not (part or "").lstrip().startswith("⚠️ Частина") for part in partials)


MODERATION_ACTION_MARKERS = {
    "обмеж", "огранич", "restricted", "mute", "muted", "мут", "мьют",
    "замʼюч", "зам'юч", "замьюч",
    "забан", "banned", "ban user", "розбан", "разбан", "unban",
    "видал", "удал", "removed", "kick", "кик",
}

MODERATION_USER_MARKERS = {
    "@", "користувач", "пользователь", "user ", "юзер", "учасник", "участник", "member",
}

MODERATION_CONTEXT_MARKERS = {
    "до ", "until", "по ", "чат", "груп", "group", "channel", "канал", "admin", "адмін", "админ",
}

MODERATION_DIRECT_PHRASES = {
    "був обмежений до", "был ограничен до", "обмежений до", "ограничен до",
    "restricted until", "muted until", "замʼючено до", "замьючен до",
    "зам'ючено до", "замьючено до",
    "заблокований до", "заблокирован до",
}


def is_moderation_message(text: str) -> bool:
    normalized = " ".join((text or "").casefold().split())
    if not normalized:
        return False

    has_user = any(marker in normalized for marker in MODERATION_USER_MARKERS)
    if not has_user:
        return False

    if any(phrase in normalized for phrase in MODERATION_DIRECT_PHRASES):
        return True

    has_action = any(marker in normalized for marker in MODERATION_ACTION_MARKERS)
    has_context = any(marker in normalized for marker in MODERATION_CONTEXT_MARKERS)
    return has_action and has_context


JOB_POST_MARKERS = {
    "ваканс", "vacancy", "job", "hiring", "hire", "looking for", "ищем", "шукаємо",
    "ищу", "шукаю", "позиция", "позиція", "position", "remote", "ремоут",
    "віддалено", "удаленно", "full-time", "part-time", "full time", "part time",
    "salary", "зарплата", "ставка", "оклад", "ставка +", "ставка+",
}

JOB_ROLE_MARKERS = {
    "media buyer", "медіабаєр", "медиабайер", "buyer", "affiliate manager",
    "bizdev", "business development", "team lead", "designer", "дизайнер",
    "farmer", "фармер", "фарм", "copywriter", "копірайтер", "копирайтер",
    "developer", "розробник", "разработчик", "sales manager", "account manager",
}


def is_job_post(text: str) -> bool:
    normalized = " ".join((text or "").casefold().split())
    if not normalized:
        return False
    has_job_marker = any(marker in normalized for marker in JOB_POST_MARKERS)
    has_role = any(marker in normalized for marker in JOB_ROLE_MARKERS)
    has_work_terms = any(marker in normalized for marker in {
        "remote", "office", "офіс", "офис", "гібрид", "гибрид", "kyiv", "київ",
        "ставка", "оклад", "salary", "зарплата", "% від", "% от", "досвід", "опыт",
    })
    return (has_job_marker and (has_role or has_work_terms)) or (has_role and has_work_terms)


IMPORTANT_EXCLUDE_MARKERS = {
    "ваканс", "vacancy", "job", "hiring", "hire", "шукаємо", "ищем", "looking for",
    "media buyer", "team lead", "designer", "дизайнер", "farmer", "фармер",
    "зарплата", "salary", "ставка", "remote", "віддалено", "удаленно",
    "продаж баз", "продам баз", "база дан", "database", "data base", "leads database",
    "voip", "автодозвон", "автодозвонювач", "колл-центр", "call center",
    "розсилк", "рассылк", "tfn", "did number", "did номера",
    "куплю аккаунт", "продам аккаунт", "продаж акаунт", "агентські акаунти продам",
}

IMPORTANT_INCLUDE_MARKERS = {
    "offer", "оффер", "офер", "geo", "cpa", "payout", "approve", "апрув",
    "ar ", "cr ", "epc", "$", "%", "ленд", "landing", "prelend", "преленд",
    "facebook ads", "fb ads", "google ads", "tiktok ads", "pmax", "demand gen",
    "бан", "ban", "бм", "bm", "fp", "фп", "клоака", "cloak", "оплата", "карта",
    "закрили", "відкрили", "зміна", "новий ленд", "новий преленд",
}


REPORT_EXCLUDE_MARKERS = {
    "матеріал", "материал", "стройк", "строител", "гараж", "викуп", "выкуп",
    "продам", "куплю", "ціна за", "цена за",
    "участь у чаті обмежена", "участие в чате ограничено", "підписк", "подписк",
    "для подальшої участі", "для дальнейшего участия", "captcha", "капча",
    "rules", "правила чату", "правила группы", "натисніть кнопку", "нажмите кнопку",
    "welcome", "добро пожаловать",
}

REPORT_INCLUDE_MARKERS = IMPORTANT_INCLUDE_MARKERS | {
    "affiliate", "арбітраж", "арбитраж", "media buying", "медіабаїнг", "медиабаинг",
    "traffic", "трафік", "трафик", "nutra", "нутра", "gambling", "гембл",
    "betting", "беттинг", "dating", "дейтинг", "crypto", "крипт",
    "facebook ads", "google ads", "tiktok ads", "meta ads", "ads", "реклама",
    "buyer", "фарм", "фарминг", "фармінг", "аккаунт", "акаунт", "креатив",
    "creative", "spy", "спай", "прокси", "антик", "bm", "бм",
}


def is_report_candidate(text: str) -> bool:
    lowered = (text or "").casefold()
    if not lowered:
        return False
    if any(marker in lowered for marker in REPORT_EXCLUDE_MARKERS):
        return False
    return any(marker.casefold() in lowered for marker in REPORT_INCLUDE_MARKERS)


def filter_important_candidate_messages(messages: list[dict]) -> list[dict]:
    filtered = []
    for msg in messages:
        text = (msg.get("text") or "").casefold()
        if not text:
            continue
        if is_moderation_message(text):
            continue
        if is_job_post(text):
            continue
        if any(marker in text for marker in IMPORTANT_EXCLUDE_MARKERS):
            continue
        if any(marker in text for marker in IMPORTANT_INCLUDE_MARKERS):
            filtered.append(msg)
    return filtered


def filter_general_report_messages(messages: list[dict]) -> list[dict]:
    filtered = []
    for msg in messages:
        text = msg.get("text") or ""
        if is_moderation_message(text):
            continue
        if is_job_post(text):
            continue
        if not is_report_candidate(text):
            continue
        filtered.append(msg)
    return filtered


class AIService:
    def __init__(
        self,
        api_keys: str | list[str] | tuple[str, ...],
        model: str,
        max_input_chars: int,
        groq_api_key: str = "",
        groq_models: list[str] | tuple[str, ...] | None = None,
    ):
        if isinstance(api_keys, str):
            raw_keys = [part.strip() for part in api_keys.split(",") if part.strip()]
        else:
            raw_keys = [key.strip() for key in api_keys if key.strip()]

        self.groq_api_key = groq_api_key.strip()
        keys = [key for key in raw_keys if self._looks_like_gemini_key(key)]
        skipped_keys = len(raw_keys) - len(keys)
        if skipped_keys:
            logger.warning("AIService: пропущено %s Gemini ключів з неправильним форматом", skipped_keys)
        if not keys and not self.groq_api_key:
            raise RuntimeError("AIService requires at least one valid Gemini API key or Groq API key")

        self.api_keys = keys
        self.model = model
        self.max_input_chars = max_input_chars
        self.base_url = "https://generativelanguage.googleapis.com/v1beta"
        self.gemini_max_retries = int(os.getenv("GEMINI_MAX_RETRIES", "3"))
        self.gemini_retry_delay = float(os.getenv("GEMINI_RETRY_DELAY", "8"))
        self.gemini_min_request_interval = float(os.getenv("GEMINI_MIN_REQUEST_INTERVAL", "8"))
        self.gemini_thinking_budget = os.getenv("GEMINI_THINKING_BUDGET", "0").strip()
        self._gemini_lock = asyncio.Lock()
        self._gemini_last_request_at: dict[int, float] = {}

        self.groq_models = [
            model.strip()
            for model in (groq_models or ("llama-3.3-70b-versatile", "llama-3.1-8b-instant"))
            if model.strip()
        ]
        self.groq_max_output_tokens = int(os.getenv("GROQ_MAX_OUTPUT_TOKENS", "8192"))
        self.groq_safe_max_output_tokens = int(os.getenv("GROQ_SAFE_MAX_OUTPUT_TOKENS", "1200"))
        self.groq_max_input_chars = int(os.getenv("GROQ_MAX_INPUT_CHARS", "12000"))
        self.groq_min_request_interval = float(os.getenv("GROQ_MIN_REQUEST_INTERVAL", "2"))
        self._groq_lock = asyncio.Lock()
        self._groq_last_request_at = 0.0
        logger.info(
            "AIService: Gemini keys=%s, Gemini model=%s, Groq fallback=%s",
            len(self.api_keys),
            self.model,
            "on" if self.groq_api_key else "off",
        )

    @staticmethod
    def _looks_like_gemini_key(key: str) -> bool:
        return (key.startswith("AIza") or key.startswith("AQ.")) and len(key) >= 30

    async def _throttled_gemini_post(self, key_index: int, url: str, payload: dict) -> httpx.Response:
        async with self._gemini_lock:
            elapsed = time.monotonic() - self._gemini_last_request_at.get(key_index, 0.0)
            wait_for = self.gemini_min_request_interval - elapsed
            if wait_for > 0:
                await asyncio.sleep(wait_for)

            async with httpx.AsyncClient(timeout=180) as client:
                response = await client.post(url, params={"key": self.api_keys[key_index]}, json=payload)

            self._gemini_last_request_at[key_index] = time.monotonic()
            return response

    async def _throttled_groq_post(self, payload: dict) -> httpx.Response:
        async with self._groq_lock:
            elapsed = time.monotonic() - self._groq_last_request_at
            wait_for = self.groq_min_request_interval - elapsed
            if wait_for > 0:
                await asyncio.sleep(wait_for)

            async with httpx.AsyncClient(timeout=180) as client:
                response = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {self.groq_api_key}"},
                    json=payload,
                )

            self._groq_last_request_at = time.monotonic()
            return response

    async def _complete(
        self,
        system: str,
        user: str,
        max_output_tokens: int = 32768,
        retries: int | None = None,
    ) -> str:
        if not self.api_keys:
            if self.groq_api_key and self.groq_models:
                return await self._complete_groq(system, user, max_output_tokens)
            raise AIProviderUnavailableError("Немає валідних Gemini ключів і Groq fallback вимкнений")

        try:
            return await self._complete_gemini(system, user, max_output_tokens, retries)
        except Exception as gemini_exc:
            if self.groq_api_key and self.groq_models:
                logger.warning("Gemini недоступний, пробую Groq fallback: %s", gemini_exc)
                try:
                    return await self._complete_groq(system, user, max_output_tokens)
                except Exception as groq_exc:
                    logger.error("Groq fallback теж не спрацював: %s", groq_exc)
                    raise RuntimeError(f"Усі AI-провайдери недоступні. Gemini: {gemini_exc}; Groq: {groq_exc}") from groq_exc
            raise

    async def _complete_gemini(
        self,
        system: str,
        user: str,
        max_output_tokens: int,
        retries: int | None,
    ) -> str:
        retries = retries or self.gemini_max_retries
        url = f"{self.base_url}/models/{self.model}:generateContent"

        payload = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "temperature": 0.15,
                "maxOutputTokens": max_output_tokens,
            },
        }
        if self.model.startswith("gemini-2.5") and self.gemini_thinking_budget:
            try:
                payload["generationConfig"]["thinkingConfig"] = {
                    "thinkingBudget": int(self.gemini_thinking_budget)
                }
            except ValueError:
                logger.warning("Некоректний GEMINI_THINKING_BUDGET=%s, пропускаю", self.gemini_thinking_budget)

        last_error: Exception | None = None

        for key_index in range(len(self.api_keys)):
            for attempt in range(1, retries + 1):
                try:
                    response = await self._throttled_gemini_post(key_index, url, payload)

                    if response.status_code == 429:
                        logger.warning("Gemini key %s/%s отримав 429: %s", key_index + 1, len(self.api_keys), response.text[:600])
                        last_error = GeminiRateLimitError(
                            "Gemini API ліміт вичерпано або перевищено rate limit."
                        )
                        break

                    if response.status_code >= 500:
                        raise RuntimeError(f"Gemini API тимчасова помилка {response.status_code}: {response.text[:300]}")

                    if response.status_code >= 400:
                        # інші 4xx (наприклад, неправильний ключ) немає сенсу повторювати на цьому ж ключі
                        last_error = RuntimeError(f"Gemini API error {response.status_code}: {response.text[:1000]}")
                        logger.warning("Gemini key %s/%s отримав %s", key_index + 1, len(self.api_keys), response.status_code)
                        break

                    data = response.json()

                    try:
                        candidate = data["candidates"][0]
                        parts = candidate["content"]["parts"]
                        text = "".join(part.get("text", "") for part in parts).strip()
                        finish_reason = candidate.get("finishReason")
                    except (KeyError, IndexError, TypeError):
                        text = ""
                        finish_reason = None

                    if not text:
                        raise RuntimeError(f"Gemini API returned empty response: {str(data)[:1000]}")

                    if finish_reason == "MAX_TOKENS":
                        logger.warning("Відповідь Gemini обірвана через MAX_TOKENS (запит %s символів)", len(user))

                    if key_index > 0:
                        logger.info("Gemini відповів через резервний ключ %s/%s", key_index + 1, len(self.api_keys))
                    return text

                except (httpx.TransportError, RuntimeError) as exc:
                    last_error = exc
                    logger.warning(
                        "Gemini key %s/%s, спроба %s/%s не вдалась: %s",
                        key_index + 1,
                        len(self.api_keys),
                        attempt,
                        retries,
                        exc,
                    )
                    if isinstance(exc, GeminiRateLimitError):
                        break
                    if attempt < retries:
                        await asyncio.sleep(self.gemini_retry_delay)

        logger.error("Усі Gemini ключі/спроби провалились: %s", last_error)
        raise AIProviderUnavailableError(f"Gemini API недоступний: {last_error}")

    def _compact_for_groq(self, user: str) -> str:
        if len(user) <= self.groq_max_input_chars:
            return user

        marker = "Повідомлення з бази:\n"
        if marker in user:
            head, body = user.split(marker, 1)
            budget = max(2000, self.groq_max_input_chars - len(head) - len(marker) - 500)
            lines = body.splitlines()
            selected: list[str] = []
            used = 0
            for line in lines:
                line_len = len(line) + 1
                if used + line_len > budget:
                    break
                selected.append(line)
                used += line_len
            return (
                f"{head}{marker}"
                + "\n".join(selected)
                + "\n\n[Дані для Groq скорочені, бо free tier має малий TPM. Дай коротку відповідь тільки по видимих даних.]"
            )

        head = user[: max(1000, self.groq_max_input_chars // 3)]
        tail_budget = self.groq_max_input_chars - len(head) - 300
        tail = user[-tail_budget:] if tail_budget > 0 else ""
        return f"{head}\n\n[Середину запиту скорочено для Groq free tier.]\n\n{tail}"

    def _public_ai_error(self, exc: Exception) -> str:
        text = str(exc)
        if "API_KEY_INVALID" in text or "API key not valid" in text:
            return "Gemini ключ невалідний. Перевір .env і прибери неправильний GEMINI_API_KEY."
        if "rate_limit" in text.lower() or "tpm" in text.lower() or "429" in text or "413" in text:
            return "AI вперся в ліміт. Я зменшив запит, спробуй ще раз або зачекай хвилину."
        return "AI тимчасово недоступний. Спробуй ще раз пізніше."

    async def _complete_groq(self, system: str, user: str, max_output_tokens: int) -> str:
        last_error: Exception | None = None
        max_tokens = min(max_output_tokens, self.groq_max_output_tokens, self.groq_safe_max_output_tokens)
        compact_user = self._compact_for_groq(user)

        for model in self.groq_models:
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": compact_user},
                ],
                "temperature": 0.15,
                "max_tokens": max_tokens,
            }

            try:
                response = await self._throttled_groq_post(payload)

                if response.status_code == 429:
                    last_error = GroqRateLimitError(f"Groq model {model} rate limit: {response.text[:600]}")
                    logger.warning("%s", last_error)
                    continue

                if response.status_code >= 500:
                    last_error = RuntimeError(f"Groq API тимчасова помилка {response.status_code}: {response.text[:300]}")
                    logger.warning("%s", last_error)
                    continue

                if response.status_code >= 400:
                    last_error = RuntimeError(f"Groq API error {response.status_code}: {response.text[:600]}")
                    logger.warning("%s", last_error)
                    continue

                data = response.json()
                try:
                    choice = data["choices"][0]
                    text = choice["message"]["content"].strip()
                    finish_reason = choice.get("finish_reason")
                except (KeyError, IndexError, TypeError):
                    text = ""
                    finish_reason = None

                if not text:
                    raise RuntimeError(f"Groq API returned empty response: {str(data)[:1000]}")

                if finish_reason == "length":
                    logger.warning("Відповідь Groq обірвана через max_tokens (запит %s символів)", len(compact_user))

                logger.info("Groq fallback відповів через модель %s", model)
                return text

            except (httpx.TransportError, RuntimeError) as exc:
                last_error = exc
                logger.warning("Groq model %s не відповів: %s", model, exc)
                if not isinstance(exc, GroqRateLimitError):
                    break

        raise AIProviderUnavailableError(f"Groq API недоступний: {last_error}")

    async def _run_chunks(
        self,
        chunks: list[list[dict]],
        build_prompt,
        system: str,
        max_output_tokens: int = 32768,
    ) -> list[str]:
        """
        Обробляє чанки послідовно через спільний throttle.
        Для безкоштовних Gemini-лімітів це важливіше за швидкість:
        паралельні запити легко ловлять 429 і витрачають повтори.
        """

        results: list[str] = []
        for i, chunk in enumerate(chunks, start=1):
            label = _describe_chunk(chunk)
            logger.info("Чанк %s/%s: %s", i, len(chunks), label)
            user = build_prompt(i, chunk)
            try:
                results.append(await self._complete(system, user, max_output_tokens=max_output_tokens))
            except Exception as exc:
                logger.error("Чанк %s/%s не оброблено: %s", i, len(chunks), exc)
                results.append(f"⚠️ Частина {i} ({label}) не оброблена: {self._public_ai_error(exc)}")
                if isinstance(exc, GeminiRateLimitError):
                    break
        return results

    async def extract_memory(self, text: str, current_memory: dict[str, str] | None = None) -> dict[str, str]:
        clean_text = (text or "").strip()
        if len(clean_text) < 20:
            return {}

        current = "\n".join(f"- {k}: {v}" for k, v in (current_memory or {}).items() if v)
        user = (
            f"Поточна пам'ять:\n{current or 'порожня'}\n\n"
            f"Нове повідомлення користувача:\n{clean_text[:2500]}\n\n"
            "Витягни тільки нові або уточнені довгострокові факти."
        )
        raw = await self._complete(MEMORY_EXTRACT_SYSTEM, user, max_output_tokens=800)
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.strip("`").strip()
            if raw.lower().startswith("json"):
                raw = raw[4:].strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("memory extract returned non-json: %s", raw[:300])
            return {}
        if not isinstance(data, dict):
            return {}

        allowed = {
            "role", "verticals", "geo", "partners", "traffic_sources", "budget",
            "priorities", "excludes", "style", "job_criteria", "workflow",
        }
        updates: dict[str, str] = {}
        for key, value in data.items():
            normalized_key = str(key).strip().lower()
            if normalized_key not in allowed:
                continue
            if isinstance(value, list):
                text_value = ", ".join(str(item).strip() for item in value if str(item).strip())
            else:
                text_value = str(value).strip()
            if text_value and text_value not in {"{}", "[]", "null", "None"}:
                updates[normalized_key] = text_value[:500]
        return updates

    async def summarize(self, messages: list[dict]) -> str:
        if not messages:
            return "За цей період повідомлень не знайдено."

        messages = filter_general_report_messages(messages)
        if not messages:
            return "За цей період не знайшов корисних повідомлень для загального звіту. Вакансії винесені в окремий розділ."

        chunks = chunk_messages(messages, self.max_input_chars)

        if not chunks:
            return "За цей період повідомлень не знайдено."

        logger.info("summarize: %s повідомлень -> %s чанків", len(messages), len(chunks))

        def build_prompt(i: int, chunk: list[dict]) -> str:
            body = format_messages(chunk)
            user = SUMMARY_PROMPT.format(messages=body)
            if len(chunks) > 1:
                user = (
                    f"(Частина {i} з {len(chunks)} загальної бази повідомлень за період. "
                    f"Ця частина: {_describe_chunk(chunk)}.)\n\n{user}"
                )
            return user

        partials = await self._run_chunks(chunks, build_prompt, SUMMARY_SYSTEM, max_output_tokens=900)
        if not _has_successful_partials(partials):
            return "⚠️ AI не зміг обробити повідомлення через ключі/ліміти. Перевір Gemini API keys або повтори трохи пізніше."

        if len(partials) == 1:
            return fill_missing_report_sources(clean_report_links(partials[0]), messages)

        # Кожна частина підписана номером і вмістом, щоб при зведенні
        # було зрозуміло, з якого шматка бази походить інформація.
        labeled = [
            f"=== ЧАСТИНА {i} ({_describe_chunk(chunk)}) ===\n{partial}"
            for i, (chunk, partial) in enumerate(zip(chunks, partials), start=1)
        ]

        merge_user = (
            "Ось часткові звіти, які треба звести в один фінальний звіт "
            "за форматом: 📌 Звіт, максимум 6 пунктів, потім ✅ Що робити. "
            "Без оцінок 5/5/4/5, без вакансій, без дублювання:\n\n"
            + "\n\n".join(labeled)
        )

        final_report = await self._complete(MERGE_SYSTEM, merge_user, max_output_tokens=1200)
        return fill_missing_report_sources(clean_report_links(final_report), messages)

    async def ask(self, question: str, messages: list[dict]) -> str:
        if not messages:
            return "В базі немає повідомлень для відповіді на це питання."

        chunks = chunk_messages(messages, self.max_input_chars)
        logger.info("ask: %s повідомлень -> %s чанків", len(messages), len(chunks))

        def build_prompt(i: int, chunk: list[dict]) -> str:
            body = format_messages(chunk)
            user = f"Питання: {question}\n\n"
            if len(chunks) > 1:
                user += f"(Частина {i} з {len(chunks)}: {_describe_chunk(chunk)}.)\n\n"
            user += f"Повідомлення з бази:\n{body}"
            return user

        partials = await self._run_chunks(chunks, build_prompt, ASK_SYSTEM)
        if not _has_successful_partials(partials):
            return "⚠️ AI зараз не зміг обробити базу через ключі/ліміти. Я зменшив Groq-запит; спробуй поставити питання ще раз."

        if len(partials) == 1:
            return partials[0]

        labeled = [
            f"=== ЧАСТИНА {i} ({_describe_chunk(chunk)}) ===\n{partial}"
            for i, (chunk, partial) in enumerate(zip(chunks, partials), start=1)
        ]

        merge_user = (
            f"Питання: {question}\n\n"
            "Ось часткові відповіді на це питання, кожна по своїй частині бази. "
            "Зведи їх в одну фінальну відповідь:\n\n"
            + "\n\n".join(labeled)
        )

        return await self._complete(ASK_MERGE_SYSTEM, merge_user)

    async def search_summary(self, query: str, messages: list[dict]) -> str:
        if not messages:
            return f"По запиту '{query}' нічого не знайшов."

        chunks = chunk_messages(messages, self.max_input_chars)
        logger.info("search_summary: %s повідомлень -> %s чанків (запит: %s)", len(messages), len(chunks), query)

        def build_prompt(i: int, chunk: list[dict]) -> str:
            body = format_messages(chunk)
            user = SEARCH_PROMPT.format(query=query, messages=body)
            if len(chunks) > 1:
                user = f"(Частина {i} з {len(chunks)}: {_describe_chunk(chunk)}.)\n\n{user}"
            return user

        partials = await self._run_chunks(chunks, build_prompt, SEARCH_SYSTEM)
        if not _has_successful_partials(partials):
            return "⚠️ AI зараз не зміг обробити пошук через ключі/ліміти. Перевір Gemini ключі або повтори трохи пізніше."

        if len(partials) == 1:
            return partials[0]

        labeled = [
            f"=== ЧАСТИНА {i} ({_describe_chunk(chunk)}) ===\n{partial}"
            for i, (chunk, partial) in enumerate(zip(chunks, partials), start=1)
        ]

        merge_user = (
            f"Запит: {query}\n\n"
            "Ось часткові звіти пошуку, зведи їх в один фінальний звіт "
            "за тим самим форматом (🔎/🔥/⭐/🟡/💰/💼/🗑️/✅):\n\n"
            + "\n\n".join(labeled)
        )

        return await self._complete(SEARCH_MERGE_SYSTEM, merge_user)

    async def important_digest(self, messages: list[dict]) -> str:
        if not messages:
            return "Немає важливих 5/5 або 4/5 повідомлень."

        messages = filter_important_candidate_messages(messages)
        if not messages:
            return "Немає важливих 5/5 або 4/5 повідомлень."

        chunks = chunk_messages(messages, self.max_input_chars)
        logger.info("important_digest: %s повідомлень -> %s чанків", len(messages), len(chunks))

        def build_prompt(i: int, chunk: list[dict]) -> str:
            body = format_messages(chunk)
            prefix = f"(Частина {i} з {len(chunks)}: {_describe_chunk(chunk)}.)\n\n" if len(chunks) > 1 else ""
            return prefix + f"Залиши тільки 5/5 і 4/5 з цих повідомлень:\n\n{body}"

        partials = await self._run_chunks(chunks, build_prompt, IMPORTANT_SYSTEM)
        if not _has_successful_partials(partials):
            return "⚠️ AI зараз не зміг обробити важливе через ключі/ліміти. Повтори трохи пізніше."
        if len(partials) == 1:
            return partials[0]

        merge_user = (
            "Зведи ці часткові дайджести в один короткий дайджест. "
            "Залиши тільки 5/5 і 4/5, прибери дублікати:\n\n"
            + "\n\n".join(partials)
        )
        return await self._complete(IMPORTANT_SYSTEM, merge_user)

    async def evaluate_chat(self, chat_title: str, username: str | None, messages: list[dict]) -> str:
        if not messages:
            return "Вердикт: ❌ Не варто\nЧому: немає доступних текстових повідомлень для оцінки.\nСигнали: немає"
        body = format_messages(messages[:60])
        user = (
            f"Назва чату: {chat_title}\n"
            f"Username/link: {username or 'немає'}\n\n"
            f"Останні повідомлення:\n{body}"
        )
        return await self._complete(EVALUATE_CHAT_SYSTEM, user, max_output_tokens=2048)

    async def quick_alert(self, message: dict) -> str | None:
        if is_moderation_message(message.get("text") or ""):
            logger.info("quick_alert: службове повідомлення модерації відсічено")
            return None
        if is_job_post(message.get("text") or ""):
            logger.info("quick_alert: вакансію відсічено, вона йде в окремий модуль")
            return None
        body = format_messages([message])
        result = await self._complete(
            QUICK_ALERT_SYSTEM,
            f"Перевір це повідомлення:\n\n{body}",
            max_output_tokens=1024,
            retries=2,
        )
        if "NO_ALERT" in result.strip().upper():
            return None
        return _clean_quick_output(result)

    async def quick_job_alert(self, profile: str, message: dict) -> str | None:
        if not _job_profile_allows_message(profile, message.get("text") or ""):
            logger.info("quick_job_alert: вакансію відсічено по вертикалі профілю")
            return None
        body = format_messages([message])
        user = (
            f"Дані користувача / критерії вакансії:\n{profile or 'Критерії не заповнені'}\n\n"
            f"Перевір повідомлення:\n{body}"
        )
        result = await self._complete(
            JOB_ALERT_SYSTEM,
            user,
            max_output_tokens=2048,
            retries=2,
        )
        if result.strip().upper().startswith("NO_JOB"):
            return None
        return _clean_quick_output(result)
