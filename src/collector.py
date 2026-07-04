from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, UserAlreadyParticipantError
from telethon.tl import types
from telethon.tl.functions import account, folders, messages
from telethon.tl.functions.contacts import SearchRequest
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.types import User, Chat, Channel

from .ai import is_job_post, is_moderation_message, job_profile_allows_message
from .db import Database

logger = logging.getLogger("collector")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def _entity_title(entity) -> str:
    if isinstance(entity, User):
        return ' '.join([x for x in [entity.first_name, entity.last_name] if x]) or entity.username or str(entity.id)
    return getattr(entity, 'title', None) or getattr(entity, 'username', None) or str(getattr(entity, 'id', 'unknown'))


def _entity_type(entity) -> str:
    if isinstance(entity, User):
        return 'private'
    if isinstance(entity, Channel):
        return 'channel' if getattr(entity, 'broadcast', False) else 'supergroup'
    if isinstance(entity, Chat):
        return 'group'
    return 'unknown'


def _sender_name(sender) -> str | None:
    if sender is None:
        return None
    if isinstance(sender, User):
        return ' '.join([x for x in [sender.first_name, sender.last_name] if x]) or sender.username or str(sender.id)
    return getattr(sender, 'title', None) or getattr(sender, 'username', None) or str(getattr(sender, 'id', ''))


def _strip_channel_prefix(raw_id: int) -> int:
    """
    Telethon повертає id каналу/супергрупи як звичайне число (наприклад 1234567890),
    але реальний внутрішній id в Telegram API часто має вигляд -100<id>.
    Формат посилання t.me/c/<id>/<msg> очікує id БЕЗ мінуса і без префіксу -100.
    Якщо префікс -100 уже присутній (буває залежно від того, звідки прийшов id) -
    знімаємо його; якщо id вже "чистий" - лишаємо як є.
    """
    s = str(raw_id)
    if s.startswith('-100'):
        s = s[4:]
    elif s.startswith('-'):
        s = s[1:]
    return int(s)


def _message_link(entity, message_id: int) -> str | None:
    username = getattr(entity, 'username', None)
    if username:
        return f"https://t.me/{username}/{message_id}"

    # Приватні канали/супергрупи без публічного username.
    # t.me/c/<internal_id>/<message_id> відкривається тільки якщо твій
    # акаунт є учасником цього чату.
    if isinstance(entity, Channel):
        clean_id = _strip_channel_prefix(entity.id)
        return f"https://t.me/c/{clean_id}/{message_id}"

    return None


class TelegramCollector:
    ALERT_KEYWORDS = {
        'geo', 'offer', 'оффер', 'офер', 'cpa', 'ar', 'cr', 'epc', 'approve', 'апрув',
        'payout', 'виплата', 'price', 'ціна', 'facebook', 'fb', 'ban', 'бан',
        'lend', 'landing', 'ленд', 'prelend', 'преленд', 'клоака', 'аккаунт',
        'account', 'fp', 'фп', 'карта', 'оплата', 'закрили', 'відкрили',
    }
    ALERT_NUMERIC_MARKERS = {'$', '%', '€'}
    ALERT_ACTION_MARKERS = {
        'закрили', 'відкрили', 'новий', 'нова', 'зміна', 'підняли', 'знизили',
        'апрув', 'approve', 'бан', 'ban', 'не ллється', 'конвертить',
    }
    JOB_KEYWORDS = {
        'вакансія', 'вакансия', 'job', 'hiring', 'hire', 'remote', 'ремоут',
        'удаленно', 'віддалено', 'ищем', 'шукаємо', 'looking for', 'position',
        'media buyer', 'buyer', 'affiliate manager', 'account manager',
        'фармер', 'фарм', 'designer', 'дизайнер', 'копірайтер', 'копирайтер',
        'developer', 'розробник', 'разработчик', 'salary', 'зарплата', 'ставка',
        'оклад', 'full-time', 'part-time', 'full time', 'part time',
    }

    def __init__(
        self,
        api_id: int,
        api_hash: str,
        phone: str,
        session_path: str,
        db: Database,
        max_messages_on_start: int,
        ai=None,
        alert_callback=None,
        ignored_folders: tuple[str, ...] = ("Партнери",),
    ):
        self.client = TelegramClient(session_path, api_id, api_hash)
        self.phone = phone
        self.db = db
        self.max_messages_on_start = max_messages_on_start
        self.ai = ai
        self.alert_callback = alert_callback
        self.ignored_folders = {name.casefold() for name in ignored_folders if name}
        self._search_cache = {}
        self._running = False

    async def start(self):
        await self.client.start(phone=self.phone)
        self._running = True
        self.client.add_event_handler(self._on_new_message, events.NewMessage())
        logger.info("👂 Колектор запущений, слухаю нові повідомлення")

    async def _on_new_message(self, event):
        try:
            msg = await self._convert_message(event.message)
            if msg:
                added = await self.db.add_message(msg)
                if added:
                    await self._maybe_send_job_alert(msg)
        except FloodWaitError as e:
            logger.warning("⏱️ FloodWait на новому повідомленні, чекаю %s сек", e.seconds)
            await asyncio.sleep(e.seconds)
        except Exception as e:
            logger.error("❌ Помилка обробки нового повідомлення: %s", e)

    async def _should_collect_entity(self, entity) -> bool:
        chat_type = _entity_type(entity)
        chat_id = int(getattr(entity, 'id', 0))
        title = _entity_title(entity).casefold()
        if title in self.ignored_folders:
            return False
        if chat_type == 'private':
            return False
        if chat_type in ('channel', 'supergroup'):
            return True
        if chat_type == 'group':
            return await self.db.is_monitored_chat(chat_id)
        return False

    def _looks_potentially_important(self, text: str) -> bool:
        if is_moderation_message(text):
            return False
        if is_job_post(text):
            return False

        lowered = text.lower()
        has_keyword = any(keyword in lowered for keyword in self.ALERT_KEYWORDS)
        has_number = any(ch.isdigit() for ch in lowered) or any(marker in lowered for marker in self.ALERT_NUMERIC_MARKERS)
        has_action = any(marker in lowered for marker in self.ALERT_ACTION_MARKERS)
        has_geo_like = any(token in lowered for token in ['geo', 'гео', 'th', 'id', 'vn', 'ph', 'br', 'mx', 'latam', 'asia'])

        score = sum([has_keyword, has_number, has_action, has_geo_like])
        return score >= 2

    async def _maybe_send_quick_alert(self, msg: dict):
        if not self.ai or not self.alert_callback:
            return
        if not self._looks_potentially_important(msg.get('text') or ''):
            return
        try:
            alert = await self.ai.quick_alert(msg)
            if alert:
                await self.alert_callback(alert)
        except Exception as exc:
            logger.error("❌ quick_alert помилка: %s", exc)

    def _looks_like_job(self, text: str) -> bool:
        lowered = text.lower()
        return any(keyword in lowered for keyword in self.JOB_KEYWORDS)

    async def _maybe_send_job_alert(self, msg: dict):
        if not self.ai or not self.alert_callback:
            return
        if not self._looks_like_job(msg.get('text') or ''):
            return
        profile = ""
        try:
            if not await self.db.is_job_monitor_active():
                return
            profile = await self.db.get_job_profile()
            if not profile.strip():
                return
            if not job_profile_allows_message(profile, msg.get('text') or ''):
                logger.info("job_alert: вакансію відсічено по вертикалі профілю до AI")
                return
            alert = await self.ai.quick_job_alert(profile, msg)
            if alert:
                await self.db.save_job_alert(alert, msg)
                await self.alert_callback(alert)
        except Exception as exc:
            logger.error("❌ job_alert помилка: %s", exc)
            if profile.strip() and job_profile_allows_message(profile, msg.get('text') or ''):
                text = (msg.get('text') or '').strip()
                fallback = (
                    "💼 Можлива вакансія під твої критерії\n\n"
                    "AI зараз не зміг нормально оцінити через ліміт/помилку, але повідомлення схоже на вакансію.\n\n"
                    f"Чат: {msg.get('chat_title') or 'невідомо'}\n"
                    f"Посилання: {msg.get('message_link') or 'недоступне'}\n\n"
                    f"{text[:900]}"
                )
                await self.db.save_job_alert(fallback, msg)
                await self.alert_callback(fallback)

    async def _convert_message(self, message) -> dict | None:
        text = (message.message or '').strip()
        if not text:
            return None
        entity = await message.get_chat()
        if not await self._should_collect_entity(entity):
            return None
        sender = await message.get_sender()
        date = message.date
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return {
            'message_id': message.id,
            'chat_id': int(getattr(entity, 'id', message.chat_id)),
            'chat_title': _entity_title(entity),
            'chat_type': _entity_type(entity),
            'sender_id': int(getattr(sender, 'id', 0)) if sender else None,
            'sender_name': _sender_name(sender),
            'text': text,
            'date': date.astimezone(timezone.utc).isoformat(),
            'message_link': _message_link(entity, message.id),
        }

    async def backfill_recent(self, hours: int = 24) -> int:
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        added = 0
        dialogs_done = 0

        async for dialog in self.client.iter_dialogs():
            try:
                folder_name = getattr(getattr(dialog, 'folder', None), 'title', None)
                if folder_name and folder_name.casefold() in self.ignored_folders:
                    continue
                if not await self._should_collect_entity(dialog.entity):
                    continue
                async for message in self.client.iter_messages(dialog.entity, limit=self.max_messages_on_start):
                    if not message.date:
                        continue
                    msg_date = message.date if message.date.tzinfo else message.date.replace(tzinfo=timezone.utc)
                    if msg_date < since:
                        break
                    converted = await self._convert_message(message)
                    if converted and await self.db.add_message(converted):
                        added += 1
                dialogs_done += 1
            except FloodWaitError as e:
                # Telegram явно просить почекати N секунд - чекаємо стільки,
                # скільки сказано, і пробуємо цей самий діалог ще раз,
                # а не пропускаємо його назавжди для цього запуску.
                logger.warning("⏱️ FloodWait у '%s', чекаю %s сек", dialog.name, e.seconds)
                await asyncio.sleep(e.seconds)
                try:
                    if not await self._should_collect_entity(dialog.entity):
                        continue
                    async for message in self.client.iter_messages(dialog.entity, limit=self.max_messages_on_start):
                        if not message.date:
                            continue
                        msg_date = message.date if message.date.tzinfo else message.date.replace(tzinfo=timezone.utc)
                        if msg_date < since:
                            break
                        converted = await self._convert_message(message)
                        if converted and await self.db.add_message(converted):
                            added += 1
                    dialogs_done += 1
                except Exception as retry_e:
                    logger.error("❌ Повторна спроба для '%s' теж не вдалась: %s", dialog.name, retry_e)
            except Exception as e:
                logger.error("❌ Backfill помилка в '%s': %s", dialog.name, e)

        logger.info("✅ Backfill завершено: %s нових повідомлень, %s діалогів оброблено", added, dialogs_done)
        return added

    async def sync_monitored_recent(
        self,
        hours: int = 24,
        per_chat_limit: int = 50,
        max_chats: int = 80,
    ) -> int:
        """
        Fast refresh for "what happened today" questions.
        Reads only chats explicitly stored in monitored_chats, not every dialog
        in the account. This keeps answers fresh without doing global Telegram
        search or touching subscriptions.
        """
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        added = 0
        chats_done = 0
        chats = await self.db.get_monitored_chats(active_only=True)

        for chat in chats[:max_chats]:
            ref = chat.get("username") or chat.get("link") or chat.get("chat_id")
            if not ref:
                continue
            if isinstance(ref, str) and chat.get("username") and not ref.startswith("@"):
                ref = f"@{ref}"

            try:
                entity = await self.client.get_entity(ref)
                if not await self._should_collect_entity(entity):
                    continue

                async for message in self.client.iter_messages(
                    entity,
                    limit=min(per_chat_limit, self.max_messages_on_start),
                ):
                    if not message.date:
                        continue
                    msg_date = message.date if message.date.tzinfo else message.date.replace(tzinfo=timezone.utc)
                    msg_date = msg_date.astimezone(timezone.utc)
                    if msg_date < since:
                        break
                    converted = await self._convert_message(message)
                    if converted and await self.db.add_message(converted):
                        added += 1
                chats_done += 1
            except FloodWaitError as exc:
                logger.warning("⏱️ sync_monitored_recent FloodWait у '%s': %s сек", chat.get("title"), exc.seconds)
                if exc.seconds <= 20:
                    await asyncio.sleep(exc.seconds)
                    continue
                break
            except Exception as exc:
                logger.warning("sync_monitored_recent: не вдалось оновити '%s': %s", chat.get("title"), exc)

            if chats_done % 10 == 0:
                await asyncio.sleep(0.5)

        logger.info("🔄 sync_monitored_recent: %s нових повідомлень, %s чатів перевірено", added, chats_done)
        return added

    async def add_monitored_chat_from_ref(self, ref: str) -> dict:
        ref = ref.strip()
        if not ref:
            raise ValueError("Порожнє посилання або username")
        entity = await self.client.get_entity(ref)
        chat_type = _entity_type(entity)
        if chat_type == 'private':
            raise ValueError("Private-чати не додаються в моніторинг")
        username = getattr(entity, 'username', None)
        link = f"https://t.me/{username}" if username else None
        data = {
            'chat_id': int(getattr(entity, 'id')),
            'title': _entity_title(entity),
            'username': username,
            'link': link or ref,
            'chat_type': chat_type,
        }
        is_new = await self.db.add_monitored_chat(data['chat_id'], data['title'], data['username'], data['link'])
        data['is_new'] = is_new
        logger.info("%s: %s", "Новий чат додано" if is_new else "Чат вже існує, оновлено", data['title'])
        return data

    async def search_public_chats(
        self,
        query: str,
        limit: int = 10,
        messages_limit: int = 8,
        max_total: int = 40,
        active_days: int = 30,
    ) -> list[dict]:
        queries = self._expand_search_queries(query)
        found = []
        seen = set()
        active_since = datetime.now(timezone.utc) - timedelta(days=active_days)

        for q in queries:
            await asyncio.sleep(0)
            result = await self.client(SearchRequest(q=q, limit=limit))
            for chat in result.chats:
                chat_type = _entity_type(chat)
                chat_id = int(getattr(chat, 'id'))
                if chat_type == 'private' or chat_id in seen:
                    continue
                if await self.db.is_failed_chat_join(chat_id):
                    continue
                seen.add(chat_id)

                last_activity_at, recent_count = await self._chat_activity(chat, messages_limit=messages_limit)
                if not last_activity_at or last_activity_at < active_since:
                    continue

                username = getattr(chat, 'username', None)
                link = f"https://t.me/{username}" if username else None
                cache_key = str(chat_id)
                self._search_cache[cache_key] = chat
                found.append({
                    'key': cache_key,
                    'chat_id': chat_id,
                    'title': _entity_title(chat),
                    'username': username,
                    'link': link,
                    'chat_type': chat_type,
                    'last_activity_at': last_activity_at.isoformat(),
                    'recent_messages_checked': recent_count,
                })
                if len(found) >= max_total:
                    return found
        return found

    async def _chat_activity(self, chat, messages_limit: int = 8) -> tuple[datetime | None, int]:
        last_activity_at = None
        count = 0
        try:
            async for message in self.client.iter_messages(chat, limit=messages_limit):
                if not message.date:
                    continue
                msg_date = message.date if message.date.tzinfo else message.date.replace(tzinfo=timezone.utc)
                msg_date = msg_date.astimezone(timezone.utc)
                if last_activity_at is None or msg_date > last_activity_at:
                    last_activity_at = msg_date
                count += 1
        except Exception as exc:
            logger.warning("Не вдалось перевірити активність '%s': %s", _entity_title(chat), exc)
            return None, 0
        return last_activity_at, count

    def _expand_search_queries(self, query: str) -> list[str]:
        base = " ".join(query.strip().split())
        if not base:
            base = "affiliate arbitrage"

        expanded = [base]
        expanded.extend([
            # General affiliate / media buying
            'affiliate marketing',
            'affiliate chat',
            'affiliate community',
            'affiliate offers',
            'affiliate network',
            'affiliate manager',
            'cpa affiliate',
            'cpa marketing',
            'cpa network',
            'cpa offers',
            'performance marketing',
            'media buying',
            'media buyers',
            'traffic arbitrage',
            'webmaster affiliate',
            'webmaster cpa',

            # Traffic sources
            'facebook ads affiliate',
            'fb ads affiliate',
            'google ads affiliate',
            'tiktok ads affiliate',
            'native ads affiliate',
            'push traffic affiliate',
            'pop traffic affiliate',
            'telegram traffic affiliate',
            'seo affiliate',
            'uac affiliate',
            'in app traffic',

            # Verticals
            'nutra affiliate',
            'nutra offers',
            'health affiliate',
            'beauty affiliate',
            'weight loss affiliate',
            'gambling affiliate',
            'igaming affiliate',
            'betting affiliate',
            'casino affiliate',
            'sports betting affiliate',
            'dating affiliate',
            'adult affiliate',
            'crypto affiliate',
            'forex affiliate',
            'finance affiliate',
            'loan affiliate',
            'insurance affiliate',
            'sweepstakes affiliate',
            'lead generation affiliate',
            'mobile app affiliate',
            'app installs affiliate',
            'ecommerce affiliate',
            'ecom affiliate',
            'cod affiliate',
            'whitehat affiliate',
            'saas affiliate',
            'edu affiliate',
            'travel affiliate',
            'solar affiliate',
            'home improvement leads',

            # GEO / regional
            'latam affiliate',
            'latam cpa',
            'asia affiliate',
            'asia cpa',
            'europe affiliate',
            'mena affiliate',
            'gcc affiliate',
            'thai cpa',
            'indonesia cpa',
            'vietnam cpa',
            'philippines cpa',
            'brazil cpa',
            'mexico cpa',

            # Russian/Ukrainian ecosystem
            'арбитраж трафика',
            'арбитраж чат',
            'арбитраж офферы',
            'арбитраж вертикали',
            'арбитраж gambling',
            'арбитраж betting',
            'арбитраж dating',
            'арбитраж crypto',
            'арбитраж finance',
            'арбитраж leadgen',
            'cpa арбитраж',
            'cpa офферы',
            'партнерские программы',
            'партнерки cpa',
            'медиабаинг',
            'медиабайинг',
            'заливы facebook',
            'facebook арбитраж',
            'google ads арбитраж',
            'tiktok арбитраж',
            'нутра cpa',
            'нутра офферы',
            'нутра арбитраж',
            'гемблинг арбитраж',
            'беттинг арбитраж',
            'крипта арбитраж',
            'дейтинг арбитраж',
            'финансы арбитраж',
            'лиды арбитраж',
            'мобильные приложения арбитраж',
        ])

        result = []
        for item in expanded:
            if item and item not in result:
                result.append(item)
        return result[:120]

    async def join_and_monitor_search_result(self, key: str) -> dict:
        entity = self._search_cache.get(str(key))
        if entity is None:
            raise ValueError("Результат пошуку вже не в кеші. Запусти пошук ще раз.")
        try:
            return await self._join_and_monitor_entity(entity)
        except Exception as exc:
            await self._mark_join_failed(entity, exc)
            raise

    async def join_and_monitor_all_search_results(self, delay_seconds: float = 8.0, max_chats: int = 20) -> list[dict]:
        results = []
        active_before = await self.db.get_monitored_chats()
        entities = []
        for entity in self._search_cache.values():
            chat_id = int(getattr(entity, 'id'))
            if await self.db.is_failed_chat_join(chat_id):
                continue
            entities.append(entity)
            if len(entities) >= max_chats:
                break
        for index, entity in enumerate(entities, start=1):
            try:
                data = await self._join_and_monitor_entity(entity)
                results.append({"ok": True, "title": data["title"], "link": data.get("link"), "is_new": data.get("is_new", False)})
            except FloodWaitError as exc:
                logger.warning("FloodWait під час batch join: %s сек", exc.seconds)
                await self._mark_join_failed(entity, exc)
                results.append({"ok": False, "title": _entity_title(entity), "error": f"FloodWait {exc.seconds} сек"})
                break
            except Exception as exc:
                logger.warning("Batch join не вдався для '%s': %s", _entity_title(entity), exc)
                await self._mark_join_failed(entity, exc)
                results.append({"ok": False, "title": _entity_title(entity), "error": str(exc)})
            if index < len(entities):
                await asyncio.sleep(delay_seconds)
        active_after = await self.db.get_monitored_chats()
        logger.info(
            "Batch join: нових=%s, вже існували=%s, всього активних=%s",
            sum(1 for r in results if r.get("ok") and r.get("is_new")),
            sum(1 for r in results if r.get("ok") and not r.get("is_new")),
            len(active_after),
        )
        return results

    async def _join_and_monitor_entity(self, entity) -> dict:
        input_peer = await self.client.get_input_entity(entity)

        chat_type = _entity_type(entity)
        if chat_type == 'private':
            raise ValueError("Private-чати не додаються в моніторинг")

        try:
            if isinstance(entity, Channel):
                await self.client(JoinChannelRequest(entity))
        except UserAlreadyParticipantError:
            logger.info("Вже підписаний на '%s'", _entity_title(entity))
        except Exception as exc:
            logger.warning("Не вдалось підписатися на '%s': %s", _entity_title(entity), exc)
            raise

        await self._organize_joined_chat(entity, input_peer)

        username = getattr(entity, 'username', None)
        link = f"https://t.me/{username}" if username else None
        data = {
            'chat_id': int(getattr(entity, 'id')),
            'title': _entity_title(entity),
            'username': username,
            'link': link,
            'chat_type': chat_type,
        }
        is_new = await self.db.add_monitored_chat(data['chat_id'], data['title'], data['username'], data['link'])
        data['is_new'] = is_new
        logger.info("%s: %s", "Новий чат додано" if is_new else "Чат вже існує, пропущено", data['title'])
        return data

    async def _mark_join_failed(self, entity, exc: Exception):
        chat_id = int(getattr(entity, 'id'))
        username = getattr(entity, 'username', None)
        link = f"https://t.me/{username}" if username else None
        await self.db.mark_chat_join_failed(
            chat_id=chat_id,
            title=_entity_title(entity),
            username=username,
            link=link,
            error=str(exc),
        )
        self._search_cache.pop(str(chat_id), None)

    async def _organize_joined_chat(self, entity, input_peer):
        title = _entity_title(entity)
        try:
            await self.client(account.UpdateNotifySettingsRequest(
                peer=types.InputNotifyPeer(input_peer),
                settings=types.InputPeerNotifySettings(
                    silent=True,
                    mute_until=datetime.now(timezone.utc) + timedelta(days=3650),
                ),
            ))
            logger.info("🔇 Чат зам'ючено: %s", title)
        except Exception as exc:
            logger.warning("Не вдалось зам'ютити '%s': %s", title, exc)

        try:
            await self.client(folders.EditPeerFoldersRequest([
                types.InputFolderPeer(peer=input_peer, folder_id=1)
            ]))
            logger.info("🗄️ Чат заархівовано: %s", title)
        except Exception as exc:
            logger.warning("Не вдалось заархівувати '%s': %s", title, exc)

        try:
            await self._add_peer_to_ai_folder(input_peer)
            logger.info("📁 Чат додано в папку AI: %s", title)
        except Exception as exc:
            logger.warning("Не вдалось додати '%s' в папку AI: %s", title, exc)

    async def _add_peer_to_ai_folder(self, input_peer):
        dialog_filters = await self.client(messages.GetDialogFiltersRequest())
        filters_list = getattr(dialog_filters, 'filters', dialog_filters)
        existing = None
        used_ids = set()
        for item in filters_list:
            filter_id = getattr(item, 'id', None)
            if filter_id is not None:
                used_ids.add(filter_id)
            if isinstance(item, types.DialogFilter) and getattr(item, 'title', None) == 'AI':
                existing = item

        if existing and isinstance(existing, types.DialogFilter):
            include_peers = list(existing.include_peers or [])
            if not any(repr(peer) == repr(input_peer) for peer in include_peers):
                include_peers.append(input_peer)
            updated = types.DialogFilter(
                id=existing.id,
                title=existing.title,
                pinned_peers=list(existing.pinned_peers or []),
                include_peers=include_peers,
                exclude_peers=list(existing.exclude_peers or []),
                contacts=existing.contacts,
                non_contacts=existing.non_contacts,
                groups=existing.groups,
                broadcasts=existing.broadcasts,
                bots=existing.bots,
                exclude_muted=existing.exclude_muted,
                exclude_read=existing.exclude_read,
                exclude_archived=existing.exclude_archived,
                emoticon=existing.emoticon,
                color=getattr(existing, 'color', None),
            )
            await self.client(messages.UpdateDialogFilterRequest(id=existing.id, filter=updated))
            return

        folder_id = next((i for i in range(2, 20) if i not in used_ids), 2)
        new_filter = types.DialogFilter(
            id=folder_id,
            title='AI',
            pinned_peers=[],
            include_peers=[input_peer],
            exclude_peers=[],
            contacts=False,
            non_contacts=False,
            groups=True,
            broadcasts=True,
            bots=False,
            exclude_muted=False,
            exclude_read=False,
            exclude_archived=False,
            emoticon='🤖',
        )
        await self.client(messages.UpdateDialogFilterRequest(id=folder_id, filter=new_filter))

    async def _convert_public_message(self, entity, message) -> dict | None:
        text = (message.message or '').strip()
        if not text:
            return None
        date = message.date
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return {
            'message_id': message.id,
            'chat_id': int(getattr(entity, 'id', message.chat_id)),
            'chat_title': _entity_title(entity),
            'chat_type': _entity_type(entity),
            'sender_id': None,
            'sender_name': None,
            'text': text,
            'date': date.astimezone(timezone.utc).isoformat(),
            'message_link': _message_link(entity, message.id),
        }

    async def periodic_backfill(self, interval_seconds: int):
        while self._running:
            try:
                added = await self.backfill_recent(hours=24)
                logger.info("🔄 Періодичний backfill: додано %s повідомлень", added)
            except Exception as e:
                logger.error("❌ Періодичний backfill помилка: %s", e)
            await asyncio.sleep(interval_seconds)

    async def run_until_disconnected(self):
        await self.client.run_until_disconnected()

    async def stop(self):
        """
        Зупиняє periodic_backfill (цикл вийде на наступній перевірці _running)
        і коректно відключає Telethon-клієнт.
        """
        self._running = False
        await self.client.disconnect()
        logger.info("⛔ Колектор зупинений")
