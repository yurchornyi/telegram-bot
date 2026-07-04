from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.ai import AIService, filter_general_report_messages, is_market_overview_question
from src.config import load_config


NOW = datetime.now(timezone.utc).isoformat()


def msg(message_id: int, chat: str, text: str, link: str) -> dict:
    return {
        "message_id": message_id,
        "chat_id": abs(hash(chat)) % 1_000_000,
        "chat_title": chat,
        "chat_type": "channel",
        "sender_id": message_id,
        "sender_name": "tester",
        "date": NOW,
        "message_link": link,
        "text": text,
    }


QUALITY_MESSAGES = [
    msg(
        1,
        "Nutra Chat",
        "Thailand nutra joints TikTok Ads CPA $32 AR 54%. Новий преленд, треба тестити сьогодні.",
        "https://t.me/nutra/1",
    ),
    msg(
        2,
        "Nutra Chat",
        "Indonesia nutra weight loss Facebook Ads CPA $25 approve 40%. UGC креатив з болем працює краще.",
        "https://t.me/nutra/2",
    ),
    msg(
        3,
        "Ads Problems",
        "Facebook Ads: БМ не проходять оплату, карти відвалюються після першого білінгу. Пробують інші карти і прогрів.",
        "https://t.me/ads/3",
    ),
    msg(
        4,
        "Jobs",
        "Вакансія Media Buyer Gambling salary $3000 remote, пишіть HR.",
        "https://t.me/jobs/4",
    ),
    msg(
        5,
        "Webinar",
        "Безкоштовний вебінар для новачків, рефералка і курс.",
        "https://t.me/webinar/5",
    ),
    msg(
        6,
        "Crypto Chat",
        "Crypto gambling зараз без конкретних цифр, тільки обговорюють що TikTok банить креативи.",
        "https://t.me/crypto/6",
    ),
]


def assert_not_contains(text: str, forbidden: list[str], label: str):
    lowered = text.casefold()
    leaked = [word for word in forbidden if word.casefold() in lowered]
    if leaked:
        raise AssertionError(f"{label}: forbidden words leaked: {leaked}\n{text}")


def assert_contains_any(text: str, required: list[str], label: str):
    lowered = text.casefold()
    if not any(word.casefold() in lowered for word in required):
        raise AssertionError(f"{label}: none of required markers found: {required}\n{text}")


def assert_source_for_mentioned_facts(text: str, label: str):
    checks = [
        ("Thailand", "https://t.me/nutra/1"),
        ("Indonesia", "https://t.me/nutra/2"),
        ("Facebook", "https://t.me/ads/3"),
    ]
    for marker, source in checks:
        if marker.casefold() in text.casefold() and source not in text:
            raise AssertionError(f"{label}: mentioned {marker} without source {source}\n{text}")


def offline_checks():
    assert is_market_overview_question("шо сьогодні було важливо")
    assert is_market_overview_question("шо заливають по суті без вакансій")

    filtered = filter_general_report_messages(QUALITY_MESSAGES)
    texts = "\n".join(item["text"] for item in filtered)
    assert "Вакансія" not in texts
    assert "вебінар" not in texts.casefold()
    assert "Thailand nutra" in texts
    assert "Facebook Ads" in texts
    print("offline_checks: OK")


async def live_checks(max_cases: int):
    cfg = load_config()
    ai = AIService(
        cfg.gemini_api_keys,
        cfg.gemini_model,
        cfg.max_input_chars,
        groq_api_keys=cfg.groq_api_keys,
        groq_models=cfg.groq_models,
        openrouter_api_keys=cfg.openrouter_api_keys,
        openrouter_models=cfg.openrouter_models,
    )

    cases = [
        (
            "overview",
            "шо сьогодні було важливо",
            QUALITY_MESSAGES,
            ["nutra", "facebook", "https://t.me/"],
            ["ваканс", "вебінар", "реферал"],
        ),
        (
            "pouring",
            "шо заливають по суті без вакансій",
            QUALITY_MESSAGES,
            ["Thailand", "Indonesia", "CPA", "https://t.me/"],
            ["ваканс", "вебінар", "реферал"],
        ),
        (
            "single_choice",
            "дай одне що тестити першим",
            QUALITY_MESSAGES[:3],
            ["Thailand", "nutra", "https://t.me/nutra/1"],
            ["ваканс", "вебінар", "реферал", "без посилання"],
        ),
        (
            "no_data",
            "що важливого по ecom сьогодні",
            [QUALITY_MESSAGES[4]],
            ["немає", "конкретики"],
            ["CPA $", "AR ", "виглядає перспективно"],
        ),
    ][:max_cases]

    for label, question, messages, required, forbidden in cases:
        answer = await ai.ask(question, messages)
        print(f"\n===== {label}: {question} =====")
        print(answer[:1800])
        assert_contains_any(answer, required, label)
        assert_not_contains(answer, forbidden, label)
        assert_source_for_mentioned_facts(answer, label)

    print(f"\nlive_checks: OK ({len(cases)} cases)")


async def main():
    parser = argparse.ArgumentParser(description="AI quality smoke test for the Telegram bot.")
    parser.add_argument("--live", action="store_true", help="Run real Gemini/Groq calls.")
    parser.add_argument("--max-cases", type=int, default=2, help="Limit live AI calls. Default: 2.")
    args = parser.parse_args()

    offline_checks()
    if args.live:
        await live_checks(max(1, min(args.max_cases, 4)))
    else:
        print("live_checks: skipped. Run with --live after adding fresh keys.")


if __name__ == "__main__":
    asyncio.run(main())
