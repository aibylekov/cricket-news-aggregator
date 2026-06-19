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

Връща (body, status, resolved_url):
  - успех:  (текст, "ok", истински_url)
  - провал: (None, причина, истински_url) — причината е една от
            403 | empty | timeout | unresolved | http_XXX | error

Телата се пазят в базата (колоните body* в articles), за да НЕ се теглят
повторно — на всеки цикъл се обработват само статии в съвпадение без тяло.

ВАЖНО (Фаза 3): тук НЯМА платена fetch/render услуга — само `trafilatura`.
Целта на този скрипт е да покаже точно кои сайтове се провалят, за да изберем
резерва на база реални данни (виж отчета накрая).

Стартиране (тегли телата за текущите съвпадащи двойки + отчет):
    python -m src.extract
    python -m src.extract --retry-failed   # повтаря и провалените опити
"""

import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

try:
    import requests
    import trafilatura
except ImportError:
    print("Липсват зависимости. Изпълни:  pip install -r requirements.txt")
    sys.exit(1)

try:
    from . import db
    from .collect import HEADERS, resolve_google_news, _looks_resolved
except ImportError:  # позволява и директно `python src/extract.py`
    import db
    from collect import HEADERS, resolve_google_news, _looks_resolved


FETCH_TIMEOUT = 25      # секунди за теглене на една статия
MIN_BODY_CHARS = 200    # под това смятаме извлеченото за празно/невалидно
POLITE_DELAY = 0.7      # пауза между тегленията — да не чукаме сайтовете лудо

# Собствена сесия с браузърска заглавка (резолюцията на Google ползва своята).
_session = requests.Session()
_session.headers.update(HEADERS)


def extract_body(url):
    """Тегли и извлича тялото за един URL. Виж модулния docstring за изхода."""
    real_url = resolve_google_news(url)  # без-операция за вече истински URL

    # Ако линкът си остана към google.com — резолюцията не е минала.
    if not _looks_resolved(real_url):
        return None, "unresolved", real_url

    try:
        resp = _session.get(real_url, timeout=FETCH_TIMEOUT, allow_redirects=True)
    except requests.Timeout:
        return None, "timeout", real_url
    except requests.RequestException:
        return None, "error", real_url

    if resp.status_code == 403:
        return None, "403", real_url
    if resp.status_code != 200:
        return None, f"http_{resp.status_code}", real_url

    text = trafilatura.extract(
        resp.text, url=real_url,
        include_comments=False, include_tables=False,
        favor_precision=True,
    )
    if not text or len(text.strip()) < MIN_BODY_CHARS:
        return None, "empty", real_url

    return text.strip(), "ok", real_url


def run_extraction(db_path=db.DB_PATH, retry_failed=False, delay=POLITE_DELAY):
    """Извлича телата за статиите в съвпадение без тяло и връща отчета."""
    conn = db.connect(db_path)
    todo = db.articles_needing_body(conn, retry_failed=retry_failed)

    print(f"Статии в съвпадение за извличане: {len(todo)}\n")
    for i, art in enumerate(todo, 1):
        body, status, real_url = extract_body(art["url"])
        db.save_body(conn, art["id"], body, status, real_url)
        conn.commit()
        mark = "✓" if status == "ok" else "✗"
        size = f"{len(body)} симв." if body else status
        print(f"  {mark} [{art['source']}] {size}  — {art['headline'][:60]}")
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
        print("\n  → Сайтовете с провали са кандидатите за fetch/render резерв.")


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
