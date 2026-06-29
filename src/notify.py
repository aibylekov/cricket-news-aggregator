#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ревю-имейл доставка (Фаза 6 — human-in-the-loop, без API на сайта засега).

За всеки новообединен, непратен материал праща имейл до ревюъра:
  - тема   = генерираното заглавие
  - тяло   = чистата статия, после блокът EDITOR NOTES, после двата
             източника (URL-и), за да може ревюърът да провери фактите

Доставка през SMTP над SSL на порт 465 (`smtplib.SMTP_SSL`, НЕ STARTTLS).
Конфигурацията се чете от `.env`:
  EMAIL_FROM, EMAIL_TO, SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD

Идемпотентност: материалът се маркира `emailed_at` след пращане — никога не
се праща втори път.

Тестируемост / безопасност:
  - `--dry-run` (изричен): само логва „would send“, не праща нищо — изборът
    dry-run срещу реално пращане е съзнателен, не просто „празна ли е паролата“.
  - Ако КОЯ ДА Е от SMTP променливите е празна, пак НЕ праща (dry-run), за да
    е конвейерът тестваем преди да има креденшъли.
  - `--limit N`: праща най-много N непратени материала в едно пускане. С
    `--limit 1` тръгва ТОЧНО един, а останалите остават непратени и недокоснати.
    Безопасен единичен тест преди масово пращане.

Стартиране:
    python -m src.notify --limit 1            # реален единичен тест (1 имейл)
    python -m src.notify --dry-run            # само лог, нищо не се праща
    python -m src.notify                      # праща всички непратени
"""

import argparse
import os
import smtplib
import ssl
import sys
from email.message import EmailMessage

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
except ImportError:  # позволява и директно `python src/notify.py`
    import db


load_dotenv()


def _config():
    """Чете SMTP конфигурацията от средата."""
    return {
        "from": os.getenv("EMAIL_FROM", "").strip(),
        "to": os.getenv("EMAIL_TO", "").strip(),
        "host": os.getenv("SMTP_HOST", "").strip(),
        "port": os.getenv("SMTP_PORT", "").strip(),
        "user": os.getenv("SMTP_USER", "").strip(),
        "password": os.getenv("SMTP_PASSWORD", "").strip(),
    }


def is_configured(cfg=None):
    """True само ако ВСИЧКИ SMTP променливи са попълнени."""
    cfg = cfg or _config()
    return all(cfg.values())


def build_body(row):
    """Сглобява текста на имейла: статия + EDITOR NOTES + двата източника."""
    notes = (row.get("editor_notes") or "EDITOR NOTES: none.").strip()
    parts = [
        row["body"].strip(),
        notes,
        "Sources (for verification):\n"
        f"  A: {row.get('source_a_url') or '—'}\n"
        f"  B: {row.get('source_b_url') or '—'}",
    ]
    return "\n\n".join(parts) + "\n"


def build_message(row, cfg):
    """EmailMessage с тема = заглавието и тяло = build_body."""
    msg = EmailMessage()
    msg["Subject"] = row["headline"]
    msg["From"] = cfg["from"]
    msg["To"] = cfg["to"]
    msg.set_content(build_body(row))
    return msg


def send_unsent(conn, limit=None, dry_run=False):
    """Праща (или dry-run логва) непратените материали. Връща броячи.

    limit:   праща най-много N материала този път (None = всички). Срезът се
             прави ПРЕДИ пращането, тъй че `limit=1` праща точно един, а
             останалите остават непратени и недокоснати (idempotent по emailed_at).
    dry_run: ако True — само логва „would send“, без реално пращане (изричен
             избор). Реално пращане има само при configured И not dry_run.
    """
    pending = db.combined_to_email(conn)
    cfg = _config()
    configured = is_configured(cfg)
    do_send = configured and not dry_run

    # Срезът определя колко обработваме този път; останалите стоят непокътнати.
    batch = pending if limit is None else pending[:max(0, limit)]
    remaining_untouched = len(pending) - len(batch)

    sent, would_send, errors = 0, 0, 0

    if not pending:
        print("  (няма непратени материали)")
        return {"sent": 0, "would_send": 0, "errors": 0, "configured": configured,
                "dry_run": not do_send, "pending_total": 0, "remaining": 0}

    if not do_send:
        # Сухо пускане — изричен --dry-run или липсваща конфигурация.
        # НЕ маркираме нищо, за да тръгнат, щом се пусне реално.
        why = "--dry-run" if dry_run else "SMTP не е конфигуриран"
        for row in batch:
            print(f"  ✉ would send → [{cfg['to'] or 'EMAIL_TO?'}] {row['headline']}")
            would_send += 1
        if remaining_untouched:
            print(f"  … още {remaining_untouched} непратени (извън --limit)")
        return {"sent": 0, "would_send": would_send, "errors": 0,
                "configured": configured, "dry_run": True,
                "pending_total": len(pending), "remaining": len(pending),
                "dry_run_reason": why}

    # Реално пращане през SSL/465.
    context = ssl.create_default_context()
    try:
        server = smtplib.SMTP_SSL(cfg["host"], int(cfg["port"]), context=context)
        server.login(cfg["user"], cfg["password"])
    except Exception as ex:
        print(f"  ✗ SMTP връзка/логин се провали: {type(ex).__name__}: {ex}")
        return {"sent": 0, "would_send": 0, "errors": len(batch),
                "configured": True, "dry_run": False,
                "pending_total": len(pending), "remaining": len(pending)}

    try:
        for row in batch:
            try:
                server.send_message(build_message(row, cfg))
                db.mark_emailed(conn, row["id"])   # маркира → няма повторно пращане
                conn.commit()
                sent += 1
                print(f"  ✓ sent → [{cfg['to']}] {row['headline']}")
            except Exception as ex:
                errors += 1
                print(f"  ✗ грешка при пращане ({row['headline']}): "
                      f"{type(ex).__name__}: {ex}")
    finally:
        try:
            server.quit()
        except Exception:
            pass

    if remaining_untouched:
        print(f"  … още {remaining_untouched} непратени остават (извън --limit)")
    return {"sent": sent, "would_send": 0, "errors": errors, "configured": True,
            "dry_run": False, "pending_total": len(pending),
            "remaining": len(pending) - sent}


def main():
    parser = argparse.ArgumentParser(
        description="Ревю-имейл доставка (Фаза 6). Безопасен единичен тест: "
                    "python -m src.notify --limit 1")
    parser.add_argument("--limit", type=int, default=None,
                        help="максимум материали за пращане в едно пускане "
                             "(--limit 1 = точно един)")
    parser.add_argument("--dry-run", action="store_true",
                        help="само логва „would send“, не праща нищо")
    args = parser.parse_args()

    print("\n=== Ревю-имейл доставка (Фаза 6) ===\n")
    conn = db.connect()
    report = send_unsent(conn, limit=args.limit, dry_run=args.dry_run)
    conn.close()

    if report["dry_run"]:
        mode = f"DRY-RUN ({report.get('dry_run_reason', '--dry-run')})"
    else:
        mode = "SSL/465 — РЕАЛНО ПРАЩАНЕ"
    print(f"\nРежим: {mode}")
    print(f"Пратени: {report['sent']} | would-send: {report['would_send']} | "
          f"грешки: {report['errors']} | остават непратени: {report['remaining']}\n")


if __name__ == "__main__":
    main()
