from __future__ import annotations

import asyncio
import io
import json
import logging
import zipfile
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telegram import (
    Update,
    BotCommand,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    KeyboardButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from .db import Database
from .ai import AIService, split_for_telegram, contains_important_report

logger = logging.getLogger("bot")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


class DigestBot:
    MODE_ASK = "ask"
    MODE_CHAT_SEARCH = "chat_search"
    MODE_ADD_CHAT = "add_chat"
    MODE_REMOVE_CHAT = "remove_chat"
    MODE_JOB_PROFILE = "job_profile"
    MODE_JOB_FIELD_PREFIX = "job_field:"
    MODE_MEMORY_ADD = "memory_add"
    MODE_MEMORY_DELETE = "memory_delete"
    AUTO_SEARCH_DEFAULT_QUERY = (
        "affiliate marketing cpa affiliate nutra offers media buying traffic arbitrage "
        "facebook ads affiliate gambling betting dating crypto finance ecommerce leadgen "
        "latam cpa asia cpa арбитраж трафика cpa офферы партнерки cpa медиабаинг "
        "нутра арбитраж гемблинг беттинг дейтинг крипта"
    )

    def __init__(self, token: str, owner_chat_id: int, db: Database, ai: AIService, external_ai_urls: dict | None = None):
        self.token = token
        self.owner_chat_id = owner_chat_id
        self.db = db
        self.ai = ai
        self.external_ai_urls = external_ai_urls or {}
        self.collector = None
        self.chat_search_task = None
        self.app = Application.builder().token(token).build()
        self._register()

    def set_collector(self, collector):
        self.collector = collector

    @property
    def commands(self):
        return [
            BotCommand('start', 'Відкрити меню'),
            BotCommand('menu', 'Показати нижнє меню'),
            BotCommand('summary', 'Звіт по нових повідомленнях'),
            BotCommand('important', 'Тільки 5/5 і 4/5'),
            BotCommand('ask', 'Питання по базі'),
            BotCommand('search', 'Пошук по базі: /search Таїланд'),
            BotCommand('add_chat', 'Додати чат: /add_chat @chatname'),
            BotCommand('remove_chat', 'Видалити чат: /remove_chat @chatname'),
            BotCommand('my_chats', 'Мої чати'),
            BotCommand('memory', 'Памʼять бота про тебе'),
        ]

    @property
    def keyboard(self):
        return ReplyKeyboardMarkup(
            [
                [KeyboardButton('📊 Звіт'), KeyboardButton('💬 Спитати')],
                [KeyboardButton('📜 Історія'), KeyboardButton('🔍 Пошук чатів')],
                [KeyboardButton('⭐ Важливе'), KeyboardButton('💼 Вакансії')],
                [KeyboardButton('⚙️ Налаштування')],
            ],
            resize_keyboard=True,
            one_time_keyboard=False,
            is_persistent=True,
            input_field_placeholder='Вибери дію…',
        )

    @property
    def settings_keyboard(self):
        return ReplyKeyboardMarkup(
            [
                [KeyboardButton('➕ Додати чат'), KeyboardButton('➖ Видалити чат')],
                [KeyboardButton('📋 Мої чати'), KeyboardButton('🔇 Пауза звітів')],
                [KeyboardButton('🧠 Памʼять'), KeyboardButton('🤖 Автопошук чатів')],
                [KeyboardButton('🧹 Очистити історію'), KeyboardButton('📤 Експорт')],
                [KeyboardButton('⬅️ Назад')],
            ],
            resize_keyboard=True,
            one_time_keyboard=False,
            is_persistent=True,
        )

    @property
    def auto_search_keyboard(self):
        return ReplyKeyboardMarkup(
            [
                [KeyboardButton('▶️ Увімкнути'), KeyboardButton('⏸ Вимкнути')],
                [KeyboardButton('📊 Статус'), KeyboardButton('🔍 Запустити зараз')],
                [KeyboardButton('✅ Автопідписка ON/OFF'), KeyboardButton('⚙️ Налаштування автопошуку')],
                [KeyboardButton('⬅️ Назад')],
            ],
            resize_keyboard=True,
            one_time_keyboard=False,
            is_persistent=True,
        )

    @property
    def jobs_keyboard(self):
        return ReplyKeyboardMarkup(
            [
                [KeyboardButton('👤 Мої дані'), KeyboardButton('🗑 Очистити дані')],
                [KeyboardButton('📋 Варіанти'), KeyboardButton('🕘 Історія вакансій')],
                [KeyboardButton('🟢 Активний пошук ON/OFF'), KeyboardButton('⬅️ Назад')],
            ],
            resize_keyboard=True,
            one_time_keyboard=False,
            is_persistent=True,
        )

    @property
    def cancel_keyboard(self):
        return ReplyKeyboardMarkup(
            [[KeyboardButton('❌ Скасувати')]],
            resize_keyboard=True,
            one_time_keyboard=False,
        )

    @property
    def ask_keyboard(self):
        return ReplyKeyboardMarkup(
            [
                [KeyboardButton('🚀 ChatGPT Media Buying'), KeyboardButton('🚀 Claude Media Buying')],
                [KeyboardButton('🚀 Gemini Media Buying'), KeyboardButton('📤 Зробити вижимку')],
                [KeyboardButton('❌ Скасувати')],
            ],
            resize_keyboard=True,
            one_time_keyboard=False,
            is_persistent=True,
        )

    @property
    def memory_keyboard(self):
        return ReplyKeyboardMarkup(
            [
                [KeyboardButton('➕ Додати памʼять'), KeyboardButton('➖ Видалити памʼять')],
                [KeyboardButton('📋 Показати памʼять')],
                [KeyboardButton('⬅️ Назад')],
            ],
            resize_keyboard=True,
            one_time_keyboard=False,
            is_persistent=True,
        )

    @property
    def hide_keyboard(self):
        return ReplyKeyboardRemove()

    def _is_owner(self, update: Update) -> bool:
        return bool(update.effective_chat and update.effective_chat.id == self.owner_chat_id)

    async def _guard(self, update: Update) -> bool:
        if not self._is_owner(update):
            if update.message:
                logger.warning("Доступ заблоковано для chat_id=%s", update.effective_chat.id if update.effective_chat else None)
                await update.message.reply_text('Доступ заборонений.')
            elif update.callback_query:
                await update.callback_query.answer('Доступ заборонений.', show_alert=True)
            return False
        return True

    def _register(self):
        self.app.add_handler(CommandHandler('start', self.start))
        self.app.add_handler(CommandHandler('menu', self.start))
        self.app.add_handler(CommandHandler('summary', self.summary))
        self.app.add_handler(CommandHandler('important', self.important))
        self.app.add_handler(CommandHandler('ask', self.ask_command))
        self.app.add_handler(CommandHandler('memory', self.memory_menu))
        self.app.add_handler(CommandHandler('search', self.search_db))
        self.app.add_handler(CommandHandler('add_chat', self.add_chat_command))
        self.app.add_handler(CommandHandler('remove_chat', self.remove_chat_command))
        self.app.add_handler(CommandHandler('my_chats', self.my_chats))
        self.app.add_handler(CallbackQueryHandler(self.handle_callback))
        self.app.add_handler(MessageHandler(filters.PHOTO, self.handle_photo))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text))
        self.app.add_error_handler(self.on_error)

    def _friendly_error(self, exc: Exception) -> str:
        text = str(exc)
        if 'Gemini API ліміт вичерпано' in text or 'Усі AI-провайдери недоступні' in text or '429' in text:
            return (
                '⚠️ AI зараз не дає відповідь через ліміт.\n'
                'Бот спробував резервні ключі/моделі. Спробуй пізніше або збільш паузи в .env.'
            )
        return f'⚠️ Помилка: {text}'

    async def _reply(self, update_or_query, text: str, reply_markup=None):
        if hasattr(update_or_query, 'message') and update_or_query.message:
            return await update_or_query.message.reply_text(text, reply_markup=reply_markup)
        return await update_or_query.message.reply_text(text, reply_markup=reply_markup)

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        logger.error("Необроблена помилка: %s", context.error, exc_info=context.error)
        try:
            await self.app.bot.send_message(chat_id=self.owner_chat_id, text=self._friendly_error(context.error))
        except Exception:
            logger.error("Не вдалось повідомити про помилку")

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        context.user_data.pop("mode", None)
        self._clear_ask_context(context)
        await update.message.reply_text(
            'Бот моніторингу працює. Вибери дію в меню.',
            reply_markup=self.keyboard,
        )

    def _clear_ask_context(self, context: ContextTypes.DEFAULT_TYPE):
        context.user_data["ask_mode"] = False
        context.user_data["dialog_history"] = []
        context.user_data["context_summary"] = ""
        context.user_data["last_handoff_prompt"] = ""
        context.user_data["last_source_messages"] = []

    def _ensure_ask_context(self, context: ContextTypes.DEFAULT_TYPE):
        context.user_data["ask_mode"] = True
        context.user_data.setdefault("dialog_history", [])
        context.user_data.setdefault("context_summary", "")
        context.user_data.setdefault("last_handoff_prompt", "")
        context.user_data.setdefault("last_source_messages", [])

    def _append_dialog_history(self, context: ContextTypes.DEFAULT_TYPE, question: str, answer: str):
        history = list(context.user_data.get("dialog_history") or [])
        history.append({"question": question, "answer": answer})
        context.user_data["dialog_history"] = history[-8:]
        summary_parts = []
        for item in context.user_data["dialog_history"][-4:]:
            summary_parts.append(f"Q: {item['question']}\nA: {item['answer'][:700]}")
        context.user_data["context_summary"] = "\n\n".join(summary_parts)[-4000:]

    def _extract_memory_updates(self, text: str) -> dict[str, str]:
        lowered = (text or "").casefold()
        updates: dict[str, list[str]] = {}

        def add(key: str, value: str):
            updates.setdefault(key, [])
            if value and value not in updates[key]:
                updates[key].append(value)

        verticals = {
            "nutra": ["nutra", "нутра", "health", "суглоб", "паразит", "діабет", "диабет", "похуд", "weight loss"],
            "gambling": ["gambling", "гембл", "betting", "беттинг", "casino", "казино"],
            "dating": ["dating", "дейтинг", "adult", "адалт"],
            "crypto/finance": ["crypto", "крипт", "finance", "фінанс", "финанс", "forex"],
            "ecom/leadgen": ["ecom", "ecommerce", "leadgen", "lead generation", "cod"],
            "mobile apps": ["mobile apps", "app installs", "uac"],
        }
        for label, words in verticals.items():
            if any(word in lowered for word in words):
                add("verticals", label)

        geos = {
            "Asia": ["asia", "азія", "азия"],
            "LatAm": ["latam", "латам"],
            "Poland": ["poland", "польща", "польша"],
            "Indonesia": ["indonesia", "індонез", "индонез"],
            "Thailand": ["thailand", "таїланд", "таиланд"],
            "Peru": ["peru", "перу"],
            "Brazil": ["brazil", "бразил"],
            "Mexico": ["mexico", "мексик"],
            "Vietnam": ["vietnam", "вʼєтнам", "вьетнам"],
            "Philippines": ["philippines", "філіп", "филип"],
        }
        for label, words in geos.items():
            if any(word in lowered for word in words):
                add("geo", label)

        partners = {
            "WhoCPA": ["whocpa"],
            "Dr.Cash": ["dr.cash", "drcash", "др кеш"],
            "TerraLeads": ["terraleads", "terra leads"],
            "Everad": ["everad"],
        }
        for label, words in partners.items():
            if any(word in lowered for word in words):
                add("partners", label)

        priorities = {
            "дешевий трафік": ["дешев", "cheap traffic"],
            "нормальний approve rate": ["approve", "апрув", "ar "],
            "невеликі бюджети": ["невелик", "малий бюджет", "small budget", "бюджет"],
            "просте пояснення": ["простими словами", "пояснюй просто", "коротко поясни"],
        }
        for label, words in priorities.items():
            if any(word in lowered for word in words):
                add("priorities", label)

        if any(marker in lowered for marker in ["не цікаво", "не интересно", "не хочу", "крім", "кроме", "exclude"]):
            add("excludes", text.strip()[:300])

        if "media buyer" in lowered or "медіабаєр" in lowered or "медиабайер" in lowered:
            add("role", "media buyer")
        if "team lead" in lowered:
            add("role", "team lead")

        if any(marker in lowered for marker in ["не показуй", "не пхай", "не присилай", "не треба", "не интересует"]):
            add("excludes", text.strip()[:300])

        if any(marker in lowered for marker in ["пояснюй", "пояснювати", "коротко", "детально", "простими словами", "на укр"]):
            add("style", text.strip()[:300])

        if any(marker in lowered for marker in ["працюю з", "работаю с", "цікавить", "интересует", "мене цікавить", "хочу шоб"]):
            add("priorities", text.strip()[:300])

        return {key: ", ".join(values) for key, values in updates.items()}

    def _merge_memory_value(self, old: str, new: str) -> str:
        parts: list[str] = []
        for raw in f"{old}, {new}".replace(";", ",").split(","):
            value = raw.strip()
            if value and value.casefold() not in {p.casefold() for p in parts}:
                parts.append(value)
        return ", ".join(parts)

    async def _auto_learn_memory(self, text: str):
        updates = self._extract_memory_updates(text)
        current = await self.db.get_user_memory()
        try:
            ai_updates = await self.ai.extract_memory(text, current)
            for key, value in ai_updates.items():
                if value:
                    updates[key] = value
        except Exception as exc:
            logger.info("AI memory extract skipped: %s", exc)

        if not updates:
            return

        for key, value in updates.items():
            merged = self._merge_memory_value(current.get(key, ""), value)
            await self.db.set_user_memory(key, merged)
            current[key] = merged

    def _is_main_menu_text(self, text: str) -> bool:
        return text in {
            '📊 звіт', 'звіт',
            '💬 спитати', 'спитати',
            '📜 історія', 'історія',
            '🔍 пошук чатів', 'пошук чатів',
            '⭐ важливе', 'важливе',
            '💼 вакансії', 'вакансії', 'вакансии',
            '⚙️ налаштування', 'налаштування',
        }

    async def _handle_main_menu_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
        context.user_data.pop("mode", None)
        if context.user_data.get("ask_mode") and text not in {'💬 спитати', 'спитати'}:
            self._clear_ask_context(context)

        if text in ['📊 звіт', 'звіт']:
            await self.summary(update, context)
        elif text in ['💬 спитати', 'спитати']:
            await self.ask_command(update, context)
        elif text in ['📜 історія', 'історія']:
            await self.history(update, context)
        elif text in ['🔍 пошук чатів', 'пошук чатів']:
            context.user_data["mode"] = self.MODE_CHAT_SEARCH
            await self.show_chat_search_presets(update)
        elif text in ['⭐ важливе', 'важливе']:
            await self.important(update, context)
        elif text in ['💼 вакансії', 'вакансії', 'вакансии']:
            await self.jobs_menu(update, context)
        elif text in ['⚙️ налаштування', 'налаштування']:
            await self.settings(update, context)

    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return

        raw = (update.message.text or '').strip()
        text = raw.lower()
        mode = context.user_data.get("mode")
        ask_mode = bool(context.user_data.get("ask_mode"))

        if text in ['❌ скасувати', 'скасувати', 'отмена', 'cancel']:
            context.user_data.pop("mode", None)
            if ask_mode:
                self._clear_ask_context(context)
                await update.message.reply_text('Діалог завершено. Головне меню.', reply_markup=self.keyboard)
            elif mode == self.MODE_JOB_PROFILE or (mode and mode.startswith(self.MODE_JOB_FIELD_PREFIX)):
                await update.message.reply_text('Скасовано.', reply_markup=self.jobs_keyboard)
            elif mode in [self.MODE_ADD_CHAT, self.MODE_REMOVE_CHAT]:
                await update.message.reply_text('Скасовано.', reply_markup=self.settings_keyboard)
            elif mode in [self.MODE_MEMORY_ADD, self.MODE_MEMORY_DELETE]:
                await update.message.reply_text('Скасовано.', reply_markup=self.memory_keyboard)
            else:
                await update.message.reply_text('Скасовано.', reply_markup=self.keyboard)
            return

        if text in ['⬅️ назад', 'назад']:
            context.user_data.pop("mode", None)
            if ask_mode:
                self._clear_ask_context(context)
            await update.message.reply_text('Головне меню.', reply_markup=self.keyboard)
            return

        if self._is_main_menu_text(text):
            await self._handle_main_menu_text(update, context, text)
            return

        if ask_mode:
            if text in ['📤 зробити вижимку', 'зробити вижимку']:
                await self.send_dialog_digest(update, context)
                return
            if 'chatgpt' in text:
                await self.send_handoff(update, context, 'chatgpt')
                return
            if 'claude' in text:
                await self.send_handoff(update, context, 'claude')
                return
            if 'gemini' in text:
                await self.send_handoff(update, context, 'gemini')
                return
            await self._answer_question(update, context, raw)
            return

        if mode == self.MODE_CHAT_SEARCH:
            if text in ['🔍 пошук чатів', 'пошук чатів']:
                await self.show_chat_search_presets(update)
                return
            if text in [
                '📊 звіт', 'звіт', '💬 спитати', 'спитати', '📜 історія', 'історія',
                '⭐ важливе', 'важливе', '💼 вакансії', 'вакансії', 'вакансии',
                '⚙️ налаштування', 'налаштування',
            ]:
                context.user_data.pop("mode", None)
                mode = None
            context.user_data.pop("mode", None)
            if mode == self.MODE_CHAT_SEARCH:
                await self.start_chat_search(update, raw)
                return

        if mode == self.MODE_ADD_CHAT:
            context.user_data.pop("mode", None)
            await self._add_chat(update, raw)
            return

        if mode == self.MODE_REMOVE_CHAT:
            context.user_data.pop("mode", None)
            await self._remove_chat(update, raw)
            return

        if mode == self.MODE_MEMORY_ADD:
            context.user_data.pop("mode", None)
            await self._save_memory_text(raw)
            await self._auto_learn_memory(raw)
            await update.message.reply_text('✅ Додав у памʼять.', reply_markup=self.memory_keyboard)
            return

        if mode == self.MODE_MEMORY_DELETE:
            context.user_data.pop("mode", None)
            key = raw.strip().lower()
            deleted = await self.db.delete_user_memory(key)
            if deleted:
                await update.message.reply_text(f'🗑 Видалив з памʼяті: {key}', reply_markup=self.memory_keyboard)
            else:
                await update.message.reply_text(f'Не знайшов такого ключа в памʼяті: {key}', reply_markup=self.memory_keyboard)
            return

        if mode == self.MODE_JOB_PROFILE:
            context.user_data.pop("mode", None)
            await self.db.set_job_profile(raw)
            await self._auto_learn_memory(raw)
            await update.message.reply_text(
                '✅ Зберіг твої дані для моніторингу вакансій.',
                reply_markup=self.jobs_keyboard,
            )
            return

        if mode and mode.startswith(self.MODE_JOB_FIELD_PREFIX):
            context.user_data.pop("mode", None)
            field = mode.split(":", 1)[1]
            profile = await self.db.get_job_profile()
            updated = self._set_job_profile_field(profile, field, raw)
            await self.db.set_job_profile(updated)
            await self._auto_learn_memory(updated)
            await update.message.reply_text(
                f'✅ Оновив поле: {self._job_field_label(field)}',
                reply_markup=self.jobs_keyboard,
            )
            return

        if text in ['➕ додати памʼять', 'додати памʼять', 'добавить память']:
            context.user_data["mode"] = self.MODE_MEMORY_ADD
            await update.message.reply_text(
                'Напиши, що запамʼятати. Наприклад: GEO: Індонезія, Таїланд; бюджет невеликий.',
                reply_markup=self.cancel_keyboard,
            )
        elif text in ['➖ видалити памʼять', 'видалити памʼять', 'удалить память']:
            memory = await self.db.get_user_memory()
            if not memory:
                await update.message.reply_text('Памʼять порожня, видаляти нічого.', reply_markup=self.memory_keyboard)
                return
            context.user_data["mode"] = self.MODE_MEMORY_DELETE
            await update.message.reply_text(
                'Напиши ключ, який видалити з памʼяті.\n\n'
                f'Доступні ключі: {", ".join(memory.keys())}',
                reply_markup=self.cancel_keyboard,
            )
        elif text in ['🧠 памʼять', 'памʼять', 'память', '📋 показати памʼять', 'показати памʼять']:
            await self.memory_menu(update, context)
        elif text in ['🧹 очистити історію', 'очистити історію', 'очистити историю']:
            await self.confirm_clear_history(update, context)
        elif text in ['🤖 автопошук чатів', 'автопошук чатів', 'автопоиск чатов']:
            await self.auto_search_menu(update, context)
        elif text in ['▶️ увімкнути', 'увімкнути автопошук', 'включить автопоиск']:
            await self.set_auto_chat_search(update, True)
        elif text in ['⏸ вимкнути', 'вимкнути автопошук', 'выключить автопоиск']:
            await self.set_auto_chat_search(update, False)
        elif text in ['📊 статус', 'статус автопошуку', 'статус автопоиска']:
            await self.auto_search_status(update)
        elif text in ['🔍 запустити зараз', 'запустити зараз', 'запустить сейчас']:
            await self.run_auto_chat_search(manual=True)
        elif text in ['✅ автопідписка on/off', 'автопідписка', 'автоподписка']:
            await self.toggle_auto_chat_join(update)
        elif text in ['⚙️ налаштування автопошуку', 'ліміт автопошуку', 'лимит автопоиска']:
            await self.show_auto_chat_search_limit(update)
        elif text in ['👤 мої дані', 'мої дані', 'мои данные']:
            await self.job_profile(update, context)
        elif text in ['🗑 очистити дані', 'очистити дані', 'удалить данные']:
            await self.clear_job_profile(update, context)
        elif text in ['📋 варіанти', 'варіанти', 'варианты']:
            await self.job_variants(update, context)
        elif text in ['🕘 історія вакансій', 'історія вакансій', 'история вакансий']:
            await self.job_history(update, context)
        elif text in ['🟢 активний пошук', 'активний пошук', 'активный поиск', '🟢 активний пошук on/off']:
            await self.toggle_job_monitor(update, context)
        elif text in ['➕ додати чат', 'додати чат']:
            context.user_data["mode"] = self.MODE_ADD_CHAT
            await update.message.reply_text('Надішли @chatname або https://t.me/chatname', reply_markup=self.cancel_keyboard)
        elif text in ['➖ видалити чат', 'видалити чат']:
            context.user_data["mode"] = self.MODE_REMOVE_CHAT
            await update.message.reply_text('Надішли @chatname, посилання або chat_id для видалення.', reply_markup=self.cancel_keyboard)
        elif text in ['📋 мої чати', 'мої чати']:
            await self.my_chats(update, context)
        elif text in ['🔇 пауза звітів', 'пауза звітів']:
            await self.toggle_reports_pause(update, context)
        elif text in ['📤 експорт', 'експорт']:
            await self.export_data(update, context)
        else:
            await update.message.reply_text('Не зрозумів. Натисни кнопку або команду.', reply_markup=self.keyboard)

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        query = update.callback_query
        await query.answer()
        data = query.data or ""
        if data.startswith("report:"):
            report_id = int(data.split(":", 1)[1])
            report = await self.db.get_report(report_id)
            if not report:
                await query.message.reply_text('Звіт не знайдено.', reply_markup=self.keyboard)
                return
            await self.send_long(report["text"])
            return

        if data == "clear_reports_confirm":
            deleted = await self.db.clear_reports()
            await query.message.reply_text(f'🧹 Історію звітів очищено. Видалено: {deleted}', reply_markup=self.keyboard)
            return

        if data == "clear_reports_cancel":
            await query.message.reply_text('Скасовано. Історію не очищено.', reply_markup=self.keyboard)
            return

        if data.startswith("handoff:"):
            await self.send_handoff(query, context, data.split(":", 1)[1])
            return

        if data.startswith("memory_delete:"):
            key = data.split(":", 1)[1]
            deleted = await self.db.delete_user_memory(key)
            if deleted:
                await query.message.reply_text(f'🗑 Видалив з памʼяті: {key}', reply_markup=self.memory_keyboard)
            else:
                await query.message.reply_text(f'Не знайшов такого ключа в памʼяті: {key}', reply_markup=self.memory_keyboard)
            return

        if data.startswith("auto_search_limit:"):
            value = data.split(":", 1)[1]
            try:
                limit = max(1, min(20, int(value)))
            except ValueError:
                limit = 5
            await self.db.set_meta("auto_search_max_per_day", str(limit))
            await query.message.reply_text(f'✅ Ліміт автопошуку: {limit} чатів/день.', reply_markup=self.auto_search_keyboard)
            return

        if data.startswith("reports_pause:"):
            value = data.split(":", 1)[1]
            await self.apply_reports_pause(query, value)
            return

        if data.startswith("job_alert:"):
            alert_id = int(data.split(":", 1)[1])
            alert = await self.db.get_job_alert(alert_id)
            if not alert:
                await query.message.reply_text('Вакансію не знайдено.', reply_markup=self.jobs_keyboard)
                return
            await self.db.mark_job_alert_read(alert_id)
            buttons = []
            if alert.get("message_link"):
                buttons.append([InlineKeyboardButton('Перейти до повідомлення', url=alert["message_link"])])
            await query.message.reply_text(
                alert["text"],
                reply_markup=InlineKeyboardMarkup(buttons) if buttons else self.jobs_keyboard,
            )
            return

        if data.startswith("job_edit:"):
            field = data.split(":", 1)[1]
            if field == "all":
                context.user_data["mode"] = self.MODE_JOB_PROFILE
                await query.message.reply_text(
                    '✏️ Надішли повний новий текст критеріїв одним повідомленням.',
                    reply_markup=self.cancel_keyboard,
                )
                return

            context.user_data["mode"] = f"{self.MODE_JOB_FIELD_PREFIX}{field}"
            current = self._parse_job_profile(await self.db.get_job_profile()).get(field, "")
            current_text = f'\nПоточне значення: {current}' if current else ''
            await query.message.reply_text(
                f'✏️ Введи нове значення для поля “{self._job_field_label(field)}”.{current_text}',
                reply_markup=self.cancel_keyboard,
            )
            return

        if data.startswith("join_chat:"):
            key = data.split(":", 1)[1]
            if not self.collector:
                await query.message.reply_text('Collector ще не підключений.', reply_markup=self.keyboard)
                return
            try:
                chat = await self.collector.join_and_monitor_search_result(key)
            except Exception as exc:
                logger.error("join_chat: помилка: %s", exc)
                await query.message.reply_text(f'⚠️ Не вдалось підписатися: {exc}', reply_markup=self.keyboard)
                return
            await query.message.reply_text(
                f'✅ Підписався і додав у моніторинг:\n'
                f'{chat["title"]}\n'
                f'{chat.get("link") or chat.get("username") or chat["chat_id"]}',
                reply_markup=self.keyboard,
            )
            return

        if data == "join_all_chats":
            if not self.collector:
                await query.message.reply_text('Collector ще не підключений.', reply_markup=self.keyboard)
                return
            await query.message.reply_text(
                '🔄 Починаю підписку на знайдені чати частинами. '
                'Між вступами є пауза, щоб не ловити flood-limit.',
                reply_markup=self.keyboard,
            )
            try:
                results = await self.collector.join_and_monitor_all_search_results(delay_seconds=60, max_chats=10)
            except Exception as exc:
                logger.error("join_all_chats: помилка: %s", exc)
                await query.message.reply_text(f'⚠️ Batch-підписка зупинилась: {exc}', reply_markup=self.keyboard)
                return

            ok = [r for r in results if r.get("ok")]
            failed = [r for r in results if not r.get("ok")]
            lines = [f'✅ Додано в моніторинг: {len(ok)}']
            for item in ok[:20]:
                lines.append(f'• {item["title"]}')
            if failed:
                lines.append(f'\n⚠️ Не вдалось: {len(failed)}')
                for item in failed[:10]:
                    lines.append(f'• {item["title"]}: {item.get("error")}')
            await self.send_long("\n".join(lines))
            return

        if data == "cancel_chat_search":
            if self.chat_search_task and not self.chat_search_task.done():
                self.chat_search_task.cancel()
                await query.message.reply_text('⛔ Пошук чатів скасовано.', reply_markup=self.keyboard)
            else:
                await query.message.reply_text('Активного пошуку зараз немає.', reply_markup=self.keyboard)
            return

        if data.startswith("chat_preset:"):
            preset = data.split(":", 1)[1]
            if preset == "manual":
                context.user_data["mode"] = self.MODE_CHAT_SEARCH
                await query.message.reply_text(
                    '✍️ Напиши свій запит текстом. Наприклад: solar leads affiliate, casino ads, арбитраж крипта.',
                    reply_markup=self.cancel_keyboard,
                )
                return
            query_text = self._chat_search_preset_query(preset)
            await query.message.reply_text(f'🔍 Запускаю пошук: {query_text}', reply_markup=self.keyboard)
            await self.start_chat_search(query, query_text)
            return

    def _format_memory(self, memory: dict[str, str]) -> str:
        if not memory:
            return "Памʼять порожня."
        labels = {
            "geo": "GEO",
            "priorities": "Пріоритети",
            "style": "Стиль відповіді",
            "verticals": "Вертикалі",
            "partners": "Партнерки",
            "traffic_sources": "Джерела трафіку",
            "budget": "Бюджет",
            "excludes": "Не показувати",
            "role": "Роль",
            "job_criteria": "Критерії вакансій",
            "workflow": "Робочі правила",
        }
        lines = []
        for key, value in memory.items():
            if not (value or "").strip():
                continue
            label = labels.get(key, key)
            lines.append(f"{label}:\n{value}")
        return "\n\n".join(lines) if lines else "Памʼять порожня."

    def _format_dialog_history(self, history: list[dict]) -> str:
        if not history:
            return ""
        lines = []
        for i, item in enumerate(history[-8:], start=1):
            lines.append(f"{i}. Питання: {item.get('question', '')}\nВідповідь: {(item.get('answer', '') or '')[:1200]}")
        return "\n\n".join(lines)

    def _format_sources(self, messages: list[dict]) -> str:
        if not messages:
            return "Прямих джерел у базі немає."
        lines = []
        seen = set()
        for msg in messages[:20]:
            key = (msg.get("chat_title"), msg.get("date"), msg.get("message_link"))
            if key in seen:
                continue
            seen.add(key)
            lines.append(
                f"- Чат: {msg.get('chat_title') or 'невідомо'}\n"
                f"  Дата: {self._format_dt(msg.get('date') or '')}\n"
                f"  Посилання: {msg.get('message_link') or 'недоступне'}"
            )
        return "\n".join(lines)

    async def memory_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        memory = await self.db.get_user_memory()
        buttons = [
            [InlineKeyboardButton(f'🗑 {key}'[:64], callback_data=f'memory_delete:{key}')]
            for key in memory.keys()
        ]
        await update.message.reply_text(
            f'🧠 Памʼять бота про тебе:\n\n{self._format_memory(memory)}',
            reply_markup=InlineKeyboardMarkup(buttons) if buttons else self.memory_keyboard,
        )
        if buttons:
            await update.message.reply_text('Керування памʼяттю:', reply_markup=self.memory_keyboard)

    async def _save_memory_text(self, raw: str):
        if ":" in raw:
            key, value = raw.split(":", 1)
            await self.db.set_user_memory(key, value)
        else:
            now_key = f"note_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
            await self.db.set_user_memory(now_key, raw)

    async def confirm_clear_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        buttons = [[
            InlineKeyboardButton('✅ Так, очистити', callback_data='clear_reports_confirm'),
            InlineKeyboardButton('❌ Скасувати', callback_data='clear_reports_cancel'),
        ]]
        await update.message.reply_text('Точно очистити історію звітів?', reply_markup=InlineKeyboardMarkup(buttons))

    def _build_handoff_prompt(self, context: ContextTypes.DEFAULT_TYPE) -> str:
        memory = context.user_data.get("memory_text") or ""
        history = self._format_dialog_history(context.user_data.get("dialog_history") or [])
        sources = self._format_sources(context.user_data.get("last_source_messages") or [])
        prompt = (
            "Я працюю з Telegram AI-ботом для арбітражу/медіабаїнгу.\n\n"
            f"Мій контекст / памʼять:\n{memory or 'Памʼять не завантажена в цьому повідомленні.'}\n\n"
            f"Коротка вижимка діалогу:\n{context.user_data.get('context_summary') or 'немає'}\n\n"
            f"Останні питання і відповіді:\n{history or 'немає'}\n\n"
            f"Джерела з Telegram-бази:\n{sources}\n\n"
            "Продовжуй аналіз з цього місця як AI-помічник по media buying.\n"
            "Дай:\n"
            "1. короткий висновок;\n"
            "2. що важливо;\n"
            "3. які офери / GEO / партнерки перевірити;\n"
            "4. які ризики;\n"
            "5. що тестити першим;\n"
            "6. які питання задати менеджеру.\n"
        )
        context.user_data["last_handoff_prompt"] = prompt
        return prompt

    def _build_short_handoff_prompt(self, context: ContextTypes.DEFAULT_TYPE, limit: int = 1400) -> str:
        memory = context.user_data.get("memory_text") or ""
        summary = context.user_data.get("context_summary") or ""
        history = context.user_data.get("dialog_history") or []
        last_q = history[-1].get("question", "") if history else ""
        last_a = history[-1].get("answer", "")[:450] if history else ""
        sources = context.user_data.get("last_source_messages") or []
        source_lines = []
        for msg in sources[:3]:
            source_lines.append(
                f"{msg.get('chat_title') or 'чат'} {self._format_dt(msg.get('date') or '')} {msg.get('message_link') or ''}".strip()
            )

        prompt = (
            "Продовж аналіз як AI-помічник з media buying/арбітражу.\n"
            f"Памʼять: {memory[:350] or 'немає'}\n"
            f"Контекст: {summary[:450] or 'немає'}\n"
            f"Останнє питання: {last_q[:250] or 'немає'}\n"
            f"Остання відповідь: {last_a or 'немає'}\n"
            f"Джерела: {'; '.join(source_lines)[:250] or 'прямих джерел немає'}\n"
            "Дай коротко: висновок, що важливо, що тестити першим, ризики, питання менеджеру."
        )
        context.user_data["last_handoff_prompt"] = prompt[:limit]
        return prompt[:limit]

    def _build_prefilled_ai_url(self, base_url: str, target: str, prompt: str) -> tuple[str, bool]:
        # Telegram/browser URLs have practical length limits. Long handoffs can only open the project.
        if len(prompt) > 1800:
            return base_url, False

        split = urlsplit(base_url)
        query = dict(parse_qsl(split.query, keep_blank_values=True))
        if target in {"chatgpt", "claude", "gemini"}:
            query.setdefault("q", prompt)
        else:
            query.setdefault("q", prompt)
        return urlunsplit((split.scheme, split.netloc, split.path, urlencode(query), split.fragment)), True

    async def send_handoff(self, update_or_query, context: ContextTypes.DEFAULT_TYPE, target: str):
        memory = await self.db.get_user_memory()
        context.user_data["memory_text"] = self._format_memory(memory)
        prompt = self._build_short_handoff_prompt(context)
        handoff_id = await self.db.save_handoff(target, prompt)
        url = self.external_ai_urls.get(target) or {
            "chatgpt": "https://chatgpt.com/",
            "claude": "https://claude.ai/",
            "gemini": "https://gemini.google.com/",
        }.get(target, "https://chatgpt.com/")

        buttons = [[InlineKeyboardButton(f'🚀 Відкрити {target.title()} Media Buying', url=url)]]
        await self._reply(
            update_or_query,
            f'📤 Handoff #{handoff_id} готовий. Відкрий сервіс кнопкою нижче.\n\n'
            f'Короткий prompt для копіювання:\n\n{prompt}',
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    async def send_dialog_digest(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        memory = await self.db.get_user_memory()
        context.user_data["memory_text"] = self._format_memory(memory)
        prompt = self._build_handoff_prompt(context)
        digest = (
            "ВИЖИМКА ДІАЛОГУ\n\n"
            f"Памʼять:\n{context.user_data['memory_text']}\n\n"
            f"Що обговорювали:\n{context.user_data.get('context_summary') or 'немає'}\n\n"
            f"Питання/відповіді:\n{self._format_dialog_history(context.user_data.get('dialog_history') or []) or 'немає'}\n\n"
            f"Джерела:\n{self._format_sources(context.user_data.get('last_source_messages') or [])}\n\n"
            f"Готовий prompt для зовнішнього AI:\n{prompt}"
        )
        compact = digest
        if len(compact) > 3900:
            compact = compact[:3800].rstrip() + "\n\n...обрізав, щоб можна було скопіювати з чату."
        await update.message.reply_text(compact, reply_markup=self.ask_keyboard)

    async def send_text_file(self, text: str, filename: str):
        data = io.BytesIO(text.encode('utf-8'))
        data.name = filename
        await self.app.bot.send_document(
            chat_id=self.owner_chat_id,
            document=data,
            filename=filename,
            caption='Готовий текст для копіювання.',
            reply_markup=self.ask_keyboard,
        )

    async def export_data(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return

        payload = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "memory": await self.db.get_user_memory(),
            "monitored_chats": await self.db.get_monitored_chats(active_only=False),
            "reports": await self.db.get_reports(limit=50),
            "job_alerts": await self.db.get_job_alerts(limit=50),
        }

        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("export.json", json.dumps(payload, ensure_ascii=False, indent=2))

            db_path = Path(self.db.path)
            if db_path.exists():
                zf.write(db_path, "digest.sqlite3")

            session_base = Path((await self.db.get_meta("session_path", "")) or "data/telegram_user")
            for candidate in [session_base, session_base.with_suffix(".session")]:
                if candidate.exists():
                    zf.write(candidate, candidate.name)

            env_example = (
                "BOT_TOKEN=...\n"
                "OWNER_CHAT_ID=...\n"
                "TELEGRAM_API_ID=...\n"
                "TELEGRAM_API_HASH=...\n"
                "TELEGRAM_PHONE=...\n"
                "GEMINI_API_KEY=...\n"
                "GROQ_API_KEY=...\n"
                "DB_PATH=data/digest.sqlite3\n"
                "SESSION_PATH=data/telegram_user\n"
            )
            zf.writestr("env.example", env_example)

        archive.seek(0)
        archive.name = f"telegram_bot_export_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.zip"
        await update.message.reply_document(
            document=archive,
            filename=archive.name,
            caption="📤 Експорт готовий: база, памʼять, чати, звіти і session якщо файл знайдено. Секрети з .env не додавав.",
            reply_markup=self.settings_keyboard,
        )

    async def handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        if not context.user_data.get("ask_mode"):
            await update.message.reply_text('Фото зараз обробляються тільки в режимі “💬 Спитати”.', reply_markup=self.keyboard)
            return
        await update.message.reply_text(
            'Зараз підключений текстовий AI-ланцюг. Аналіз фото напряму ще не увімкнений для цієї моделі. '
            'Надішли текстом, що саме треба розібрати зі скріну, або скопіюй текст зі скріну.',
            reply_markup=self.ask_keyboard,
        )

    async def settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        await update.message.reply_text('⚙️ Налаштування:', reply_markup=self.settings_keyboard)

    async def auto_search_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        text = await self._auto_search_status_text()
        await update.message.reply_text(text, reply_markup=self.auto_search_keyboard)

    async def set_auto_chat_search(self, update: Update, enabled: bool):
        if not await self._guard(update):
            return
        await self.db.set_meta("auto_search_enabled", "1" if enabled else "0")
        status = "увімкнений" if enabled else "вимкнений"
        await update.message.reply_text(f'🤖 Автопошук чатів {status}.', reply_markup=self.auto_search_keyboard)

    async def toggle_auto_chat_join(self, update: Update):
        if not await self._guard(update):
            return
        enabled = (await self.db.get_meta("auto_search_autojoin", "0")) == "1"
        new_value = not enabled
        await self.db.set_meta("auto_search_autojoin", "1" if new_value else "0")
        text = (
            "✅ Автопідписка увімкнена. Бот буде сам підписуватись у межах денного ліміту."
            if new_value
            else "⏸ Автопідписка вимкнена. Бот буде тільки присилати список нових чатів."
        )
        await update.message.reply_text(text, reply_markup=self.auto_search_keyboard)

    async def auto_search_status(self, update: Update):
        if not await self._guard(update):
            return
        await update.message.reply_text(await self._auto_search_status_text(), reply_markup=self.auto_search_keyboard)

    async def show_auto_chat_search_limit(self, update: Update):
        if not await self._guard(update):
            return
        buttons = [
            [
                InlineKeyboardButton('3/день', callback_data='auto_search_limit:3'),
                InlineKeyboardButton('5/день', callback_data='auto_search_limit:5'),
            ],
            [
                InlineKeyboardButton('10/день', callback_data='auto_search_limit:10'),
                InlineKeyboardButton('20/день', callback_data='auto_search_limit:20'),
            ],
        ]
        await update.message.reply_text(
            '⚙️ Обери денний ліміт. Для безпеки акаунта краще 3-5 чатів/день.',
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    async def toggle_reports_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        paused_until = await self.db.get_reports_pause_until()
        status = self._format_reports_pause_status(paused_until)
        buttons = [
            [
                InlineKeyboardButton('1 година', callback_data='reports_pause:1h'),
                InlineKeyboardButton('6 годин', callback_data='reports_pause:6h'),
            ],
            [
                InlineKeyboardButton('1 день', callback_data='reports_pause:1d'),
                InlineKeyboardButton('Поки не ввімкну', callback_data='reports_pause:forever'),
            ],
            [
                InlineKeyboardButton('🔔 Увімкнути звіти', callback_data='reports_pause:off'),
            ],
        ]
        await update.message.reply_text(
            f'🔇 Пауза планових звітів\n\n{status}\n\n'
            'Обери термін. Ручні 📊 Звіт і ⭐ Важливе працюють завжди.',
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    async def apply_reports_pause(self, query, value: str):
        now = datetime.now(timezone.utc)
        if value == "off":
            await self.db.set_reports_paused(False)
            await query.message.reply_text('🔔 Планові звіти знову увімкнені.', reply_markup=self.settings_keyboard)
            return

        labels = {
            "1h": ("1 годину", now + timedelta(hours=1)),
            "6h": ("6 годин", now + timedelta(hours=6)),
            "1d": ("1 день", now + timedelta(days=1)),
        }
        if value == "forever":
            await self.db.set_reports_paused(True, "forever")
            await query.message.reply_text(
                '🔇 Планові звіти на паузі, поки ти вручну не ввімкнеш їх назад.',
                reply_markup=self.settings_keyboard,
            )
            return

        label, until = labels.get(value, labels["1h"])
        await self.db.set_reports_paused(True, until)
        await query.message.reply_text(
            f'🔇 Планові звіти на паузі на {label}.\n'
            f'Автоматично ввімкнуться після: {self._format_dt(until.isoformat())} UTC.',
            reply_markup=self.settings_keyboard,
        )

    def _format_reports_pause_status(self, paused_until: str) -> str:
        if not paused_until:
            return "Поточний статус: 🔔 планові звіти увімкнені."
        if paused_until == "forever":
            return "Поточний статус: 🔇 пауза безстроково."
        return f"Поточний статус: 🔇 пауза до {self._format_dt(paused_until)} UTC."

    async def _auto_search_status_text(self) -> str:
        enabled = (await self.db.get_meta("auto_search_enabled", "0")) == "1"
        autojoin = (await self.db.get_meta("auto_search_autojoin", "0")) == "1"
        max_per_day = int(await self.db.get_meta("auto_search_max_per_day", "3") or "3")
        interval_hours = int(await self.db.get_meta("auto_search_interval_hours", "24") or "24")
        day, count = await self._auto_search_daily_counter()
        last_run = await self.db.get_meta("auto_search_last_run", "")
        status = "🟢 увімкнений" if enabled else "🔴 вимкнений"
        join_status = "🟢 увімкнена" if autojoin else "🔴 вимкнена"
        last_run_text = self._format_dt(last_run) if last_run else "ще не запускався"
        return (
            "🤖 Автопошук чатів\n\n"
            f"Статус: {status}\n"
            f"Автопідписка: {join_status}\n"
            f"Ліміт: {count}/{max_per_day} за {day}\n"
            f"Інтервал: кожні {interval_hours} год\n"
            f"Останній запуск: {last_run_text}\n\n"
            "За замовчуванням бот тільки шукає і кидає список. "
            "Автопідписку краще тримати вимкненою, щоб не ловити flood/spam-limit."
        )

    async def _auto_search_daily_counter(self) -> tuple[str, int]:
        today = datetime.now(timezone.utc).date().isoformat()
        day = await self.db.get_meta("auto_search_day", today)
        if day != today:
            await self.db.set_meta("auto_search_day", today)
            await self.db.set_meta("auto_search_count", "0")
            return today, 0
        count = int(await self.db.get_meta("auto_search_count", "0") or "0")
        return day, count

    async def _bump_auto_search_counter(self, amount: int):
        day, count = await self._auto_search_daily_counter()
        await self.db.set_meta("auto_search_day", day)
        await self.db.set_meta("auto_search_count", str(count + max(0, amount)))

    async def run_auto_chat_search(self, manual: bool = False):
        if manual:
            await self.app.bot.send_message(
                chat_id=self.owner_chat_id,
                text='🔍 Запускаю автопошук чатів зараз...',
                reply_markup=self.auto_search_keyboard,
            )

        if not self.collector:
            if manual:
                await self.app.bot.send_message(
                    chat_id=self.owner_chat_id,
                    text='Автопошук недоступний: collector ще не підключений.',
                    reply_markup=self.auto_search_keyboard,
                )
            return

        enabled = (await self.db.get_meta("auto_search_enabled", "0")) == "1"
        if not enabled and not manual:
            return

        interval_hours = int(await self.db.get_meta("auto_search_interval_hours", "24") or "24")
        last_run = await self.db.get_meta("auto_search_last_run", "")
        if last_run and not manual:
            try:
                last_dt = datetime.fromisoformat(last_run)
                if datetime.now(timezone.utc) - last_dt < timedelta(hours=interval_hours):
                    return
            except ValueError:
                pass

        max_per_day = int(await self.db.get_meta("auto_search_max_per_day", "3") or "3")
        _day, count = await self._auto_search_daily_counter()
        remaining = max_per_day - count
        if remaining <= 0:
            if manual:
                await self.app.bot.send_message(
                    chat_id=self.owner_chat_id,
                    text=f'Денний ліміт автопошуку вже вичерпано: {count}/{max_per_day}.',
                    reply_markup=self.auto_search_keyboard,
                )
            return

        await self.db.set_meta("auto_search_last_run", datetime.now(timezone.utc).isoformat())
        query = await self.db.get_meta("auto_search_query", self.AUTO_SEARCH_DEFAULT_QUERY)
        try:
            chats = await self.collector.search_public_chats(
                query,
                limit=5,
                messages_limit=3,
                max_total=min(15, max(remaining * 3, 6)),
                active_days=30,
            )
        except Exception as exc:
            logger.error("auto chat search: помилка: %s", exc)
            await self.app.bot.send_message(
                chat_id=self.owner_chat_id,
                text=self._friendly_error(exc),
                reply_markup=self.auto_search_keyboard,
            )
            return

        fresh = []
        for chat in chats:
            if not await self.db.is_monitored_chat(chat["chat_id"]):
                fresh.append(chat)
            if len(fresh) >= remaining:
                break

        if not fresh:
            if manual:
                await self.app.bot.send_message(
                    chat_id=self.owner_chat_id,
                    text='Нових живих чатів не знайшов. Те, що знайшлось, вже є в моніторингу.',
                    reply_markup=self.auto_search_keyboard,
                )
            return

        autojoin = (await self.db.get_meta("auto_search_autojoin", "0")) == "1"
        if autojoin:
            await self._auto_join_found_chats(fresh)
            return

        await self.app.bot.send_message(
            chat_id=self.owner_chat_id,
            text=f'🤖 Автопошук знайшов {len(fresh)} нових живих чатів. Автопідписка вимкнена, кидаю список.',
            reply_markup=self.auto_search_keyboard,
        )
        for i, chat in enumerate(fresh, start=1):
            link = chat.get("link")
            username = f"@{chat['username']}" if chat.get("username") else "username недоступний"
            text = (
                f'{i}. {chat["title"]}\n'
                f'{username}\n'
                f'Тип: {chat.get("chat_type")}\n'
                f'Активність: {self._format_dt(chat.get("last_activity_at") or "")}\n'
                f'{link or "Посилання недоступне"}'
            )
            buttons = []
            if link:
                buttons.append(InlineKeyboardButton('Відкрити', url=link))
            buttons.append(InlineKeyboardButton('Підписатись + моніторити', callback_data=f'join_chat:{chat["key"]}'))
            await self.app.bot.send_message(
                chat_id=self.owner_chat_id,
                text=text,
                reply_markup=InlineKeyboardMarkup([buttons]),
            )

    async def _auto_join_found_chats(self, chats: list[dict]):
        added = []
        failed = []
        for chat in chats:
            try:
                data = await self.collector.join_and_monitor_search_result(chat["key"])
                added.append(data)
                await asyncio.sleep(60)
            except Exception as exc:
                failed.append((chat, str(exc)))
                logger.warning("auto join не вдався для '%s': %s", chat.get("title"), exc)

        if added:
            await self._bump_auto_search_counter(len(added))

        lines = [f'🤖 Автопошук: підписався і додав у моніторинг: {len(added)}']
        for item in added[:10]:
            lines.append(f'• {item["title"]}')
        if failed:
            lines.append(f'\n⚠️ Не вдалось: {len(failed)}')
            for chat, error in failed[:5]:
                lines.append(f'• {chat.get("title")}: {error}')
        await self.send_long("\n".join(lines), reply_markup=self.auto_search_keyboard)

    async def jobs_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        active = await self.db.is_job_monitor_active()
        profile = await self.db.get_job_profile()
        status = '🟢 увімкнений' if active else '🔴 вимкнений'
        profile_status = 'заповнені' if profile.strip() else 'не заповнені'
        await update.message.reply_text(
            f'💼 Моніторинг вакансій\n\n'
            f'Статус: {status}\n'
            f'Мої дані: {profile_status}\n\n'
            f'Коли активний пошук увімкнений, бот перевіряє нові повідомлення '
            f'на вакансії під твої критерії і шле пуш одразу.',
            reply_markup=self.jobs_keyboard,
        )

    async def clear_job_profile(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        await self.db.clear_job_profile()
        await update.message.reply_text(
            '🗑 Дані вакансій очищено. Активний пошук вимкнено.',
            reply_markup=self.jobs_keyboard,
        )

    async def job_profile(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        profile = await self.db.get_job_profile()
        current = profile.strip() or 'Поки не заповнено.'
        buttons = [
            [
                InlineKeyboardButton('✏️ Посада', callback_data='job_edit:position'),
                InlineKeyboardButton('✏️ Вертикалі', callback_data='job_edit:verticals'),
            ],
            [
                InlineKeyboardButton('✏️ GEO', callback_data='job_edit:geo'),
                InlineKeyboardButton('✏️ Формат', callback_data='job_edit:format'),
            ],
            [
                InlineKeyboardButton('✏️ ЗП', callback_data='job_edit:salary'),
                InlineKeyboardButton('✏️ Не цікаво', callback_data='job_edit:exclude'),
            ],
            [
                InlineKeyboardButton('✏️ Редагувати все', callback_data='job_edit:all'),
            ],
        ]
        await update.message.reply_text(
            '👤 Мої дані для вакансій:\n\n'
            f'{current}\n\n'
            'Обери поле для редагування або “Редагувати все”.',
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    def _job_field_label(self, field: str) -> str:
        return {
            "position": "Посада",
            "verticals": "Вертикалі",
            "geo": "GEO",
            "format": "Формат",
            "salary": "ЗП",
            "exclude": "Не цікаво",
        }.get(field, field)

    def _parse_job_profile(self, profile: str) -> dict:
        fields = {
            "position": "",
            "verticals": "",
            "geo": "",
            "format": "",
            "salary": "",
            "exclude": "",
        }
        aliases = {
            "посада": "position",
            "позиція": "position",
            "позиция": "position",
            "роль": "position",
            "вертикалі": "verticals",
            "вертикали": "verticals",
            "verticals": "verticals",
            "geo": "geo",
            "гео": "geo",
            "формат": "format",
            "format": "format",
            "зп": "salary",
            "зарплата": "salary",
            "salary": "salary",
            "не цікаво": "exclude",
            "не интересно": "exclude",
            "мінус": "exclude",
            "минус": "exclude",
        }
        extras = []
        for line in (profile or "").splitlines():
            if ":" not in line:
                if line.strip():
                    extras.append(line.strip())
                continue
            key, value = line.split(":", 1)
            field = aliases.get(key.strip().lower())
            if field:
                fields[field] = value.strip()
            elif line.strip():
                extras.append(line.strip())
        if extras:
            fields["extra"] = "\n".join(extras)
        return fields

    def _format_job_profile(self, fields: dict) -> str:
        lines = []
        for field in ["position", "verticals", "geo", "format", "salary", "exclude"]:
            value = (fields.get(field) or "").strip()
            if value:
                lines.append(f"{self._job_field_label(field)}: {value}")
        if fields.get("extra"):
            lines.append(fields["extra"])
        return "\n".join(lines).strip()

    def _set_job_profile_field(self, profile: str, field: str, value: str) -> str:
        fields = self._parse_job_profile(profile)
        fields[field] = value.strip()
        return self._format_job_profile(fields)

    async def job_variants(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        await update.message.reply_text(
            '📋 Варіанти, які бот буде нормально розуміти в “Мої дані”:\n\n'
            '1. Посади: media buyer, affiliate manager, farmer, designer, copywriter, developer, team lead.\n'
            '2. Вертикалі: nutra, gambling, betting, dating, adult, crypto, finance, ecom, leadgen, mobile apps.\n'
            '3. GEO: Europe, LATAM, Asia, Tier-1, Tier-2 або конкретні країни.\n'
            '4. Формат: remote, office, hybrid, full-time, part-time.\n'
            '5. Гроші: ставка, %, profit share, мінімальна ЗП.\n'
            '6. Мінус-фільтр: не показувати MLM/HYIP/без досвіду/пасивний дохід/офіс/не ті вертикалі.\n\n'
            'Краще писати природно, не обовʼязково по шаблону.',
            reply_markup=self.jobs_keyboard,
        )

    async def job_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        alerts = await self.db.get_job_alerts(limit=20)
        if not alerts:
            await update.message.reply_text('Історія вакансій поки порожня.', reply_markup=self.jobs_keyboard)
            return

        buttons = []
        for alert in alerts:
            status = '✅' if alert.get('is_read') else '🆕'
            created = self._format_dt(alert.get('created_at') or '')
            title = alert.get('chat_title') or 'чат'
            buttons.append([
                InlineKeyboardButton(
                    f'{status} {created} — {title}'[:64],
                    callback_data=f'job_alert:{alert["id"]}',
                )
            ])
        await update.message.reply_text(
            '🕘 Історія вакансій:',
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    async def toggle_job_monitor(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        profile = await self.db.get_job_profile()
        if not profile.strip():
            await update.message.reply_text(
                'Спочатку заповни “👤 Мої дані”, інакше бот не знатиме, які вакансії тобі підходять.',
                reply_markup=self.jobs_keyboard,
            )
            return
        active = await self.db.is_job_monitor_active()
        await self.db.set_job_monitor_active(not active)
        new_status = '🟢 увімкнений' if not active else '🔴 вимкнений'
        await update.message.reply_text(
            f'✅ Активний пошук вакансій: {new_status}',
            reply_markup=self.jobs_keyboard,
        )

    async def show_chat_search_presets(self, update: Update):
        buttons = [
            [
                InlineKeyboardButton('Всі вертикалі', callback_data='chat_preset:all'),
                InlineKeyboardButton('Traffic sources', callback_data='chat_preset:traffic'),
            ],
            [
                InlineKeyboardButton('Gambling/Betting', callback_data='chat_preset:gambling'),
                InlineKeyboardButton('Dating/Adult', callback_data='chat_preset:dating'),
            ],
            [
                InlineKeyboardButton('Crypto/Finance', callback_data='chat_preset:finance'),
                InlineKeyboardButton('Ecom/Leadgen', callback_data='chat_preset:leadgen'),
            ],
            [
                InlineKeyboardButton('Nutra/Health', callback_data='chat_preset:nutra'),
                InlineKeyboardButton('Mobile Apps', callback_data='chat_preset:mobile'),
            ],
            [
                InlineKeyboardButton('Арбітраж RU/UA', callback_data='chat_preset:ruua'),
                InlineKeyboardButton('LATAM/Asia', callback_data='chat_preset:geo'),
            ],
            [
                InlineKeyboardButton('✍️ Ввести вручну', callback_data='chat_preset:manual'),
            ],
        ]
        await update.message.reply_text(
            '🔍 Обери напрям або натисни “Ввести вручну”.\n'
            'Показую тільки живі публічні чати/канали з активністю за останні 30 днів.\n'
            '“Арбітраж RU/UA” = російсько- та україномовні арбітражні чати.',
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    def _chat_search_preset_query(self, preset: str) -> str:
        presets = {
            'all': 'affiliate media buying cpa arbitrage all verticals',
            'traffic': 'facebook google tiktok native push pop seo uac traffic sources affiliate',
            'gambling': 'gambling betting casino igaming sportsbook affiliate',
            'dating': 'dating adult cams sweepstakes affiliate',
            'finance': 'crypto forex finance loans insurance affiliate',
            'leadgen': 'ecommerce cod lead generation whitehat saas affiliate',
            'nutra': 'nutra health beauty weight loss affiliate',
            'mobile': 'mobile apps app installs in app traffic affiliate',
            'ruua': 'арбитраж трафика медиабаинг партнерки cpa гемблинг беттинг дейтинг крипта финансы нутра',
            'geo': 'latam asia brazil mexico thailand indonesia vietnam philippines affiliate cpa',
        }
        return presets.get(preset, presets['all'])

    async def start_chat_search(self, update_or_query, query_text: str):
        if self.chat_search_task and not self.chat_search_task.done():
            await self._reply(
                update_or_query,
                'Пошук уже йде. Спочатку скасуй поточний або дочекайся завершення.',
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton('⛔ Скасувати пошук', callback_data='cancel_chat_search')
                ]]),
            )
            return

        self.chat_search_task = asyncio.create_task(self._search_public_chats(update_or_query, query_text))
        self.chat_search_task.add_done_callback(self._on_chat_search_done)

    def _on_chat_search_done(self, task: asyncio.Task):
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("chat_search_task: необроблена помилка: %s", exc)

    async def summary(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        await update.message.reply_text('🔄 Роблю звіт по нових повідомленнях...', reply_markup=self.hide_keyboard)
        try:
            last_report_at = await self.db.get_last_report_time()
            if last_report_at:
                messages = await self.db.get_messages_since(last_report_at)
            else:
                messages = await self.db.get_recent_messages(hours=24)

            if not messages:
                await update.message.reply_text('Нових повідомлень для звіту немає.', reply_markup=self.keyboard)
                return

            report = await self.ai.summarize(messages)
            await self.db.save_report(report)
            await self.db.set_last_report_time()
            logger.info("manual summary: %s повідомлень", len(messages))
        except Exception as exc:
            logger.error("summary: помилка: %s", exc)
            await update.message.reply_text(self._friendly_error(exc), reply_markup=self.keyboard)
            return
        await self.send_long(report)

    async def important(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        await update.message.reply_text('⭐ Шукаю 5/5 і 4/5 за останні 24 години...', reply_markup=self.hide_keyboard)
        try:
            messages = await self.db.get_recent_messages(hours=24)
            if not messages:
                await update.message.reply_text('За останні 24 години повідомлень немає.', reply_markup=self.keyboard)
                return
            report = await self.ai.important_digest(messages)
        except Exception as exc:
            logger.error("important: помилка: %s", exc)
            await update.message.reply_text(self._friendly_error(exc), reply_markup=self.keyboard)
            return
        await self.send_long(report)

    async def history(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        reports = await self.db.get_reports(limit=10)
        if not reports:
            await update.message.reply_text('Історія звітів поки порожня.', reply_markup=self.keyboard)
            return
        buttons = []
        for report in reports:
            created = self._format_dt(report["created_at"])
            preview = (report["text"].splitlines()[0] if report["text"] else "Звіт")[:35]
            buttons.append([InlineKeyboardButton(f'{created} — {preview}', callback_data=f'report:{report["id"]}')])
        await update.message.reply_text('📜 Останні 10 звітів:', reply_markup=InlineKeyboardMarkup(buttons))

    async def ask_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        question = ' '.join(context.args).strip() if context.args else ''
        self._ensure_ask_context(context)
        if question:
            await self._answer_question(update, context, question)
            return
        await update.message.reply_text(
            'Напиши питання по базі повідомлень. Можеш задавати декілька питань підряд. '
            'Щоб вийти — натисни ❌ Скасувати.',
            reply_markup=self.ask_keyboard,
        )

    async def _answer_question(self, update: Update, context: ContextTypes.DEFAULT_TYPE, question: str):
        await update.message.reply_text('Думаю по базі повідомлень...', reply_markup=self.ask_keyboard)
        try:
            await self._auto_learn_memory(question)
            search_context = " ".join([
                question,
                str(context.user_data.get("context_summary", ""))[-1200:],
                " ".join(item.get("question", "") for item in (context.user_data.get("dialog_history") or [])[-3:]),
            ])
            messages = await self.db.search(search_context, limit=250)
            memory = await self.db.get_user_memory()
            memory_text = self._format_memory(memory)
            history_text = self._format_dialog_history(context.user_data.get("dialog_history") or [])
            context_summary = context.user_data.get("context_summary", "")
            if not messages:
                answer = (
                    "У моїй базі мало інформації по цьому питанню або прямих згадок немає.\n\n"
                    "Можу продовжити, якщо ти уточниш партнерку / GEO / вертикаль, або запусти “🔍 Пошук чатів”, "
                    "щоб добрати нові джерела."
                )
                self._append_dialog_history(context, question, answer)
                await update.message.reply_text(answer, reply_markup=self.ask_keyboard)
                return
            context.user_data["last_source_messages"] = messages[:20]
            dialogue_question = (
                "Ти AI-помічник для арбітражу/медіабаїнгу. Відповідай не сухо, а з аналізом, висновком, "
                "наступними кроками і джерелами. Якщо даних мало — чесно скажи.\n\n"
                "Якщо користувач просить 'одне', 'одне щось', 'один варіант' або 'коротко' — дай рівно один вибір, "
                "без довгої розкладки, максимум 5-7 рядків.\n\n"
                f"Памʼять про користувача:\n{memory_text}\n\n"
                f"Короткий контекст діалогу:\n{context_summary or 'немає'}\n\n"
                f"Останні питання/відповіді:\n{history_text or 'немає'}\n\n"
                f"Нове питання користувача:\n{question}"
            )
            answer = await self.ai.ask(dialogue_question, messages)
            self._append_dialog_history(context, question, answer)
        except Exception as exc:
            logger.error("ask: помилка: %s", exc)
            await update.message.reply_text(self._friendly_error(exc), reply_markup=self.ask_keyboard)
            return
        await self.send_long(answer, reply_markup=self.ask_keyboard)

    async def search_db(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        query = ' '.join(context.args).strip()
        if not query:
            await update.message.reply_text('Напиши так: /search Таїланд Оферти', reply_markup=self.keyboard)
            return
        await update.message.reply_text(f'🔎 Шукаю в базі: {query}', reply_markup=self.hide_keyboard)
        try:
            rows = await self.db.search(query, limit=100)
            report = await self.ai.search_summary(query, rows)
        except Exception as exc:
            logger.error("search: помилка: %s", exc)
            await update.message.reply_text(self._friendly_error(exc), reply_markup=self.keyboard)
            return
        await self.send_long(report)

    async def _search_public_chats(self, update: Update, query: str):
        if not self.collector:
            await self._reply(update, 'Пошук чатів недоступний: collector ще не підключений.', reply_markup=self.keyboard)
            return
        await self._reply(
            update,
            f'🔍 Шукаю живі публічні чати: {query}',
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton('⛔ Скасувати пошук', callback_data='cancel_chat_search')
            ]]),
        )
        try:
            chats = await self.collector.search_public_chats(query, limit=10, messages_limit=8, max_total=40, active_days=30)
            if not chats:
                await self._reply(update, 'Живих публічних чатів за цим запитом не знайшов.', reply_markup=self.keyboard)
                return

            await self._reply(
                update,
                f'🔍 Знайшов {len(chats)} живих публічних чатів/каналів. '
                f'Спочатку список, кнопка “Підписати всі” буде в кінці.',
                reply_markup=self.hide_keyboard,
            )

            for i, chat in enumerate(chats, start=1):
                link = chat.get("link")
                username = f"@{chat['username']}" if chat.get("username") else "username недоступний"
                text = (
                    f'{i}. {chat["title"]}\n'
                    f'{username}\n'
                    f'Тип: {chat.get("chat_type")}\n'
                    f'Активність: {self._format_dt(chat.get("last_activity_at") or "")}\n'
                    f'{link or "Посилання недоступне"}'
                )
                buttons = []
                if link:
                    buttons.append(InlineKeyboardButton('Відкрити', url=link))
                buttons.append(InlineKeyboardButton('Підписатись + моніторити', callback_data=f'join_chat:{chat["key"]}'))
                await self._reply(update, text, reply_markup=InlineKeyboardMarkup([buttons]))

            await self._reply(
                update,
                '⬇️ Кінець списку. Можеш підписати всі знайдені разом.',
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton('Підписати всі знайдені', callback_data='join_all_chats')
                ]]),
            )
        except asyncio.CancelledError:
            logger.info("Пошук чатів скасовано користувачем")
            raise
        except Exception as exc:
            logger.error("chat search: помилка: %s", exc)
            await self._reply(update, self._friendly_error(exc), reply_markup=self.keyboard)

    async def add_chat_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        ref = ' '.join(context.args).strip()
        if not ref:
            await update.message.reply_text('Напиши так: /add_chat @chatname або /add_chat https://t.me/chatname', reply_markup=self.keyboard)
            return
        await self._add_chat(update, ref)

    async def _add_chat(self, update: Update, ref: str):
        if not self.collector:
            await update.message.reply_text('Не можу додати чат: collector ще не підключений.', reply_markup=self.settings_keyboard)
            return
        try:
            data = await self.collector.add_monitored_chat_from_ref(ref)
        except Exception as exc:
            logger.error("add_chat: помилка: %s", exc)
            await update.message.reply_text(f'⚠️ Не вдалось додати чат: {exc}', reply_markup=self.settings_keyboard)
            return
        await update.message.reply_text(
            f'✅ Чат додано в моніторинг:\n{data["title"]}\n{data.get("link") or data.get("username") or data["chat_id"]}',
            reply_markup=self.settings_keyboard,
        )

    async def remove_chat_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        value = ' '.join(context.args).strip()
        if not value:
            await update.message.reply_text('Напиши так: /remove_chat @chatname або /remove_chat chat_id', reply_markup=self.keyboard)
            return
        await self._remove_chat(update, value)

    async def _remove_chat(self, update: Update, value: str):
        try:
            key = int(value) if value.lstrip('-').isdigit() else value
            success = await self.db.remove_monitored_chat(key)
        except Exception as exc:
            logger.error("remove_chat: помилка: %s", exc)
            await update.message.reply_text(f'⚠️ Помилка: {exc}', reply_markup=self.settings_keyboard)
            return
        if success:
            await update.message.reply_text('✅ Чат видалено з моніторингу.', reply_markup=self.settings_keyboard)
        else:
            await update.message.reply_text('Такого активного чату в моніторингу не знайшов.', reply_markup=self.settings_keyboard)

    async def my_chats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        chats = await self.db.get_monitored_chats()
        if not chats:
            await update.message.reply_text('📋 Мої чати:\n\nСписок порожній.', reply_markup=self.settings_keyboard)
            return
        lines = ['📋 Мої чати:']
        for i, chat in enumerate(chats, start=1):
            name = f"@{chat['username']}" if chat.get('username') else (chat.get('link') or chat.get('title') or str(chat['chat_id']))
            lines.append(f'{i}. {name}')
        await update.message.reply_text("\n".join(lines), reply_markup=self.settings_keyboard)

    async def send_alert(self, text: str):
        await self.send_long(text)

    async def send_long(self, text: str, reply_markup=None):
        markup = self.hide_keyboard if reply_markup is None else reply_markup
        for part in split_for_telegram(text):
            await self.app.bot.send_message(chat_id=self.owner_chat_id, text=part, reply_markup=markup)

    def _format_dt(self, value: str) -> str:
        try:
            dt = datetime.fromisoformat(value)
            return dt.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M')
        except Exception:
            return value[:16]

    async def start_polling(self):
        await self.app.initialize()
        await self.app.bot.set_my_commands(self.commands)
        await self.app.start()
        await self.app.updater.start_polling()
        logger.info("🤖 Бот запущений і слухає")

    async def stop(self):
        await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()
        logger.info("❌ Бот зупинений")
