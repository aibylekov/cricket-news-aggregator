#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Обединяване на съвпадащите материали (Фаза 5).

За всяка съвпадаща двойка, на която ИЗВЕСТНИ са телата на ДВЕТЕ статии и още
не е обединявана, изпраща двата текста на OpenAI gpt-4.1-mini с молба за НОВ,
ОРИГИНАЛЕН материал — свежо заглавие + свързано тяло, което синтезира фактите
от двата източника със свои думи (не копирано и не леко преписано), неутрален
професионален тон, разумна дължина.

Решение (Фаза 5): обединяване през OpenAI `gpt-4.1-mini`.
Нуждае се от OPENAI_API_KEY в `.env` (gitignore-нат).

Свойства:
  - Идемпотентно: всяка двойка се обединява веднъж (UNIQUE по match_id);
    повторно пускане не праща нови заявки за вече обединените.
  - Дисциплина за цената: заявка тръгва само за нова, готова (с тела) двойка.
  - Устойчивост: двойка без тяло се прескача с бележка, без да чупи цикъла;
    провал на заявка/парсване също се прескача, без да сваля останалите.

Резултатът се пази в таблицата `combined_articles` със статус `draft`.

Стартиране (обединява текущите готови двойки + печата примери):
    python -m src.combine
    python -m src.combine --limit 5   # само първите N (за тест/цена)
"""

import os
import re
import sys

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
except ImportError:  # позволява и директно `python src/combine.py`
    import db


# .env се зарежда веднъж при import — ключът остава само в средата, не в кода.
load_dotenv()

MODEL = "gpt-4.1-mini"
TEMPERATURE = 0.4       # ниско — за вярност към фактите, но малко свобода в езика
MAX_TOKENS = 900        # стига за заглавие + материал с разумна дължина
MAX_BODY_CHARS = 8000   # таван на входа от източник (телата ни са доста под това)


SYSTEM_PROMPT = (
    "You are an experienced sports news editor for an English-language cricket "
    "site. You write a brand-new, original article by synthesising the reporting "
    "from two source articles about the SAME story. You never copy or lightly "
    "reword sentences from the sources — you write fresh, in your own words. You "
    "use ONLY facts that appear in the provided sources and never invent details, "
    "names, numbers or quotes."
)

USER_PROMPT = """Two sources report on the same cricket story. Write ONE new, original article that combines the facts from both.

Requirements:
- A fresh, original headline (do not copy either source's headline).
- A coherent body of reasonable news length (~250-450 words), in your own words.
- Neutral, professional tone. No opinion, no first person, no direct address.
- Synthesise: merge the overlapping facts, fold in the unique details each source adds, and resolve them into one clear narrative.
- Use ONLY facts found in the two sources. Do not fabricate anything.

Output EXACTLY in this format and nothing else:
HEADLINE: <the headline on one line>
BODY:
<the article body>

--- SOURCE 1 ({a_source}) ---
{a_body}

--- SOURCE 2 ({b_source}) ---
{b_body}
"""

_PARSE_RE = re.compile(r"HEADLINE:\s*(.+?)\s*BODY:\s*(.+)", re.IGNORECASE | re.DOTALL)


def _client():
    """OpenAI клиент; ясна грешка, ако ключът липсва."""
    from openai import OpenAI
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError(
            "Липсва OPENAI_API_KEY. Копирай .env.example в .env и сложи ключа.")
    return OpenAI()


def _parse(text):
    """Изважда (headline, body) от структурирания изход; (None, None) при провал."""
    if not text:
        return None, None
    m = _PARSE_RE.search(text)
    if not m:
        return None, None
    headline = m.group(1).strip().strip('"').strip()
    body = m.group(2).strip()
    if not headline or not body:
        return None, None
    return headline, body


def combine_pair(pair, client=None):
    """Праща двете тела на модела и връща (headline, body) или (None, None)."""
    client = client or _client()
    user = USER_PROMPT.format(
        a_source=pair["a_source"], a_body=pair["a_body"][:MAX_BODY_CHARS],
        b_source=pair["b_source"], b_body=pair["b_body"][:MAX_BODY_CHARS],
    )
    resp = client.chat.completions.create(
        model=MODEL, temperature=TEMPERATURE, max_tokens=MAX_TOKENS,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
    )
    return _parse(resp.choices[0].message.content)


def run_combine(db_path=db.DB_PATH, limit=None):
    """Обединява готовите двойки и връща отчет {produced, skipped_*, ...}."""
    conn = db.connect(db_path)
    todo = db.pairs_to_combine(conn)
    skipped_missing = db.count_pairs_missing_body(conn)

    if skipped_missing:
        print(f"Прескочени (липсва тяло на поне една статия): {skipped_missing}\n")
    if limit:
        todo = todo[:limit]

    print(f"Двойки за обединяване: {len(todo)}\n")
    client = _client() if todo else None

    produced, failed = [], 0
    for pair in todo:
        label = f"#{pair['match_id']} [{pair['a_source']} + {pair['b_source']}]"
        try:
            headline, body = combine_pair(pair, client)
        except Exception as ex:
            print(f"  ✗ {label}: грешка от модела — {type(ex).__name__}: {ex}")
            failed += 1
            continue
        if not headline or not body:
            print(f"  ✗ {label}: неуспешно парсване на изхода")
            failed += 1
            continue
        db.save_combined(conn, pair["match_id"], headline, body, MODEL)
        conn.commit()
        produced.append({"pair": pair, "headline": headline, "body": body})
        print(f"  ✓ {label}: {headline}")

    report = {
        "produced": produced,
        "skipped_missing_body": skipped_missing,
        "failed": failed,
        "total_combined": db.combined_count(conn),
    }
    conn.close()
    return report


def _print_samples(produced, n=2):
    """Печата 1–2 обединени материала изцяло — за преценка на качеството."""
    if not produced:
        return
    print(f"\n{'═'*72}\n  ПРИМЕРНИ ОБЕДИНЕНИ МАТЕРИАЛИ (изцяло)\n{'═'*72}")
    for item in produced[:n]:
        p = item["pair"]
        print(f"\n— Източници (прилика {p['similarity']:.3f}):")
        print(f"    [{p['a_source']}] {p['a_headline']}")
        print(f"    [{p['b_source']}] {p['b_headline']}")
        print(f"\n  НОВО ЗАГЛАВИЕ:\n    {item['headline']}\n")
        print(f"  НОВО ТЯЛО:\n")
        for para in item["body"].split("\n"):
            print(f"    {para}")
        print(f"\n{'─'*72}")


def main():
    print("\n=== Обединяване на материалите (Фаза 5) ===\n")
    limit = None
    if "--limit" in sys.argv:
        try:
            limit = int(sys.argv[sys.argv.index("--limit") + 1])
        except (IndexError, ValueError):
            print("Неправилен --limit; игнорирам го.\n")

    report = run_combine(limit=limit)

    _print_samples(report["produced"], n=2)

    print(f"\n── Отчет ──")
    print(f"Произведени този път : {len(report['produced'])}")
    print(f"Неуспешни (модел/парс): {report['failed']}")
    print(f"Прескочени (без тяло) : {report['skipped_missing_body']}")
    print(f"Общо обединени в база : {report['total_combined']}")
    print()


if __name__ == "__main__":
    main()
