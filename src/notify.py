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

Тестируемост: ако КОЯ ДА Е от SMTP променливите е празна, НЕ праща — логва
„would send“ със темата, така че конвейерът е тестваем преди да има креденшъли.

Стартиране (праща непратените материали самостоятелно):
    python -m src.notify
"""

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


def send_unsent(conn):
    """Праща (или dry-run логва) непратените материали. Връща броячи."""
    pending = db.combined_to_email(conn)
    cfg = _config()
    configured = is_configured(cfg)

    sent, would_send, errors = 0, 0, 0

    if not pending:
        print("  (няма непратени материали)")
        return {"sent": 0, "would_send": 0, "errors": 0, "configured": configured}

    if not configured:
        # Сухо пускане — SMTP не е конфигуриран. Не маркираме като пратени,
        # за да тръгнат, щом креденшълите бъдат добавени.
        for row in pending:
            print(f"  ✉ would send → [{cfg['to'] or 'EMAIL_TO?'}] {row['headline']}")
            would_send += 1
        return {"sent": 0, "would_send": would_send, "errors": 0,
                "configured": False}

    # Реално пращане през SSL/465.
    context = ssl.create_default_context()
    try:
        server = smtplib.SMTP_SSL(cfg["host"], int(cfg["port"]), context=context)
        server.login(cfg["user"], cfg["password"])
    except Exception as ex:
        print(f"  ✗ SMTP връзка/логин се провали: {type(ex).__name__}: {ex}")
        return {"sent": 0, "would_send": 0, "errors": len(pending),
                "configured": True}

    try:
        for row in pending:
            try:
                server.send_message(build_message(row, cfg))
                db.mark_emailed(conn, row["id"])
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

    return {"sent": sent, "would_send": 0, "errors": errors, "configured": True}


def main():
    print("\n=== Ревю-имейл доставка (Фаза 6) ===\n")
    conn = db.connect()
    report = send_unsent(conn)
    conn.close()
    mode = "SSL/465" if report["configured"] else "DRY-RUN (SMTP не е конфигуриран)"
    print(f"\nРежим: {mode}")
    print(f"Пратени: {report['sent']} | would-send: {report['would_send']} | "
          f"грешки: {report['errors']}\n")


if __name__ == "__main__":
    main()
