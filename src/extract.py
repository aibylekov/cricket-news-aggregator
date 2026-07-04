#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Извличане на телата (Фаза 3).

За статия в съвпадение взима чистото тяло на материала с `trafilatura`:
  1. Ако URL-ът е обвит Google News линк — разрешава го до истинския URL на
     издателя (преизползва логиката от Фаза 2, `collect.resolve_google_news`;
     за вече истински URL това е без-операция).
  2. Тегли страницата с браузърски User-Agent (същият като при емисиите).
  3. Подава HTML-а на `trafilatura.extract` за чистия текст.

Резерв за анти-бот блокираните сайтове (Zyte API):
  - Безплатният път (trafilatura + браузърски UA) е основният и се ползва за
    всички сайтове, които минават (TOI, HT, Cricbuzz, Cricket World, Wisden…).
  - Само за известните блокирани сайтове (News18, NDTV, ESPNcricinfo, Indian
    Express) минаваме ДИРЕКТНО през Zyte — плейн заявката при тях връща 403,
    тоест е напразна. За всеки друг сайт, който неочаквано върне 403, също
    ескалираме към Zyte. Така платените заявки са само където трябва.
  - Zyte се ползва в режим httpResponseBody (анти-бот прокси, без браузърски
    рендер — по-евтино и достатъчно за тези server-rendered страници).
  - Ключът ZYTE_API_KEY се чете от `.env` (gitignore-нат).

Връща (body, status, resolved_url):
  - успех:  (текст, "ok", истински_url)
  - провал: (None, причина, истински_url) — причината е една от
            403 | empty | timeout | unresolved | http_XXX | error |
            zyte_empty | zyte_nokey | zyte_timeout | zyte_error | zyte_http_XXX

Телата се пазят в базата (колоните body* в articles), за да НЕ се теглят
повторно — на всеки цикъл се обработват само статии в съвпадение без тяло.

Стартиране (тегли телата за текущите съвпадащи двойки + отчет):
    python -m src.extract
    python -m src.extract --retry-failed   # повтаря и провалените опити
"""

import base64
import os
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

try:
    import requests
    import trafilatura
    from dotenv import load_dotenv
except ImportError:
    print("Липсват зависимости. Изпълни:  pip install -r requirements.txt")
    sys.exit(1)

try:
    from . import db
    from .collect import HEADERS, resolve_google_news, _looks_resolved
    from .retry import retry_call
except ImportError:  # позволява и директно `python src/extract.py`
    import db
    from collect import HEADERS, resolve_google_news, _looks_resolved
    from retry import retry_call

# .env се зарежда веднъж при import — ключът остава само в средата, не в кода.
load_dotenv()

FETCH_TIMEOUT = 25      # секунди за теглене на една статия
MIN_BODY_CHARS = 200    # под това смятаме извлеченото за празно/невалидно
POLITE_DELAY = 0.7      # пауза между тегленията — да не чукаме сайтовете лудо

# Известни анти-бот сайтове — плейн заявката при тях връща 403, затова минаваме
# директно през Zyte (пести един напразен опит на цикъл). Виж SOURCES.md.
ZYTE_SOURCES = {"News18", "NDTV Sports", "ESPNcricinfo", "Indian Express"}
ZYTE_ENDPOINT = "https://api.zyte.com/v1/extract"
ZYTE_TIMEOUT = 90       # анти-бот прокси може да отнеме време
ZYTE_API_KEY = os.getenv("ZYTE_API_KEY")

# Собствена сесия с браузърска заглавка (резолюцията на Google ползва своята).
_session = requests.Session()
_session.headers.update(HEADERS)


def _clean(html, url):
    """Прекарва HTML през trafilatura; връща чистото тяло или None."""
    text = trafilatura.extract(
        html, url=url,
        include_comments=False, include_tables=False,
        favor_precision=True,
    )
    if not text or len(text.strip()) < MIN_BODY_CHARS:
        return None
    return text.strip()


def _fetch_plain(real_url):
    """Безплатният път: requests + браузърски UA. Връща (body, status).

    Заявката е с retry за преходни грешки (5xx/429/connection/timeout). 403
    НЕ се повтаря — връща се веднага, за да ескалира към Zyte.
    """
    def _get():
        resp = _session.get(real_url, timeout=FETCH_TIMEOUT, allow_redirects=True)
        if resp.status_code == 429 or resp.status_code >= 500:
            resp.raise_for_status()   # преходно → retry
        return resp

    try:
        resp = retry_call(_get, label=f"body {real_url[:50]}")
    except requests.Timeout:
        return None, "timeout"
    except requests.RequestException:
        return None, "error"

    if resp.status_code == 403:
        return None, "403"
    if resp.status_code != 200:
        return None, f"http_{resp.status_code}"

    body = _clean(resp.text, real_url)
    return (body, "ok") if body else (None, "empty")


def _fetch_zyte(real_url):
    """Резервът: Zyte API (httpResponseBody). Връща (body, status).

    Заявката е с retry за преходни Zyte грешки (5xx/429/connection/timeout);
    други статуси (напр. 403 при проблем с ключа) се провалят веднага.
    """
    if not ZYTE_API_KEY:
        return None, "zyte_nokey"

    def _post():
        resp = requests.post(
            ZYTE_ENDPOINT, auth=(ZYTE_API_KEY, ""),
            json={"url": real_url, "httpResponseBody": True},
            timeout=ZYTE_TIMEOUT,
        )
        if resp.status_code == 429 or resp.status_code >= 500:
            resp.raise_for_status()   # преходно → retry
        return resp

    try:
        resp = retry_call(_post, label=f"zyte {real_url[:50]}")
    except requests.Timeout:
        return None, "zyte_timeout"
    except requests.RequestException:
        return None, "zyte_error"

    if resp.status_code != 200:
        return None, f"zyte_http_{resp.status_code}"
    try:
        b64 = resp.json().get("httpResponseBody")
    except ValueError:
        return None, "zyte_error"
    if not b64:
        return None, "zyte_empty"

    html = base64.b64decode(b64).decode("utf-8", "replace")
    body = _clean(html, real_url)
    return (body, "ok") if body else (None, "zyte_empty")


def extract_body(url, source=None):
    """Тегли и извлича тялото за един URL. Виж модулния docstring за изхода.

    Маршрутизация:
      - известен блокиран източник → директно Zyte (плейн заявката е напразна);
      - иначе → плейн път, а при 403 ескалираме към Zyte;
      - работещите сайтове остават само на плейн пътя (без Zyte разход).
    """
    real_url = resolve_google_news(url)  # без-операция за вече истински URL

    # Ако линкът си остана към google.com — резолюцията не е минала.
    if not _looks_resolved(real_url):
        return None, "unresolved", real_url

    if source in ZYTE_SOURCES:
        body, status = _fetch_zyte(real_url)
        return body, status, real_url

    body, status = _fetch_plain(real_url)
    if status == "403":  # неочаквано блокиран сайт — ескалираме към Zyte
        body, status = _fetch_zyte(real_url)
    return body, status, real_url


def run_extraction(db_path=db.DB_PATH, retry_failed=False, delay=POLITE_DELAY):
    """Извлича телата за статиите в съвпадение без тяло и връща отчета."""
    conn = db.connect(db_path)
    todo = db.articles_needing_body(conn, retry_failed=retry_failed)

    print(f"Статии в съвпадение за извличане: {len(todo)}\n")
    for i, art in enumerate(todo, 1):
        body, status, real_url = extract_body(art["url"], art["source"])
        db.save_body(conn, art["id"], body, status, real_url)
        conn.commit()
        mark = "✓" if status == "ok" else "✗"
        size = f"{len(body)} симв." if body else status
        via = " (zyte)" if art["source"] in ZYTE_SOURCES else ""
        print(f"  {mark} [{art['source']}]{via} {size}  — {art['headline'][:55]}")
        if i < len(todo) and delay:
            time.sleep(delay)

    report = db.body_report(conn)
    conn.close()
    return report


def print_report(report):
    """Печата разбивката по източник: успехи срещу провали + причината."""
    print("\n── Отчет за телата (по източник) ──")
    print("(само статии, които участват в съвпадение)\n")

    totals = {}
    print(f"  {'Източник':<18} {'ok':>3}  провали (причина × брой)")
    print(f"  {'-'*18} {'-'*3}  {'-'*30}")
    for source in sorted(report):
        stats = report[source]
        ok = stats.get("ok", 0)
        fails = {k: v for k, v in stats.items() if k != "ok"}
        for k, v in stats.items():
            totals[k] = totals.get(k, 0) + v
        fail_str = ", ".join(f"{k}×{v}" for k, v in sorted(fails.items())) or "—"
        print(f"  {source:<18} {ok:>3}  {fail_str}")

    ok_total = totals.pop("ok", 0)
    grand = ok_total + sum(totals.values())
    fail_total = ", ".join(f"{k}×{v}" for k, v in sorted(totals.items())) or "—"
    print(f"\n  Общо: {ok_total}/{grand} успешни.  Провали: {fail_total}")

    if totals:
        print("\n  → Останалите провали не са анти-бот (напр. empty = видео/"
              "highlights без текст; unresolved = неразрешен Google линк).")


def main():
    print("\n=== Извличане на телата (Фаза 3) ===\n")
    retry = "--retry-failed" in sys.argv
    if retry:
        print("(--retry-failed: повтаряме и провалените опити)\n")
    report = run_extraction(retry_failed=retry)
    print_report(report)
    print()


if __name__ == "__main__":
    main()
