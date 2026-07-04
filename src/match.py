#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Дедупликация и кръстосано съвпадение (Фаза 4).

Веригата за едно изпълнение:
  1. Взима единния списък статии от `collect.collect_all`.
  2. Намира НОВИТЕ (url, който още не е в таблицата articles).
  3. Embed-ва само новите заглавия с ЕДНО пакетно извикване на
     text-embedding-3-small и ги записва. Старите НЕ се embed-ват наново.
  4. Смята косинусова прилика между всяка нова статия и скорошните статии
     от ДРУГИ източници (прозорец 12h). Праг ≥ 0.8.
  5. Записва новите кръстосани двойки в matches (без дубликати).

Решение (Фаза 4): embeddings през OpenAI text-embedding-3-small.
Нуждае се от OPENAI_API_KEY в `.env` (gitignore-нат, не влиза в Git).

Косинусовата прилика е нарочно проста — numpy, груба сила върху скорошния
прозорец. На този мащаб (стотици статии) няма нужда от векторна база.

Стартиране (collect → match, end-to-end):
    python -m src.match
"""

import os
import sys

import numpy as np

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from dotenv import load_dotenv

try:
    from . import db
    from .collect import collect_all
    from .retry import retry_call
except ImportError:  # позволява и директно `python src/match.py`
    import db
    from collect import collect_all
    from retry import retry_call


EMBED_MODEL = "text-embedding-3-small"
SIM_THRESHOLD = 0.8     # косинусова прилика, над която двойката е „същата новина“
WINDOW_HOURS = 12       # колко назад гледаме за кръстосани съвпадения (Фаза 6: 12h)
MAX_BATCH = 2048        # горна граница на входовете в едно embeddings извикване

# .env се зарежда веднъж при import — ключът остава само в средата, не в кода.
load_dotenv()


def _client():
    """OpenAI клиент; ясна грешка, ако ключът липсва.

    max_retries=0 — вградените повторения на клиента са изключени, за да е
    нашият retry (`src/retry.py`) единственият механизъм (с видим лог).
    """
    from openai import OpenAI
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError(
            "Липсва OPENAI_API_KEY. Копирай .env.example в .env и сложи ключа.")
    return OpenAI(max_retries=0)


def embed_headlines(headlines):
    """Embed-ва списък заглавия и връща списък вектори в същия ред.

    Едно пакетно извикване (при нужда — на части от по MAX_BATCH). Всяко
    извикване е обвито в retry за преходни OpenAI грешки (APIConnectionError,
    RateLimitError, 5xx); AuthenticationError/BadRequestError се провалят веднага.
    """
    if not headlines:
        return []
    client = _client()
    vectors = []
    for start in range(0, len(headlines), MAX_BATCH):
        chunk = headlines[start:start + MAX_BATCH]
        resp = retry_call(
            lambda: client.embeddings.create(model=EMBED_MODEL, input=chunk),
            label="openai embeddings")
        # подреждаме по .index за всеки случай — да съвпадне с входа
        for item in sorted(resp.data, key=lambda d: d.index):
            vectors.append(item.embedding)
    return vectors


def _normalize(matrix):
    """Нормализира редовете до единична дължина (за косинус през точково произв.)."""
    mat = np.asarray(matrix, dtype=np.float64)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms


def run_matching(articles, db_path=db.DB_PATH, window_hours=WINDOW_HOURS,
                 threshold=SIM_THRESHOLD):
    """Изпълнява дедупликацията + съвпадението върху подадените статии.

    Връща речник с отчет: брой нови embed-нати, общо статии, нови двойки.
    """
    conn = db.connect(db_path)
    seen = db.existing_urls(conn)

    # Намираме новите — пропускаме и дубликати в самата партида (един url
    # може да се появи два пъти в емисиите).
    new_articles, batch_seen = [], set()
    for art in articles:
        url = art["url"]
        if url in seen or url in batch_seen:
            continue
        batch_seen.add(url)
        new_articles.append(art)

    # Embed-ваме само новите — старите вектори вече са в базата.
    new_ids = set()
    if new_articles:
        vectors = embed_headlines([a["headline"] for a in new_articles])
        for art, vec in zip(new_articles, vectors):
            new_ids.add(db.insert_article(conn, art, vec))
        conn.commit()

    # Кръстосана прилика върху скорошния прозорец (включва и новите).
    recent = db.recent_articles(conn, window_hours)
    found = []
    if new_ids and len(recent) > 1:
        vecs = _normalize([r["embedding"] for r in recent])
        for i, item in enumerate(recent):
            if item["id"] not in new_ids:
                continue  # сравняваме само НОВИТЕ срещу прозореца
            sims = vecs @ vecs[i]
            for j, other in enumerate(recent):
                if other["id"] == item["id"] or other["source"] == item["source"]:
                    continue  # без себе си и без същия източник
                sim = float(sims[j])
                if sim >= threshold and db.record_match(
                        conn, item["id"], other["id"], sim):
                    found.append((item, other, sim))
        conn.commit()

    total_articles, total_matches = db.counts(conn)
    conn.close()
    return {
        "embedded": len(new_articles),
        "new_matches": found,
        "total_articles": total_articles,
        "total_matches": total_matches,
    }


def main():
    print("\n=== Дедупликация и съвпадение (Фаза 4) ===\n")

    # Не разрешаваме линковете тук — дедупликацията е по url (обвитият Google
    # News линк е стабилен идентификатор), а разрешаването до истинския URL
    # се прави по-късно само за съвпадащите двойки (Фаза 3/5), не за всички.
    print("Събиране на статии (collect)…")
    articles = collect_all(resolve_links=False)
    print(f"\nСъбрани: {len(articles)} статии.\n")

    print("Дедупликация + embeddings + прилика (match)…")
    report = run_matching(articles)

    print(f"\n── Отчет ──")
    print(f"Нови embed-нати заглавия : {report['embedded']}")
    print(f"Общо статии в базата     : {report['total_articles']}")
    print(f"Нови кръстосани двойки   : {len(report['new_matches'])}")
    print(f"Общо двойки в базата     : {report['total_matches']}")

    if report["new_matches"]:
        print(f"\n── Нови съвпадения (≥ {SIM_THRESHOLD}) ──")
        for a, b, sim in sorted(report["new_matches"],
                                key=lambda t: t[2], reverse=True):
            print(f"\n  прилика {sim:.3f}")
            print(f"    [{a['source']}] {a['headline']}")
            print(f"    [{b['source']}] {b['headline']}")
    print()


if __name__ == "__main__":
    main()
