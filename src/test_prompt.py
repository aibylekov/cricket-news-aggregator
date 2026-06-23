#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Проба на синтез-промпта — преизползваем инструмент (само за четене).

Зарежда системния промпт от `prompts/synthesis_system_prompt.md`, взима
няколко разнородни вече-съвпаднали двойки от базата и за всяка праща двете
тела като потребителско съобщение (с етикети SOURCE A: / SOURCE B:) на избран
модел. Печата изхода на модела (статия + --- + EDITOR NOTES), за да сравняваме
качеството на промпта между модели и версии.

НЕ пише в базата. Прави по едно извикване на двойка (евтино).

Параметри:
    --model MODEL        кой модел да ползваме (по подразбиране gpt-4.1-mini)
    --pairs 36,13,49,5   конкретни match id-та (за честно сравнение между пускания)
    --temperature 0.7    (модели, които не я поддържат, се пускат без нея)
    --max-tokens 3000    таван на изхода (с резерв за reasoning модели)

Примери:
    python -m src.test_prompt --model gpt-4.1-mini
    python -m src.test_prompt --model gpt-5.4-mini
    python -m src.test_prompt --model gpt-4.1-mini --pairs 36,13,49
"""

import argparse
import os
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

try:
    from dotenv import load_dotenv
except ImportError:
    print("Липсват зависимости. Изпълни:  pip install -r requirements.txt")
    sys.exit(1)

try:
    from . import db
except ImportError:  # позволява и директно `python src/test_prompt.py`
    import db


load_dotenv()

DEFAULT_MODEL = "gpt-4.1-mini"
DEFAULT_TEMPERATURE = 0.7   # промптът залага на „човешки глас“ — даваме малко свобода
DEFAULT_MAX_TOKENS = 3000   # статия + EDITOR NOTES; резерв и за reasoning модели

PROMPT_FILE = Path(__file__).resolve().parent.parent / "prompts" \
    / "synthesis_system_prompt.md"

# Подразбиращ се разнороден набор — същите id-та като при първото пускане, за да
# са пряко сравними резултатите. Всяка двойка натоварва различна част от промпта.
DEFAULT_PAIRS = [36, 13, 49, 5]
PAIR_LABELS = {
    36: "чиста двойка (почти идентични източници)",
    13: "конфликт/детайл (тестов мач, фигури само в единия)",
    49: "къса двойка (най-малки тела — тест за дисциплина на дължината)",
    5:  "директни цитати (Sreesanth/Harbhajan — тест за вярност на цитата)",
}


def load_system_prompt(path=PROMPT_FILE):
    """Изважда съдържанието на ПЪРВИЯ ограден код-блок (```) от .md файла."""
    text = Path(path).read_text(encoding="utf-8")
    blocks = re.findall(r"```(.*?)```", text, re.DOTALL)
    if not blocks:
        raise ValueError(f"Няма код-блок в {path}")
    return blocks[0].strip("\n")


def get_pair(conn, match_id):
    """Връща двойката (тела, заглавия, източници, прилика) по match_id."""
    return conn.execute(
        "SELECT m.id AS mid, m.similarity AS sim, "
        "  a.source AS a_source, a.headline AS a_headline, a.body AS a_body, "
        "  b.source AS b_source, b.headline AS b_headline, b.body AS b_body "
        "FROM matches m "
        "JOIN articles a ON a.id = m.article_a_id "
        "JOIN articles b ON b.id = m.article_b_id "
        "WHERE m.id = ?",
        (match_id,),
    ).fetchone()


def build_user_message(pair):
    """Двете тела като потребителско съобщение, по формата от промпт-файла."""
    return (f"SOURCE A:\n{pair['a_body']}\n\n"
            f"SOURCE B:\n{pair['b_body']}")


def _client():
    from openai import OpenAI
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError(
            "Липсва OPENAI_API_KEY. Копирай .env.example в .env и сложи ключа.")
    return OpenAI()


def call_model(client, model, system, user, temperature, max_tokens):
    """Извиква chat.completions устойчиво към разликите между моделите.

    По-новите модели (напр. gpt-5.x) искат `max_completion_tokens` вместо
    `max_tokens` и често приемат само temperature по подразбиране. Пробваме
    комбинациите в ред и при грешка за параметър отпадаме към следващата.
    """
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    base = {"model": model, "messages": messages}
    attempts = [
        {**base, "temperature": temperature, "max_completion_tokens": max_tokens},
        {**base, "max_completion_tokens": max_tokens},          # без temperature
        {**base, "temperature": temperature, "max_tokens": max_tokens},  # legacy
        {**base, "max_tokens": max_tokens},
    ]
    param_hints = ("temperature", "max_tokens", "max_completion_tokens",
                   "unsupported", "not supported", "param")
    last_err = None
    for kwargs in attempts:
        try:
            return client.chat.completions.create(**kwargs)
        except Exception as ex:
            last_err = ex
            if any(h in str(ex).lower() for h in param_hints):
                continue  # разлика в параметрите — пробвай следващата комбинация
            raise
    raise last_err


def run(model, pairs, temperature, max_tokens):
    system = load_system_prompt()
    conn = db.connect()
    client = _client()

    print("\n" + "█" * 78)
    print(f"  МОДЕЛ: {model}    |    двойки: {pairs}")
    print(f"  системен промпт: {len(system)} символа от {PROMPT_FILE.name}")
    print("█" * 78)

    for match_id in pairs:
        pair = get_pair(conn, match_id)
        label = PAIR_LABELS.get(match_id, "")
        print("\n" + "═" * 78)
        print(f"  ДВОЙКА #{match_id}" + (f" — {label}" if label else ""))
        print("═" * 78)
        if pair is None:
            print(f"  (match #{match_id} липсва в базата — прескачам)")
            continue

        print(f"  прилика: {pair['sim']:.3f}")
        print(f"  SOURCE A [{pair['a_source']}]: {pair['a_headline']}")
        print(f"  SOURCE B [{pair['b_source']}]: {pair['b_headline']}")
        print("─" * 78)

        try:
            resp = call_model(client, model, system,
                              build_user_message(pair), temperature, max_tokens)
        except Exception as ex:
            print(f"  ✗ грешка от модела: {type(ex).__name__}: {ex}")
            continue

        content = (resp.choices[0].message.content or "").strip()
        print(content if content else "  (празен отговор от модела)")
        print()

    conn.close()


def _parse_pairs(raw):
    if not raw:
        return list(DEFAULT_PAIRS)
    return [int(x) for x in raw.replace(" ", "").split(",") if x]


def main():
    parser = argparse.ArgumentParser(
        description="Проба на синтез-промпта върху фиксиран набор двойки.")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help=f"модел (по подразбиране {DEFAULT_MODEL})")
    parser.add_argument("--pairs", default=None,
                        help="match id-та през запетая (по подразбиране: "
                             f"{','.join(map(str, DEFAULT_PAIRS))})")
    parser.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE)
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    args = parser.parse_args()

    run(args.model, _parse_pairs(args.pairs), args.temperature, args.max_tokens)


if __name__ == "__main__":
    main()
