# -*- coding: utf-8 -*-
"""
Карта на източниците за слоя на събиране (Фаза 2).

Потвърдена на 2026-06-18 чрез `check_feeds.py` (виж SOURCES.md).
10 използваеми източника:
  - 3 през НАТИВНА RSS емисия (минава с обикновена заявка):
      ESPNcricinfo, Wisden, Hindustan Times
  - 7 през GOOGLE NEWS посредник (нативната липсва или връща 403):
      Times of India, Indian Express, News18, NDTV Sports,
      Cricket World, Cricbuzz, ICC

Този слой няма нужда от API ключове — RSS и Google News са безплатни.
"""


def google_news(domain, query="cricket"):
    """RSS емисия от Google News, ограничена до конкретен домейн.

    Линковете в тази емисия са ОБВИТИ (news.google.com/...) и изискват
    една стъпка на разрешаване до истинския URL — виж collect.resolve_google_news.
    """
    return (f"https://news.google.com/rss/search?"
            f"q={query}+site:{domain}&hl=en-IN&gl=IN&ceid=IN:en")


# Всеки източник: (име, адрес на емисията, дали линковете са от Google News).
# Подреждаме нативните първи, после посредниците — само за по-четим изход.
SOURCES = [
    # --- А) Нативна RSS емисия ---
    ("ESPNcricinfo",
     "https://www.espncricinfo.com/rss/content/story/feeds/0.xml",
     False),
    ("Wisden",
     "https://www.wisden.com/feed",
     False),
    ("Hindustan Times",
     "https://www.hindustantimes.com/feeds/rss/cricket/rssfeed.xml",
     False),

    # --- Б) През Google News посредник ---
    ("Times of India", google_news("timesofindia.indiatimes.com"), True),
    ("Indian Express", google_news("indianexpress.com"), True),
    ("News18", google_news("news18.com"), True),
    ("NDTV Sports", google_news("sports.ndtv.com"), True),
    ("Cricket World", google_news("cricketworld.com"), True),
    ("Cricbuzz", google_news("cricbuzz.com"), True),
    ("ICC", google_news("icc-cricket.com"), True),
]
