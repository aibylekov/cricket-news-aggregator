#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Оркестрация на целия конвейер (Фаза 7).

Едно изпълнение end-to-end, каквото пуска GitHub Actions на всеки ~15 минути:

    collect → match → extract → combine (+ дедуп на събития) → notify (реален send)

Свойства:
  - Състоянието живее в `data/cricket.db`; Actions го commit-ва обратно между
    пусканията, така че нищо не се праща повторно.
  - Предпазни тавани (Фаза 7): MAX_COMBINES_PER_RUN и MAX_SENDS_PER_RUN
    ограничават разхода/имейлите на едно пускане, докато системата е без надзор.
  - При необработена грешка излиза с код 1 → Actions маркира run-а като провален
    (вградено известие) и стъпката за аларма праща имейл.

Стартиране:
    python -m src.pipeline
"""

import sys
import traceback

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

try:
    from . import db, combine, notify
    from .collect import collect_all
    from .match import run_matching
    from .extract import run_extraction
except ImportError:  # позволява и директно `python src/pipeline.py`
    import db, combine, notify
    from collect import collect_all
    from match import run_matching
    from extract import run_extraction


def run():
    """Изпълнява цялата верига веднъж и печата обобщение."""
    print("\n" + "█" * 70)
    print("  КОНВЕЙЕР (Фаза 7) — едно изпълнение")
    print("█" * 70)

    # 1. Събиране + дедупликация/съвпадение (embed само на новите).
    print("\n[1/4] Събиране + съвпадение …")
    articles = collect_all(resolve_links=False)
    match_report = run_matching(articles)

    # 2. Извличане на телата за статиите в НОВИ съвпадения.
    print("\n[2/4] Извличане на телата …")
    run_extraction()

    # 3. Обединяване + дедупликация на събития (с предпазен таван).
    print("\n[3/4] Обединяване + дедупликация на събития …")
    conn = db.connect()
    combine_report = combine.run_combine(conn)

    # 4. Ревю-имейл за новите материали (реален send, с предпазен таван).
    print("\n[4/4] Ревю-имейл …")
    email_report = notify.send_unsent(conn)
    total_articles, total_matches = db.counts(conn)
    conn.close()

    print("\n" + "─" * 70)
    print("  ОБОБЩЕНИЕ")
    print("─" * 70)
    print(f"  Нови embed-нати заглавия : {match_report['embedded']}")
    print(f"  Нови съвпадения          : {len(match_report['new_matches'])}")
    print(f"  Обединени този път       : {len(combine_report['produced'])}"
          + ("  ⚠ ТАВАН" if combine_report["cap_hit"] else ""))
    print(f"  Прескочени (дубликат)    : {combine_report['duplicates']}")
    print(f"  LLM извиквания / таван   : {combine_report['llm_calls']} / "
          f"{combine_report['cap']}")
    kind = "пратени" if not email_report["dry_run"] else "would-email (dry-run)"
    print(f"  Имейли ({kind})   : "
          f"{email_report['sent'] + email_report['would_send']}")
    print(f"  Общо статии / съвпадения : {total_articles} / {total_matches}")
    print()


def main():
    try:
        run()
    except Exception as ex:
        # Изричен провал → Actions маркира run-а като failed (стъпката за аларма
        # праща имейл; вградените известия на GitHub са резерв).
        print(f"\n✗ ПРОВАЛ НА КОНВЕЙЕРА: {type(ex).__name__}: {ex}",
              file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
