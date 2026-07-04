#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Слой за събиране (Фаза 2).

За всеки от 10-те източника от `sources/feeds.py` изтегля заглавие + URL чрез
`feedparser` — нативна RSS емисия там, където минава, иначе Google News посредник.

Ключови свойства (виж README, Фаза 2):
  - Всеки източник е в собствен try/except — счупен източник НЕ сваля останалите.
  - Обвитите Google News линкове се разрешават до истинския URL на статията.
  - Единен изход: списък от речници {source, headline, url, published}.

Този слой няма нужда от API ключове — RSS и Google News са безплатни.

Стартиране (печата по няколко статии от всеки източник):
    python -m src.collect
"""

import json
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import quote, urlparse

try:
    import requests
    import feedparser
except ImportError:
    print("Липсват зависимости. Изпълни:  pip install -r requirements.txt")
    sys.exit(1)

# Windows конзолата често е cp1251 и не може да изпише ✓ или кирилица —
# подсигуряваме UTF-8 изход, за да не пада скриптът при печат.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

try:
    from .sources.feeds import SOURCES
    from .retry import retry_call
except ImportError:  # позволява и директно `python src/collect.py`
    from sources.feeds import SOURCES
    from retry import retry_call


# Браузърска заглавка — за да приличаме на нормален посетител, не на бот.
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

# От ЕС IP (както при локалната разработка) Google показва стена за съгласие
# (consent.google.com) и крие страницата на статията. Бисквитката SOCS я
# прескача и ни дава директно съдържанието. (В GitHub Actions, US IP, стената
# обикновено липсва — но бисквитката не пречи и там.)
_session = requests.Session()
_session.headers.update(HEADERS)
_session.cookies.set("SOCS", "CAESEwgDEgk0ODE3Nzk3MjQaAmVuIAEaBgiA_LyaBg",
                     domain=".google.com")

FEED_TIMEOUT = 20      # секунди за изтегляне на една емисия
RESOLVE_TIMEOUT = 20   # секунди за разрешаване на един Google News линк
GOOGLE_NEWS_DELAY = 1.5  # пауза преди всяка Google News заявка — 7 бързи
                         # заявки от един датацентър IP приличат на бот burst
                         # (и предизвикват 503); малката пауза ги разрежда


def _entry_date(entry):
    """ISO дата на статията (UTC), или None ако емисията не дава дата."""
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    if not t:
        return None
    return datetime(*t[:6], tzinfo=timezone.utc).isoformat()


def fetch_source(name, url, is_google_news):
    """Изтегля една емисия и я връща като списък от единни речници.

    Мрежовата заявка е обвита в retry (преходни грешки — Google News 503,
    connection/timeout — се повтарят до 3 пъти; 4xx освен 429 се провалят
    веднага). Не лови изключения сам — обвиването в try/except става в
    collect_all, за да остане изолацията на отказите на едно място.
    """
    def _get():
        resp = requests.get(url, headers=HEADERS, timeout=FEED_TIMEOUT)
        resp.raise_for_status()   # 5xx/429 → преходно; други 4xx → fail fast
        return resp

    resp = retry_call(_get, label=f"feed {name}")

    parsed = feedparser.parse(resp.content)
    if not parsed.entries:
        raise ValueError("0 статии (не е валиден feed?)")

    articles = []
    for entry in parsed.entries:
        headline = (entry.get("title") or "").strip()
        link = (entry.get("link") or "").strip()
        if not headline or not link:
            continue
        articles.append({
            "source": name,
            "headline": headline,
            "url": link,
            "published": _entry_date(entry),
            # маркер за стъпката на разрешаване по-долу
            "_google_news": is_google_news,
        })
    return articles


# ─────────────────────────────────────────────────────────────────────────
# Разрешаване на обвитите Google News линкове до истинския URL
# ─────────────────────────────────────────────────────────────────────────
#
# Линковете от Google News сочат към news.google.com/... и трябва да се
# „разопаковат“, преди trafilatura (Фаза 3) да изтегли тялото. Подходът е
# на пластове — спираме на първия, който даде истински URL:
#
#   1) Проследяване на пренасочванията (работи за стария формат).
#   2) Декодиране през вътрешния batchexecute endpoint на Google (новия
#      формат CBMi... — линкът вече не е просто пренасочване).
#   3) Резерв: връщаме оригиналния линк, без да чупим конвейера.

_DECODE_ENDPOINT = "https://news.google.com/_/DotsSplashUi/data/batchexecute"


def _looks_resolved(url):
    """True, ако URL вече сочи към истински сайт, а не към google.com.

    Отхвърля и consent.google.com / accounts.google.com — те са стени, не
    статии, затова проверяваме целия домейн google.com, не само news.
    """
    host = urlparse(url).netloc.lower()
    return bool(host) and not (host == "google.com"
                               or host.endswith(".google.com"))


def _decode_via_batchexecute(article_id):
    """Новия формат: вади подпис+timestamp от страницата и пита Google."""
    page = _session.get(
        f"https://news.google.com/rss/articles/{article_id}",
        timeout=RESOLVE_TIMEOUT,
    )
    page.raise_for_status()
    sig = re.search(r'data-n-a-sg="([^"]+)"', page.text)
    ts = re.search(r'data-n-a-ts="([^"]+)"', page.text)
    if not (sig and ts):
        return None

    inner = (
        '["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,'
        'null,null,null,null,null,0,1],"X","X",1,[1,1,1],1,1,null,0,0,null,0],'
        f'"{article_id}",{ts.group(1)},"{sig.group(1)}"]'
    )
    payload = "f.req=" + quote(json.dumps([[["Fbv4je", inner]]]))
    resp = _session.post(
        _DECODE_ENDPOINT, data=payload, timeout=RESOLVE_TIMEOUT,
        headers={"content-type": "application/x-www-form-urlencoded;charset=UTF-8"},
    )
    resp.raise_for_status()
    # Отговорът е „)]}'\n\n<json>“ — намираме реда с garturlres.
    for line in resp.text.splitlines():
        if "garturlres" in line:
            arr = json.loads(line)
            real = json.loads(arr[0][2])[1]
            if _looks_resolved(real):
                return real
    return None


def resolve_google_news(url):
    """Връща истинския URL зад обвит Google News линк (best-effort).

    При всяка пречка връща подадения URL — никога не хвърля изключение,
    за да не блокира една статия целия източник.
    """
    if _looks_resolved(url):
        return url

    # 1) Просто проследяване на пренасочванията (стар формат).
    try:
        r = _session.get(url, timeout=RESOLVE_TIMEOUT, allow_redirects=True)
        if _looks_resolved(r.url):
            return r.url
    except Exception:
        pass

    # 2) Декодиране през batchexecute (нов формат CBMi...).
    path = urlparse(url).path.split("/")
    if len(path) > 2 and path[-2] in ("articles", "read"):
        try:
            real = _decode_via_batchexecute(path[-1])
            if real:
                return real
        except Exception:
            pass

    # 3) Резерв — оригиналният линк.
    return url


def collect_all(resolve_links=True):
    """Минава през всички източници и връща единен списък от статии.

    Всеки източник е в собствен try/except: ако някой се счупи, само той
    се пропуска (с бележка), а останалите продължават.
    """
    all_articles = []
    for name, url, is_gn in SOURCES:
        # Разреждаме последователните Google News заявки, за да не приличат на
        # бот burst от датацентър IP (иначе → 503).
        if is_gn:
            time.sleep(GOOGLE_NEWS_DELAY)
        try:
            articles = fetch_source(name, url, is_gn)
        except Exception as ex:
            print(f"  ✗ {name}: {type(ex).__name__}: {ex}")
            continue

        if resolve_links:
            for art in articles:
                if art.pop("_google_news", False):
                    art["url"] = resolve_google_news(art["url"])
        else:
            for art in articles:
                art.pop("_google_news", None)

        print(f"  ✓ {name}: {len(articles)} статии")
        all_articles.extend(articles)

    return all_articles


def main():
    print("\n=== Слой за събиране (Фаза 2) ===\n")
    # За бърз тест разрешаването на линкове е изключено (то е бавно, защото
    # удря Google по веднъж на статия). Включи го с флага --resolve.
    resolve = "--resolve" in sys.argv
    if not resolve:
        print("(линковете не се разрешават — добави --resolve за пълните URL)\n")

    articles = collect_all(resolve_links=resolve)

    print(f"\nОбщо събрани: {len(articles)} статии "
          f"от {len(SOURCES)} източника.\n")

    # Печатаме до 2 примерни заглавия от всеки източник.
    print("─── Примери по източник ───")
    seen = {}
    for art in articles:
        if seen.get(art["source"], 0) >= 2:
            continue
        seen[art["source"]] = seen.get(art["source"], 0) + 1
        print(f"\n■ {art['source']}  ({art['published'] or 'без дата'})")
        print(f"  {art['headline']}")
        print(f"  {art['url']}")
    print()


if __name__ == "__main__":
    main()
