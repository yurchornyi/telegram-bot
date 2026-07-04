from __future__ import annotations

import os
import logging
from datetime import datetime, timedelta, timezone

import aiosqlite

logger = logging.getLogger("db")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def _lower_unicode(value):
    """
    Кастомна SQL-функція для регістронезалежного порівняння.
    Вбудована SQLite LOWER() працює коректно тільки для ASCII (a-z/A-Z),
    кирилиця (і взагалі будь-який не-ASCII текст) лишається без змін -
    тобто пошук "таїланд" не знайде "Таїланд". Python str.lower()
    обробляє Unicode правильно, тому реєструємо її як SQL-функцію.
    """
    return value.lower() if value is not None else None


def _search_variants(token: str) -> list[str]:
    variants = {token}
    for suffix in ('ами', 'ями', 'ого', 'ому', 'ими', 'ими', 'ах', 'ях', 'ом', 'ем', 'ою', 'ею', 'ів', 'ов', 'у', 'ю', 'а', 'я', 'і', 'и', 'е'):
        if len(token) > len(suffix) + 3 and token.endswith(suffix):
            variants.add(token[:-len(suffix)])
    return sorted(variants, key=len, reverse=True)


class Database:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        self._conn: aiosqlite.Connection | None = None

    async def _get_conn(self) -> aiosqlite.Connection:
        """
        Тримаємо ОДНЕ персистентне з'єднання на весь час роботи бота
        замість того, щоб відкривати нове на кожен виклик. aiosqlite
        виконує всі операції на одному внутрішньому потоці-черзі,
        тож виклики через одне з'єднання природно серіалізуються -
        це і швидше (немає накладних витрат на open/close щоразу),
        і безпечніше (менше шансів на 'database is locked' через
        конкуренцію кількох окремих з'єднань).
        """
        if self._conn is None:
            self._conn = await aiosqlite.connect(self.path)
            self._conn.row_factory = aiosqlite.Row
            await self._conn.execute('PRAGMA journal_mode=WAL;')
            await self._conn.execute('PRAGMA busy_timeout=5000;')
            await self._conn.create_function('lower_uni', 1, _lower_unicode)
        return self._conn

    async def init(self):
        db = await self._get_conn()
        await db.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                message_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                chat_title TEXT,
                chat_type TEXT,
                sender_id INTEGER,
                sender_name TEXT,
                text TEXT NOT NULL,
                date TEXT NOT NULL,
                message_link TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (chat_id, message_id)
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS ignored_chats (
                chat_id INTEGER PRIMARY KEY,
                chat_title TEXT,
                ignored_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                text TEXT NOT NULL
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS monitored_chats (
                chat_id INTEGER PRIMARY KEY,
                title TEXT,
                username TEXT,
                link TEXT,
                folder_name TEXT,
                added_at TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS failed_chat_joins (
                chat_id INTEGER PRIMARY KEY,
                title TEXT,
                username TEXT,
                link TEXT,
                error TEXT,
                failed_at TEXT NOT NULL
            )
        ''')
        await self._ensure_column('monitored_chats', 'folder_name', 'TEXT')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS user_memory (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT NOT NULL
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS handoffs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                target TEXT NOT NULL,
                prompt TEXT NOT NULL
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS chat_profiles (
                chat_id INTEGER PRIMARY KEY,
                title TEXT,
                link TEXT,
                profile TEXT,
                score INTEGER,
                updated_at TEXT NOT NULL
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS job_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                text TEXT NOT NULL,
                chat_title TEXT,
                message_link TEXT,
                is_read INTEGER NOT NULL DEFAULT 0
            )
        ''')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_messages_date ON messages(date)')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id)')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_reports_created_at ON reports(created_at)')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_monitored_chats_active ON monitored_chats(is_active)')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_job_alerts_created_at ON job_alerts(created_at)')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_failed_chat_joins_failed_at ON failed_chat_joins(failed_at)')
        await db.commit()
        logger.info("БД ініціалізована: %s", self.path)

    async def _ensure_column(self, table: str, column: str, definition: str):
        db = await self._get_conn()
        cur = await db.execute(f"PRAGMA table_info({table})")
        columns = {row[1] for row in await cur.fetchall()}
        if column not in columns:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    async def close(self):
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
            logger.info("З'єднання з БД закрите")

    async def add_message(self, msg: dict) -> bool:
        if not msg.get('text'):
            return False
        db = await self._get_conn()
        cur = await db.execute('''
            INSERT OR IGNORE INTO messages
            (message_id, chat_id, chat_title, chat_type, sender_id, sender_name, text, date, message_link)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            msg['message_id'], msg['chat_id'], msg.get('chat_title'), msg.get('chat_type'),
            msg.get('sender_id'), msg.get('sender_name'), msg['text'], msg['date'], msg.get('message_link')
        ))
        await db.commit()
        return cur.rowcount > 0

    async def count_today(self) -> int:
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        db = await self._get_conn()
        cur = await db.execute('''
            SELECT COUNT(*) FROM messages 
            WHERE date >= ? AND chat_id NOT IN (SELECT chat_id FROM ignored_chats)
        ''', (since.isoformat(),))
        row = await cur.fetchone()
        return int(row[0])

    async def get_recent_messages(self, hours: int = 24, limit: int = 5000) -> list[dict]:
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        db = await self._get_conn()
        cur = await db.execute('''
            SELECT * FROM messages
            WHERE date >= ? AND chat_id NOT IN (SELECT chat_id FROM ignored_chats)
            ORDER BY date ASC
            LIMIT ?
        ''', (since.isoformat(), limit))
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_messages_since(self, since: datetime, limit: int = 5000) -> list[dict]:
        """Беремо повідомлення від конкретного часу (не за фіксовані 24 години)"""
        db = await self._get_conn()
        cur = await db.execute('''
            SELECT * FROM messages
            WHERE date > ? AND chat_id NOT IN (SELECT chat_id FROM ignored_chats)
            ORDER BY date ASC
            LIMIT ?
        ''', (since.isoformat(), limit))
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_last_report_time(self) -> datetime | None:
        """Читає час останнього успішного звіту або None, якщо його ще не було."""
        db = await self._get_conn()
        cursor = await db.execute(
            "SELECT value FROM meta WHERE key IN ('last_report_at', 'last_report_time') ORDER BY key LIMIT 1"
        )
        row = await cursor.fetchone()
        if row:
            return datetime.fromisoformat(row[0])
        return None

    async def set_last_report_time(self, value: datetime | None = None):
        """Зберігає поточний час як час останнього звіту"""
        now = (value or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        db = await self._get_conn()
        await db.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("last_report_at", now)
        )
        await db.commit()

    async def get_meta(self, key: str, default: str | None = None) -> str | None:
        db = await self._get_conn()
        cur = await db.execute("SELECT value FROM meta WHERE key = ?", (key,))
        row = await cur.fetchone()
        return row[0] if row else default

    async def set_meta(self, key: str, value: str):
        db = await self._get_conn()
        await db.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (key, value),
        )
        await db.commit()

    async def are_reports_paused(self) -> bool:
        paused_until = await self.get_meta("reports_paused_until", "")
        if paused_until == "forever":
            return True
        if paused_until:
            try:
                until = datetime.fromisoformat(paused_until)
                if datetime.now(timezone.utc) < until:
                    return True
                await self.set_reports_paused(False)
                return False
            except ValueError:
                await self.set_reports_paused(False)
                return False
        return (await self.get_meta("reports_paused", "0")) == "1"

    async def set_reports_paused(self, paused: bool, until: datetime | str | None = None):
        await self.set_meta("reports_paused", "1" if paused else "0")
        if not paused:
            await self.set_meta("reports_paused_until", "")
        elif until == "forever":
            await self.set_meta("reports_paused_until", "forever")
        elif isinstance(until, datetime):
            await self.set_meta("reports_paused_until", until.astimezone(timezone.utc).isoformat())
        else:
            await self.set_meta("reports_paused_until", "forever")

    async def toggle_reports_paused(self) -> bool:
        paused = not await self.are_reports_paused()
        await self.set_reports_paused(paused)
        return paused

    async def get_reports_pause_until(self) -> str:
        if not await self.are_reports_paused():
            return ""
        return await self.get_meta("reports_paused_until", "") or "forever"

    async def get_job_profile(self) -> str:
        return await self.get_meta("job_profile", "") or ""

    async def set_job_profile(self, text: str):
        await self.set_meta("job_profile", text)

    async def clear_job_profile(self):
        await self.set_job_profile("")
        await self.set_job_monitor_active(False)

    async def is_job_monitor_active(self) -> bool:
        return (await self.get_meta("job_monitor_active", "0")) == "1"

    async def set_job_monitor_active(self, active: bool):
        await self.set_meta("job_monitor_active", "1" if active else "0")

    async def save_job_alert(self, text: str, msg: dict) -> int:
        db = await self._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        cur = await db.execute(
            '''
            INSERT INTO job_alerts (created_at, text, chat_title, message_link, is_read)
            VALUES (?, ?, ?, ?, 0)
            ''',
            (now, text, msg.get('chat_title'), msg.get('message_link')),
        )
        await db.commit()
        return int(cur.lastrowid)

    async def get_job_alerts(self, limit: int = 20) -> list[dict]:
        db = await self._get_conn()
        cur = await db.execute(
            "SELECT * FROM job_alerts ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_job_alert(self, alert_id: int) -> dict | None:
        db = await self._get_conn()
        cur = await db.execute("SELECT * FROM job_alerts WHERE id = ?", (alert_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def mark_job_alert_read(self, alert_id: int):
        db = await self._get_conn()
        await db.execute("UPDATE job_alerts SET is_read = 1 WHERE id = ?", (alert_id,))
        await db.commit()

    async def save_report(self, text: str) -> int:
        db = await self._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        cur = await db.execute(
            "INSERT INTO reports (created_at, text) VALUES (?, ?)",
            (now, text),
        )
        await db.commit()
        return int(cur.lastrowid)

    async def get_reports(self, limit: int = 10) -> list[dict]:
        db = await self._get_conn()
        cur = await db.execute(
            "SELECT id, created_at, text FROM reports ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_report(self, report_id: int) -> dict | None:
        db = await self._get_conn()
        cur = await db.execute(
            "SELECT id, created_at, text FROM reports WHERE id = ?",
            (report_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def clear_reports(self) -> int:
        db = await self._get_conn()
        cur = await db.execute("DELETE FROM reports")
        await db.execute("DELETE FROM meta WHERE key IN ('last_report_at', 'last_report_time')")
        await db.commit()
        return cur.rowcount

    async def checkpoint(self):
        db = await self._get_conn()
        await db.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        await db.commit()

    async def get_user_memory(self) -> dict[str, str]:
        db = await self._get_conn()
        cur = await db.execute("SELECT key, value FROM user_memory ORDER BY key")
        rows = await cur.fetchall()
        return {str(r[0]): str(r[1] or '') for r in rows}

    async def set_user_memory(self, key: str, value: str):
        db = await self._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "INSERT OR REPLACE INTO user_memory (key, value, updated_at) VALUES (?, ?, ?)",
            (key.strip().lower(), value.strip(), now),
        )
        await db.commit()

    async def delete_user_memory(self, key: str) -> bool:
        db = await self._get_conn()
        cur = await db.execute("DELETE FROM user_memory WHERE key = ?", (key.strip().lower(),))
        await db.commit()
        return cur.rowcount > 0

    async def clear_user_memory(self):
        db = await self._get_conn()
        await db.execute("DELETE FROM user_memory")
        await db.commit()

    def _default_user_memory_values(self) -> dict[str, str]:
        defaults = {
            "role": "Займаюсь арбітражем / медіабаїнгом.",
            "verticals": "Nutra; суглоби, паразити, діабет, похудання.",
            "geo": "Asia, LatAm, Польща, Індонезія, Таїланд, Перу.",
            "partners": "WhoCPA, Dr.Cash, TerraLeads, Everad.",
            "priorities": "Дешевий трафік, нормальний approve rate, невеликі бюджети.",
            "style": "Пояснювати простими словами, з висновком і наступними кроками.",
        }
        return defaults

    async def ensure_default_user_memory(self):
        # Kept for backward compatibility. Memory is no longer auto-filled.
        return

    async def remove_default_user_memory(self) -> int:
        db = await self._get_conn()
        deleted = 0
        for key, value in self._default_user_memory_values().items():
            cur = await db.execute(
                "DELETE FROM user_memory WHERE key = ? AND value = ?",
                (key, value),
            )
            deleted += cur.rowcount
        await db.commit()
        return deleted

    async def save_handoff(self, target: str, prompt: str) -> int:
        db = await self._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        cur = await db.execute(
            "INSERT INTO handoffs (created_at, target, prompt) VALUES (?, ?, ?)",
            (now, target, prompt),
        )
        await db.commit()
        return int(cur.lastrowid)

    async def save_chat_profile(self, chat_id: int, title: str, link: str | None, profile: str, score: int | None = None):
        db = await self._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            '''
            INSERT OR REPLACE INTO chat_profiles (chat_id, title, link, profile, score, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ''',
            (chat_id, title, link, profile, score, now),
        )
        await db.commit()

    async def search(self, query: str, limit: int = 30) -> list[dict]:
        # Регістронезалежний пошук через кастомну Unicode-функцію
        # lower_uni() замість вбудованої LOWER()/LIKE, щоб коректно
        # знаходити кирилицю незалежно від регістру запиту.
        stopwords = {
            'що', 'по', 'про', 'для', 'зараз', 'там', 'тут', 'the', 'and',
            'или', 'або', 'как', 'які', 'який', 'яка', 'есть', 'було',
        }
        tokens = [
            t.strip().lower()
            for t in query.replace('/', ' ').replace('-', ' ').split()
            if len(t.strip()) >= 3 and t.strip().lower() not in stopwords
        ]
        if not tokens:
            tokens = [query.lower()]

        clauses = []
        params = []
        expanded_tokens = []
        for token in tokens[:8]:
            expanded_tokens.extend(_search_variants(token))

        for token in expanded_tokens[:16]:
            q = f'%{token}%'
            clauses.append("(lower_uni(text) LIKE ? OR lower_uni(chat_title) LIKE ?)")
            params.extend([q, q])

        db = await self._get_conn()
        cur = await db.execute('''
            SELECT * FROM messages
            WHERE ({clauses})
            AND chat_id NOT IN (SELECT chat_id FROM ignored_chats)
            ORDER BY date DESC
            LIMIT ?
        '''.format(clauses=" OR ".join(clauses)), (*params, limit))
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def top_chats(self, hours: int = 24, limit: int = 15) -> list[tuple[str, int]]:
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        db = await self._get_conn()
        cur = await db.execute('''
            SELECT COALESCE(chat_title, CAST(chat_id AS TEXT)) AS title, COUNT(*) AS c
            FROM messages
            WHERE date >= ? AND chat_id NOT IN (SELECT chat_id FROM ignored_chats)
            GROUP BY chat_id
            ORDER BY c DESC
            LIMIT ?
        ''', (since.isoformat(), limit))
        rows = await cur.fetchall()
        return [(r[0], int(r[1])) for r in rows]

    async def add_ignored_chat(self, chat_id: int, chat_title: str) -> bool:
        """Додає чат до ігнору"""
        db = await self._get_conn()
        try:
            await db.execute(
                "INSERT INTO ignored_chats (chat_id, chat_title) VALUES (?, ?)",
                (chat_id, chat_title)
            )
            await db.commit()
            logger.info("Чат ігнорується: %s (ID: %s)", chat_title, chat_id)
            return True
        except Exception as e:
            logger.error("Помилка додавання чату в ігнор: %s", e)
            return False

    async def remove_ignored_chat(self, chat_id: int) -> bool:
        """Видаляє чат з ігнору"""
        db = await self._get_conn()
        cur = await db.execute("DELETE FROM ignored_chats WHERE chat_id = ?", (chat_id,))
        await db.commit()
        if cur.rowcount > 0:
            logger.info("Чат видален з ігнору: ID %s", chat_id)
            return True
        return False

    async def is_chat_ignored(self, chat_id: int) -> bool:
        """Перевіряє чи чат ігнорується"""
        db = await self._get_conn()
        cur = await db.execute("SELECT 1 FROM ignored_chats WHERE chat_id = ?", (chat_id,))
        return await cur.fetchone() is not None

    async def get_ignored_chats(self) -> list[tuple[int, str]]:
        """Повертає список ігнорованих чатів"""
        db = await self._get_conn()
        cur = await db.execute(
            "SELECT chat_id, chat_title FROM ignored_chats ORDER BY ignored_at DESC"
        )
        rows = await cur.fetchall()
        return [(r[0], r[1]) for r in rows]

    async def add_monitored_chat(
        self,
        chat_id: int,
        title: str | None = None,
        username: str | None = None,
        link: str | None = None,
        folder_name: str | None = None,
    ) -> bool:
        db = await self._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        existed_cur = await db.execute(
            "SELECT is_active FROM monitored_chats WHERE chat_id = ?",
            (chat_id,),
        )
        existed = await existed_cur.fetchone()
        cur = await db.execute(
            '''
            INSERT INTO monitored_chats (chat_id, title, username, link, folder_name, added_at, is_active)
            VALUES (?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(chat_id) DO UPDATE SET
                title = excluded.title,
                username = excluded.username,
                link = excluded.link,
                folder_name = COALESCE(excluded.folder_name, monitored_chats.folder_name),
                is_active = 1
            ''',
            (chat_id, title, username, link, folder_name, now),
        )
        await db.commit()
        return cur.rowcount > 0 and existed is None

    async def remove_monitored_chat(self, value: int | str) -> bool:
        db = await self._get_conn()
        if isinstance(value, int):
            cur = await db.execute(
                "UPDATE monitored_chats SET is_active = 0 WHERE chat_id = ? AND is_active = 1",
                (value,),
            )
        else:
            username = value.strip().lstrip('@').lower()
            cur = await db.execute(
                '''
                UPDATE monitored_chats SET is_active = 0
                WHERE is_active = 1
                  AND (lower_uni(username) = ? OR lower_uni(link) LIKE ?)
                ''',
                (username, f'%/{username}%'),
            )
        await db.commit()
        return cur.rowcount > 0

    async def get_monitored_chats(self, active_only: bool = True) -> list[dict]:
        db = await self._get_conn()
        where = "WHERE is_active = 1" if active_only else ""
        cur = await db.execute(
            f"SELECT * FROM monitored_chats {where} ORDER BY added_at DESC"
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def is_monitored_chat(self, chat_id: int) -> bool:
        db = await self._get_conn()
        cur = await db.execute(
            "SELECT 1 FROM monitored_chats WHERE chat_id = ? AND is_active = 1",
            (chat_id,),
        )
        return await cur.fetchone() is not None

    async def mark_chat_join_failed(
        self,
        chat_id: int,
        title: str | None = None,
        username: str | None = None,
        link: str | None = None,
        error: str | None = None,
    ):
        db = await self._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            '''
            INSERT INTO failed_chat_joins (chat_id, title, username, link, error, failed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                title = excluded.title,
                username = excluded.username,
                link = excluded.link,
                error = excluded.error,
                failed_at = excluded.failed_at
            ''',
            (chat_id, title, username, link, error, now),
        )
        await db.execute(
            "UPDATE monitored_chats SET is_active = 0 WHERE chat_id = ?",
            (chat_id,),
        )
        await db.commit()

    async def is_failed_chat_join(self, chat_id: int) -> bool:
        db = await self._get_conn()
        cur = await db.execute(
            "SELECT 1 FROM failed_chat_joins WHERE chat_id = ?",
            (chat_id,),
        )
        return await cur.fetchone() is not None

    async def get_failed_chat_join(self, chat_id: int) -> dict | None:
        db = await self._get_conn()
        cur = await db.execute(
            "SELECT * FROM failed_chat_joins WHERE chat_id = ?",
            (chat_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_stats(self) -> dict:
        db = await self._get_conn()

        async def scalar(sql: str, params: tuple = ()) -> int:
            cur = await db.execute(sql, params)
            row = await cur.fetchone()
            return int(row[0] or 0) if row else 0

        since_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        stats = {
            "messages_total": await scalar("SELECT COUNT(*) FROM messages"),
            "messages_24h": await scalar("SELECT COUNT(*) FROM messages WHERE date >= ?", (since_24h,)),
            "chats_in_messages": await scalar("SELECT COUNT(DISTINCT chat_id) FROM messages"),
            "monitored_active": await scalar("SELECT COUNT(*) FROM monitored_chats WHERE is_active = 1"),
            "monitored_total": await scalar("SELECT COUNT(*) FROM monitored_chats"),
            "ignored_chats": await scalar("SELECT COUNT(*) FROM ignored_chats"),
            "failed_joins": await scalar("SELECT COUNT(*) FROM failed_chat_joins"),
            "reports": await scalar("SELECT COUNT(*) FROM reports"),
            "job_alerts": await scalar("SELECT COUNT(*) FROM job_alerts"),
            "memory_items": await scalar("SELECT COUNT(*) FROM user_memory"),
        }

        cur = await db.execute("SELECT MIN(date), MAX(date) FROM messages")
        row = await cur.fetchone()
        stats["first_message_at"] = row[0] if row and row[0] else ""
        stats["last_message_at"] = row[1] if row and row[1] else ""
        stats["last_report_at"] = await self.get_meta("last_report_at", "") or ""
        stats["reports_paused"] = "1" if await self.are_reports_paused() else "0"
        stats["auto_search_enabled"] = await self.get_meta("auto_search_enabled", "0") or "0"
        stats["auto_search_autojoin"] = await self.get_meta("auto_search_autojoin", "0") or "0"
        stats["auto_search_last_run"] = await self.get_meta("auto_search_last_run", "") or ""
        try:
            stats["db_size_bytes"] = os.path.getsize(self.path)
        except OSError:
            stats["db_size_bytes"] = 0
        return stats

    async def delete_older_than(self, days: int) -> int:
        """
        Видаляє повідомлення старші за вказану кількість днів.
        Без цього БД росте назавжди - варто викликати періодично
        (наприклад, раз на добу) з main.py/планувальника, щоб тримати
        розмір файлу і швидкість пошуку під контролем.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        db = await self._get_conn()
        cur = await db.execute('DELETE FROM messages WHERE date < ?', (cutoff.isoformat(),))
        await db.commit()
        deleted = cur.rowcount
        if deleted > 0:
            await db.execute('PRAGMA wal_checkpoint(TRUNCATE);')
            logger.info("Видалено %s старих повідомлень (старші за %s днів)", deleted, days)
        return deleted
