#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Проверка на RSS емисиите за крикет агрегатора.

За всеки източник тества списък с кандидат-адреси (нативна емисия първо,
после Google News посредник) и казва за всеки:
  - жив ли е и валиден XML ли е
  - колко статии съдържа
  - колко е свежа най-новата (актуална / остаряла)

Пуска се ЛОКАЛНО от твоята машина (домашен IP), за да не те блокират
като датацентър. Това е финалната проверка на Фаза 1.

Инсталация:  pip install feedparser requests
Стартиране:  python check_feeds.py

────────────────────────────────────────────────────────────────────────
Референция — новинарски секции (за по-късно, при изтегляне на телата):
  ESPNcricinfo    https://www.espncricinfo.com/cricket-news
  Times of India  https://timesofindia.indiatimes.com/sports/cricket
  Indian Express  https://indianexpress.com/section/sports/cricket/
  News18          https://www.news18.com/cricket/
  Wisden          https://www.wisden.com/cricket-news
  Hindustan Times https://www.hindustantimes.com/cricket
  NDTV Sports     https://sports.ndtv.com/cricket/news
  Cricket World   https://www.cricketworld.com/latest-cricket-news/
  Cricbuzz        https://www.cricbuzz.com/cricket-news
  ICC             https://www.icc-cricket.com/news
  Crex            https://crex.com/news
────────────────────────────────────────────────────────────────────────
"""

import sys
from datetime import datetime, timezone, timedelta

try:
    import requests
    import feedparser
except ImportError:
    print("Липсват зависимости. Изпълни:  pip install feedparser requests")
    sys.exit(1)

# Браузърска заглавка — за да приличаме на нормален посетител, не на бот.
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

STALE_DAYS = 7  # най-нова статия по-стара от това -> маркира се като ОСТАРЯЛА


def google_news(domain, query="cricket"):
    """RSS емисия от Google News, ограничена до конкретен домейн."""
    return (f"https://news.google.com/rss/search?"
            f"q={query}+site:{domain}&hl=en-IN&gl=IN&ceid=IN:en")


# За всеки източник: кандидат-адреси по приоритет (нативен -> Google News).
# Скриптът пробва всички и показва кои работят.
SOURCES = {
    "ESPNcricinfo": [
        "https://www.espncricinfo.com/rss/content/story/feeds/0.xml",
        google_news("espncricinfo.com"),
    ],
    "Times of India": [
        # TODO: ако имаш точния нативен TOI крикет адрес от ръчния тест, сложи го тук
        google_news("timesofindia.indiatimes.com"),
    ],
    "Indian Express": [
        "https://indianexpress.com/section/sports/cricket/feed/",
        google_news("indianexpress.com"),
    ],
    "News18": [
        "https://www.news18.com/commonfeeds/v1/eng/rss/cricket.xml",
        google_news("news18.com"),
    ],
    "Wisden": [
        "https://www.wisden.com/cricket-news/feed/",
        "https://www.wisden.com/feed",
        google_news("wisden.com"),
    ],
    "NDTV Sports": [
        "https://sports.ndtv.com/rss/cricket",
        google_news("sports.ndtv.com"),
    ],
    "Hindustan Times": [
        "https://www.hindustantimes.com/feeds/rss/cricket/rssfeed.xml",
        google_news("hindustantimes.com"),
    ],
    "Cricket World": [
        google_news("cricketworld.com"),
    ],
    "Cricbuzz": [
        google_news("cricbuzz.com"),
    ],
    "ICC": [
        google_news("icc-cricket.com"),
    ],
    "Crex": [
        google_news("crex.com"),
    ],
}


def newest_entry_date(parsed):
    """datetime на най-новата статия в емисията, или None."""
    dates = []
    for e in parsed.entries:
        t = e.get("published_parsed") or e.get("updated_parsed")
        if t:
            dates.append(datetime(*t[:6], tzinfo=timezone.utc))
    return max(dates) if dates else None


def check_url(url):
    """Тества един адрес и връща резултата като речник."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
    except Exception as ex:
        return {"ok": False, "info": f"{type(ex).__name__}: {ex}"}

    if r.status_code != 200:
        return {"ok": False, "info": f"HTTP {r.status_code}"}

    parsed = feedparser.parse(r.content)
    n = len(parsed.entries)
    if n == 0:
        return {"ok": False, "info": "0 статии (не е валиден feed?)"}

    newest = newest_entry_date(parsed)
    if newest:
        stale = (datetime.now(timezone.utc) - newest) > timedelta(days=STALE_DAYS)
        date_str = newest.strftime("%Y-%m-%d %H:%M")
        freshness = "ОСТАРЯЛА" if stale else "актуална"
    else:
        date_str, freshness = "няма дата", "?"

    title = (parsed.feed.get("title") or "")[:45]
    return {"ok": True, "n": n, "newest": date_str,
            "freshness": freshness, "title": title}


def main():
    print("\n=== Проверка на RSS емисии (Фаза 1) ===\n")
    working = 0
    for source, urls in SOURCES.items():
        print(f"■ {source}")
        source_ok = False
        for url in urls:
            res = check_url(url)
            if res["ok"]:
                source_ok = True
                print(f"  ✓ {res['n']:>3} статии | най-нова: {res['newest']} "
                      f"({res['freshness']}) | {res['title']}")
            else:
                print(f"  ✗ {res['info']}")
            print(f"      {url}")
        working += 1 if source_ok else 0
        print()
    print(f"Резултат: {working} от {len(SOURCES)} източника "
          f"имат поне една жива емисия.\n")


if __name__ == "__main__":
    main()
