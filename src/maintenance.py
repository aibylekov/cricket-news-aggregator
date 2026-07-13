# -*- coding: utf-8 -*-
"""
Поддръжка на базата (Фаза 8+): архив + подрязване + VACUUM.

Проблемът, който решаваме: размерът на `data/cricket.db` расте неограничено,
защото пазим embedding вектора (1536 float-а на заглавие като JSON ≈ 30KB/ред).
При > 100 MB GitHub отхвърля push-а → целият конвейер падаше. (Виж root-cause
бележката в README.)

Всяко изпълнение, В КРАЯ:
  1. АРХИВ (append-only, dedup): преди да трием каквото и да е, дописваме към
     отделен `data/archive.db` статиите (source, headline, url, published,
     first_seen, body където има) и обединените материали. Архивът е за бъдещ
     проект и НИКОГА не се чете от конвейера — само се пише.
  2. ПОДРЯЗВАНЕ: логиката за съвпадение/дедуп гледа само 12h назад, тъй че:
     - зануляваме embedding-ите на статии по-стари от EMBED_RETENTION_DAYS (3д)
       — това маха основния размер, без риск (редовете остават за url-дедуп);
     - трием напълно остарелите редове (> ROW_RETENTION_DAYS, 14д), ненужни на
       дедупа; пазим НЕизпратените обединени материали и техните двойки.
  3. VACUUM — за да се свие файлът наистина. Логваме размер преди/след.
"""

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from . import db
except ImportError:
    import db


DEFAULT_EMBED_RETENTION_DAYS = 3    # embedding-ите живеят колкото 12h логиката + резерв
DEFAULT_ROW_RETENTION_DAYS = 14     # редовете живеят по-дълго (комфортно над всичко)
DEFAULT_ARCHIVE_PATH = db.DB_PATH.parent / "archive.db"


_ARCHIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    url         TEXT PRIMARY KEY,     -- dedup ключ: една статия се архивира веднъж
    source      TEXT,
    headline    TEXT,
    published   TEXT,
    first_seen  TEXT,
    body        TEXT,
    archived_at TEXT
);
CREATE TABLE IF NOT EXISTS combined_articles (
    dedup_key    TEXT PRIMARY KEY,    -- източниците + заглавие → едно събитие веднъж
    headline     TEXT,
    body         TEXT,
    editor_notes TEXT,
    source_a_url TEXT,
    source_b_url TEXT,
    model        TEXT,
    created_at   TEXT,
    archived_at  TEXT
);
"""


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _size_mb(path):
    try:
        return os.path.getsize(path) / 1_000_000
    except OSError:
        return 0.0


def archive_all(conn, archive_path=DEFAULT_ARCHIVE_PATH):
    """Дописва текущите статии и обединени материали към архива (dedup-on-write).

    Архивът е отделен SQLite файл. Повтарящите се пускания (като тези, които
    докараха дублиращите имейли) НЕ го подуват — dedup по url / dedup_key. Тялото
    се допълва по-късно чрез UPSERT, ако вече го имаме.
    """
    archive_path = Path(archive_path)
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    arc = sqlite3.connect(archive_path)
    try:
        arc.executescript(_ARCHIVE_SCHEMA)
        now = _now_iso()

        arts = conn.execute(
            "SELECT source, headline, url, published, first_seen, body "
            "FROM articles").fetchall()
        arc.executemany(
            "INSERT INTO articles "
            "(url, source, headline, published, first_seen, body, archived_at) "
            "VALUES (:url, :source, :headline, :published, :first_seen, :body, :ts) "
            "ON CONFLICT(url) DO UPDATE SET "
            "  body = COALESCE(excluded.body, articles.body)",
            [{"url": r["url"], "source": r["source"], "headline": r["headline"],
              "published": r["published"], "first_seen": r["first_seen"],
              "body": r["body"], "ts": now} for r in arts])

        combined = conn.execute(
            "SELECT headline, body, editor_notes, source_a_url, source_b_url, "
            "  model, created_at FROM combined_articles").fetchall()
        arc.executemany(
            "INSERT OR IGNORE INTO combined_articles "
            "(dedup_key, headline, body, editor_notes, source_a_url, "
            " source_b_url, model, created_at, archived_at) "
            "VALUES (:k, :headline, :body, :notes, :a, :b, :model, :created, :ts)",
            [{"k": f"{r['source_a_url'] or ''}|{r['source_b_url'] or ''}|{r['headline']}",
              "headline": r["headline"], "body": r["body"],
              "notes": r["editor_notes"], "a": r["source_a_url"],
              "b": r["source_b_url"], "model": r["model"],
              "created": r["created_at"], "ts": now} for r in combined])

        arc.commit()
        a = arc.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        c = arc.execute("SELECT COUNT(*) FROM combined_articles").fetchone()[0]
        return {"articles": a, "combined": c, "size_mb": _size_mb(archive_path)}
    finally:
        arc.close()


def prune(conn, embed_days=DEFAULT_EMBED_RETENTION_DAYS,
          row_days=DEFAULT_ROW_RETENTION_DAYS):
    """Зануляваме стари вектори и трием остарелите редове. Връща отчет."""
    now = datetime.now(timezone.utc)
    embed_cutoff = (now - timedelta(days=embed_days)).isoformat()
    row_cutoff = (now - timedelta(days=row_days)).isoformat()

    # 1. Основният размер: занули embedding-ите по-стари от прозореца.
    nulled = conn.execute(
        "UPDATE articles SET embedding = NULL "
        "WHERE embedding IS NOT NULL AND first_seen < ?", (embed_cutoff,)).rowcount

    # 2. Изпратените стари обединени материали — леджърът може да се подреже
    #    (НЕизпратените се пазят винаги, за да не се пращат пак).
    del_combined = conn.execute(
        "DELETE FROM combined_articles "
        "WHERE emailed_at IS NOT NULL AND created_at < ?", (row_cutoff,)).rowcount

    # 3. Стари двойки, които вече не са нужни на никой обединен материал.
    del_matches = conn.execute(
        "DELETE FROM matches WHERE matched_at < ? "
        "AND id NOT IN (SELECT match_id FROM combined_articles)",
        (row_cutoff,)).rowcount

    # 4. Стари статии, които вече не участват в никоя двойка (url-ите им вече
    #    не са нужни за дедуп — новини на такава възраст не се появяват пак).
    del_articles = conn.execute(
        "DELETE FROM articles WHERE first_seen < ? AND id NOT IN "
        "(SELECT article_a_id FROM matches UNION SELECT article_b_id FROM matches)",
        (row_cutoff,)).rowcount

    conn.commit()
    return {"embeddings_nulled": nulled, "combined_deleted": del_combined,
            "matches_deleted": del_matches, "articles_deleted": del_articles}


def vacuum(conn):
    """VACUUM (извън транзакция), за да се свие файлът реално."""
    conn.commit()
    old = conn.isolation_level
    conn.isolation_level = None
    try:
        conn.execute("VACUUM")
    finally:
        conn.isolation_level = old


def run_maintenance(conn, db_path=db.DB_PATH, archive_path=None,
                    embed_days=None, row_days=None, log=print):
    """Архив → подрязване → VACUUM, с лог на размера преди/след."""
    archive_path = archive_path or os.getenv("ARCHIVE_PATH", DEFAULT_ARCHIVE_PATH)
    embed_days = embed_days if embed_days is not None else int(
        os.getenv("EMBED_RETENTION_DAYS", DEFAULT_EMBED_RETENTION_DAYS))
    row_days = row_days if row_days is not None else int(
        os.getenv("ROW_RETENTION_DAYS", DEFAULT_ROW_RETENTION_DAYS))

    size_before = _size_mb(db_path)
    log(f"  Поддръжка: база преди = {size_before:.1f} MB")

    arc = archive_all(conn, archive_path)
    log(f"  Архив (append-only): {arc['articles']} статии, "
        f"{arc['combined']} обединени → {arc['size_mb']:.1f} MB "
        f"({Path(archive_path).name})")

    pr = prune(conn, embed_days=embed_days, row_days=row_days)
    log(f"  Подрязване: занулени {pr['embeddings_nulled']} вектора; "
        f"изтрити {pr['articles_deleted']} статии, {pr['matches_deleted']} двойки, "
        f"{pr['combined_deleted']} стари обединени (retention {embed_days}d/{row_days}d)")

    vacuum(conn)
    size_after = _size_mb(db_path)
    log(f"  VACUUM: база след = {size_after:.1f} MB "
        f"(-{max(0.0, size_before - size_after):.1f} MB)")

    return {"size_before_mb": size_before, "size_after_mb": size_after,
            "archive": arc, "prune": pr}
