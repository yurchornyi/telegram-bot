from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

load_dotenv()


def _required(name: str) -> str:
    value = os.getenv(name, '').strip()
    if not value:
        raise RuntimeError(f'Missing required env var: {name}')
    return value


def _required_int(name: str) -> int:
    raw = _required(name)
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(f'Env var {name} має бути цілим числом, отримано: "{raw}"')


def _optional_int(name: str, default: str) -> int:
    raw = os.getenv(name, default).strip()
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(f'Env var {name} має бути цілим числом, отримано: "{raw}"')


def _optional(name: str, default: str = '') -> str:
    return os.getenv(name, default).strip()


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(',') if part.strip())


def _collect_gemini_api_keys() -> Tuple[str, ...]:
    keys: list[str] = []

    for value in _split_csv(os.getenv('GEMINI_API_KEYS', '')):
        if value not in keys:
            keys.append(value)

    numbered_names = sorted(
        (
            name for name in os.environ
            if re.fullmatch(r'GEMINI_API_KEY_?\d+', name)
        ),
        key=lambda name: int(re.search(r'\d+', name).group(0)),
    )

    for name in ('GEMINI_API_KEY', *numbered_names):
        value = _optional(name)
        if value and value not in keys:
            keys.append(value)

    if not keys and not _collect_groq_api_keys() and not _collect_openrouter_api_keys():
        raise RuntimeError('Missing required env var: GEMINI_API_KEY або GROQ_API_KEY або OPENROUTER_API_KEY')

    return tuple(keys)


def _collect_api_keys(base_name: str, csv_name: str | None = None) -> Tuple[str, ...]:
    keys: list[str] = []

    if csv_name:
        for value in _split_csv(os.getenv(csv_name, '')):
            if value not in keys:
                keys.append(value)

    numbered_names = sorted(
        (
            name for name in os.environ
            if re.fullmatch(fr'{re.escape(base_name)}_?\d+', name)
        ),
        key=lambda name: int(re.search(r'\d+', name).group(0)),
    )

    for name in (base_name, *numbered_names):
        value = _optional(name)
        if value and value not in keys:
            keys.append(value)

    return tuple(keys)


def _collect_groq_api_keys() -> Tuple[str, ...]:
    return _collect_api_keys('GROQ_API_KEY', 'GROQ_API_KEYS')


def _collect_groq_models() -> Tuple[str, ...]:
    models: list[str] = []
    configured = _split_csv(os.getenv('GROQ_MODELS', ''))
    fallback_names = (
        _optional('GROQ_MODEL', 'llama-3.3-70b-versatile'),
        _optional('GROQ_FALLBACK_MODEL', 'llama-3.1-8b-instant'),
    )

    for model in (*configured, *fallback_names):
        if model and model not in models:
            models.append(model)

    quality_order = {
        'llama-3.3-70b-versatile': 0,
        'llama-3.1-8b-instant': 1,
    }
    return tuple(sorted(models, key=lambda model: quality_order.get(model, 100)))


def _collect_openrouter_api_keys() -> Tuple[str, ...]:
    return _collect_api_keys('OPENROUTER_API_KEY', 'OPENROUTER_API_KEYS')


def _collect_openrouter_models() -> Tuple[str, ...]:
    models: list[str] = []
    configured = _split_csv(os.getenv('OPENROUTER_MODELS', ''))
    fallback_names = (
        _optional('OPENROUTER_MODEL', 'openrouter/free'),
        _optional('OPENROUTER_FALLBACK_MODEL', ''),
    )
    for model in (*configured, *fallback_names):
        if model and model not in models:
            models.append(model)
    return tuple(models or ('openrouter/free',))


def _parse_summary_times(value: str) -> Tuple[tuple[int, int], ...]:
    times = []
    for part in value.split(','):
        part = part.strip()
        if not part:
            continue

        if ':' in part:
            h_raw, m_raw = part.split(':', 1)
        else:
            h_raw, m_raw = part, '0'

        try:
            h, m = int(h_raw), int(m_raw)
        except ValueError:
            raise RuntimeError(
                f'SUMMARY_TIMES містить некоректне значення "{part}" - очікується формат ГГ:ХХ, напр. 21:00'
            )

        if not (0 <= h <= 23) or not (0 <= m <= 59):
            raise RuntimeError(
                f'SUMMARY_TIMES містить час поза межами доби: "{part}" '
                f'(години 0-23, хвилини 0-59)'
            )

        times.append((h, m))

    if not times:
        times.append((21, 0))

    return tuple(times)


def _validate_timezone(tz: str) -> str:
    try:
        ZoneInfo(tz)
    except ZoneInfoNotFoundError:
        raise RuntimeError(
            f'TIMEZONE "{tz}" не розпізнана. Приклад коректного значення: Europe/Kyiv'
        )
    return tz


def _ensure_parent_dir(path: str) -> str:
    """
    Створює батьківську директорію для файлу (db_path / session_path),
    якщо вона ще не існує. Без цього перший запуск на чистому сервері
    падає з 'no such file or directory', бо папка data/ ще не створена.
    """
    parent = Path(path).parent
    if str(parent) not in ('', '.'):
        parent.mkdir(parents=True, exist_ok=True)
    return path


@dataclass(frozen=True)
class Config:
    telegram_api_id: int
    telegram_api_hash: str
    telegram_phone: str
    bot_token: str
    owner_chat_id: int
    gemini_api_keys: Tuple[str, ...]
    gemini_model: str
    groq_api_keys: Tuple[str, ...]
    groq_models: Tuple[str, ...]
    openrouter_api_keys: Tuple[str, ...]
    openrouter_models: Tuple[str, ...]
    timezone: str
    summary_times: Tuple[tuple[int, int], ...]
    fetch_interval_seconds: int
    max_messages_per_chat_on_start: int
    max_input_chars: int
    db_path: str
    session_path: str
    chatgpt_media_buying_url: str
    claude_media_buying_url: str
    gemini_media_buying_url: str
    ignored_folders: Tuple[str, ...]


def load_config() -> Config:
    db_path = _ensure_parent_dir(os.getenv('DB_PATH', 'data/digest.sqlite3').strip())
    session_path = _ensure_parent_dir(os.getenv('SESSION_PATH', 'data/telegram_user').strip())

    return Config(
        telegram_api_id=_required_int('TELEGRAM_API_ID'),
        telegram_api_hash=_required('TELEGRAM_API_HASH'),
        telegram_phone=_required('TELEGRAM_PHONE'),
        bot_token=_required('BOT_TOKEN'),
        owner_chat_id=_required_int('OWNER_CHAT_ID'),
        gemini_api_keys=_collect_gemini_api_keys(),
        gemini_model=os.getenv('GEMINI_MODEL', 'gemini-2.5-flash').strip(),
        groq_api_keys=_collect_groq_api_keys(),
        groq_models=_collect_groq_models(),
        openrouter_api_keys=_collect_openrouter_api_keys(),
        openrouter_models=_collect_openrouter_models(),
        timezone=_validate_timezone(os.getenv('TIMEZONE', 'Europe/Kyiv').strip()),
        summary_times=_parse_summary_times(os.getenv('SUMMARY_TIMES', '09:00,21:00')),
        fetch_interval_seconds=_optional_int('FETCH_INTERVAL_SECONDS', '300'),
        max_messages_per_chat_on_start=_optional_int('MAX_MESSAGES_PER_CHAT_ON_START', '200'),
        max_input_chars=_optional_int('MAX_INPUT_CHARS', '55000'),
        db_path=db_path,
        session_path=session_path,
        chatgpt_media_buying_url=_optional('CHATGPT_MEDIA_BUYING_URL', 'https://chatgpt.com/'),
        claude_media_buying_url=_optional('CLAUDE_MEDIA_BUYING_URL', 'https://claude.ai/'),
        gemini_media_buying_url=_optional('GEMINI_MEDIA_BUYING_URL', 'https://gemini.google.com/'),
        ignored_folders=_split_csv(os.getenv('IGNORED_FOLDERS', 'Партнери')),
    )
