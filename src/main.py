import asyncio
import logging
import signal

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .config import load_config
from .db import Database
from .ai import AIService
from .collector import TelegramCollector
from .bot import DigestBot

logger = logging.getLogger("main")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

# Скільки днів тримати повідомлення в БД, перш ніж видаляти.
# Якщо захочеш зробити це налаштовуваним через .env - додай
# RETENTION_DAYS в config.py і підстав сюди cfg.retention_days.
RETENTION_DAYS = 90


async def main():
    cfg = load_config()
    db = Database(cfg.db_path)
    await db.init()
    await db.remove_default_user_memory()
    await db.set_meta("session_path", cfg.session_path)
    await db.set_meta("auto_search_interval_hours", "24")
    current_auto_limit = int(await db.get_meta("auto_search_max_per_day", "3") or "3")
    if current_auto_limit > 3:
        await db.set_meta("auto_search_max_per_day", "3")

    ai = AIService(
        cfg.gemini_api_keys,
        cfg.gemini_model,
        cfg.max_input_chars,
        groq_api_key=cfg.groq_api_key,
        groq_models=cfg.groq_models,
    )
    bot = DigestBot(
        cfg.bot_token,
        cfg.owner_chat_id,
        db,
        ai,
        external_ai_urls={
            "chatgpt": cfg.chatgpt_media_buying_url,
            "claude": cfg.claude_media_buying_url,
            "gemini": cfg.gemini_media_buying_url,
        },
    )

    collector = TelegramCollector(
        cfg.telegram_api_id,
        cfg.telegram_api_hash,
        cfg.telegram_phone,
        cfg.session_path,
        db,
        cfg.max_messages_per_chat_on_start,
        ai=ai,
        alert_callback=bot.send_alert,
        ignored_folders=cfg.ignored_folders,
    )
    bot.set_collector(collector)

    await bot.start_polling()
    await collector.start()
    logger.info("✅ Телеграм акаунт підключено")

    added = await collector.backfill_recent(hours=24)
    logger.info("📥 Початкова завантаження: додано %s повідомлень", added)

    scheduler = AsyncIOScheduler(timezone=pytz.timezone(cfg.timezone))

    async def daily_summary_job():
        try:
            if await db.are_reports_paused():
                logger.info("🔇 Плановий звіт пропущено: звіти на паузі")
                return

            last_report_time = await db.get_last_report_time()
            messages = (
                await db.get_messages_since(last_report_time)
                if last_report_time
                else await db.get_recent_messages(hours=24)
            )
            
            if not messages:
                logger.info("ℹ️ Немає нових повідомлень для звіту")
                return
            
            report = await ai.summarize(messages)
            report_id = await db.save_report(report)
            await db.set_last_report_time()
            await bot.send_long(report)
            logger.info("📊 Автозвіт відправлено. Повідомлень: %d, report_id=%s", len(messages), report_id)
            
        except Exception as e:
            logger.error("❌ Помилка звіту: %s", e)
            await bot.send_long(f'❌ Помилка звіту: {e}')

    async def cleanup_job():
        try:
            deleted = await db.delete_older_than(days=RETENTION_DAYS)
            if deleted:
                logger.info("🗑️ Очищення БД: видалено %s старих повідомлень", deleted)
        except Exception as e:
            logger.error("❌ Помилка очищення БД: %s", e)

    async def auto_chat_search_job():
        try:
            await bot.run_auto_chat_search(manual=False)
        except Exception as e:
            logger.error("❌ Помилка автопошуку чатів: %s", e)

    for hour, minute in cfg.summary_times:
        scheduler.add_job(daily_summary_job, 'cron', hour=hour, minute=minute)
        logger.info("📅 Заплановано звіт на %02d:%02d %s", hour, minute, cfg.timezone)

    # Щоденне очищення старих повідомлень - в нічний час, щоб не заважати
    # звітам і не навантажувати БД серед дня.
    scheduler.add_job(cleanup_job, 'cron', hour=4, minute=0)
    logger.info("🧹 Заплановано очищення старих даних (>%s днів) на 04:00 %s", RETENTION_DAYS, cfg.timezone)

    scheduler.add_job(auto_chat_search_job, 'cron', hour=10, minute=30)
    logger.info("🤖 Автопошук чатів заплановано 1 раз на день на 10:30 %s", cfg.timezone)

    scheduler.start()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    tasks = [
        asyncio.create_task(collector.periodic_backfill(cfg.fetch_interval_seconds)),
        asyncio.create_task(collector.run_until_disconnected()),
    ]

    await stop_event.wait()
    logger.info("🛑 Зупиняюсь...")

    for t in tasks:
        t.cancel()
    # Чекаємо, поки скасування реально завершиться, щоб уникнути
    # непідхоплених CancelledError і попереджень про "pending tasks".
    await asyncio.gather(*tasks, return_exceptions=True)

    scheduler.shutdown(wait=False)
    await bot.stop()
    await collector.stop()
    await db.close()
    logger.info("✋ Повністю зупинено")


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception:
        logger.exception("🔥 Критична помилка при запуску/роботі бота")
        raise
