# -*- coding: utf-8 -*-
"""
Съхранение в SQLite (Фаза 4).

Държи състоянието между изпълненията — кои статии вече сме виждали и кои
двойки между източници вече са отбелязани като съвпадение. Заменя крехкото
сравняване на JSON файлове (виж README, „Взети решения“).

Две таблици:
  articles  — по един ред на уникална статия (дедупликация по url).
              embedding пазим като JSON масив (векторът от OpenAI).
  matches   — по един ред на съвпадаща двойка между РАЗЛИЧНИ източници.
              Двойката се пази нормализирана (по-малкото id първо), за да
              може UNIQUE да хване и двата реда на подреждане.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# Базата живее в data/ (извън Git — .gitignore изключва *.db).
DB_PATH = Path(__file__).resolve().parent.parent / "data" / "cricket.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL,
    headline        TEXT NOT NULL,
    url             TEXT NOT NULL UNIQUE,
    published       TEXT,          -- ISO дата от емисията (може да липсва)
    embedding       TEXT,          -- JSON масив с вектора
    first_seen      TEXT NOT NULL, -- ISO кога за пръв път сме видели статията
    -- Тяло на статията (Фаза 3) — пълни се само за статии в съвпадение.
    body            TEXT,          -- чистият текст; NULL при провал
    body_status     TEXT,          -- ok | 403 | empty | timeout | unresolved | http_XXX | error
    body_url        TEXT,          -- разрешеният истински URL, от който теглим
    body_fetched_at TEXT           -- ISO кога сме опитали извличането
);

CREATE TABLE IF NOT EXISTS matches (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    article_a_id INTEGER NOT NULL,
    article_b_id INTEGER NOT NULL,
    similarity   REAL NOT NULL,
    matched_at   TEXT NOT NULL,
    UNIQUE(article_a_id, article_b_id),
    FOREIGN KEY(article_a_id) REFERENCES articles(id),
    FOREIGN KEY(article_b_id) REFERENCES articles(id)
);

CREATE TABLE IF NOT EXISTS combined_articles (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id   INTEGER NOT NULL UNIQUE,  -- по едно обединение на двойка (идемпотентно)
    headline   TEXT NOT NULL,
    body       TEXT NOT NULL,
    model      TEXT NOT NULL,            -- кой LLM е написал материала
    created_at TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'draft',
    FOREIGN KEY(match_id) REFERENCES matches(id)
);

CREATE INDEX IF NOT EXISTS idx_articles_first_seen ON articles(first_seen);
CREATE INDEX IF NOT EXISTS idx_articles_published  ON articles(published);
"""


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


# Колони, добавени след първоначалната схема (Фаза 3). За вече съществуваща
# база CREATE TABLE IF NOT EXISTS не ги добавя — затова ги долепяме с ALTER.
_BODY_COLUMNS = [
    ("body", "TEXT"),
    ("body_status", "TEXT"),
    ("body_url", "TEXT"),
    ("body_fetched_at", "TEXT"),
]


def _migrate(conn):
    """Долепя липсващите колони към стара база (идемпотентно)."""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(articles)")}
    for col, decl in _BODY_COLUMNS:
        if col not in have:
            conn.execute(f"ALTER TABLE articles ADD COLUMN {col} {decl}")


def connect(db_path=DB_PATH):
    """Отваря връзка към базата и подсигурява схемата."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def existing_urls(conn):
    """Множество от вече видените URL-и — за бърза проверка кое е ново."""
    rows = conn.execute("SELECT url FROM articles").fetchall()
    return {r["url"] for r in rows}


def insert_article(conn, art, embedding):
    """Вкарва нова статия (с вектора) и връща нейното id.

    При сблъсък по url не дублира — връща id-то на съществуващия ред.
    """
    cur = conn.execute(
        "INSERT OR IGNORE INTO articles "
        "(source, headline, url, published, embedding, first_seen) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (art["source"], art["headline"], art["url"], art.get("published"),
         json.dumps(embedding), _now_iso()),
    )
    if cur.lastrowid and cur.rowcount:
        return cur.lastrowid
    row = conn.execute("SELECT id FROM articles WHERE url = ?",
                       (art["url"],)).fetchone()
    return row["id"]


def recent_articles(conn, window_hours):
    """Статии от последния прозорец, които имат вектор.

    „Скорошна“ се мери по published, а ако липсва — по first_seen
    (COALESCE), за да не изпускаме статии без дата от емисията.
    """
    cutoff = datetime.now(timezone.utc).timestamp() - window_hours * 3600
    rows = conn.execute(
        "SELECT id, source, headline, url, published, embedding, first_seen "
        "FROM articles WHERE embedding IS NOT NULL"
    ).fetchall()

    recent = []
    for r in rows:
        stamp = r["published"] or r["first_seen"]
        try:
            ts = datetime.fromisoformat(stamp).timestamp()
        except (ValueError, TypeError):
            ts = None
        if ts is None or ts >= cutoff:
            recent.append({
                "id": r["id"],
                "source": r["source"],
                "headline": r["headline"],
                "url": r["url"],
                "embedding": json.loads(r["embedding"]),
            })
    return recent


def record_match(conn, a_id, b_id, similarity):
    """Записва двойка съвпадение (нормализирана). True, ако е нова."""
    lo, hi = (a_id, b_id) if a_id < b_id else (b_id, a_id)
    cur = conn.execute(
        "INSERT OR IGNORE INTO matches "
        "(article_a_id, article_b_id, similarity, matched_at) "
        "VALUES (?, ?, ?, ?)",
        (lo, hi, float(similarity), _now_iso()),
    )
    return cur.rowcount > 0


def counts(conn):
    """Брой статии и съвпадения — за кратък отчет."""
    a = conn.execute("SELECT COUNT(*) AS n FROM articles").fetchone()["n"]
    m = conn.execute("SELECT COUNT(*) AS n FROM matches").fetchone()["n"]
    return a, m


# ── Тела на статиите (Фаза 3) ──────────────────────────────────────────────

# Подзаявка: id-тата на всички статии, които участват в поне едно съвпадение.
_MATCHED_IDS = ("SELECT article_a_id FROM matches "
                "UNION SELECT article_b_id FROM matches")


def articles_needing_body(conn, retry_failed=False):
    """Статии в съвпадение, на които още не сме извличали тялото.

    По подразбиране връща само НЕОПИТВАНИТЕ (body_status IS NULL) — така едно
    тяло никога не се тегли повторно. С retry_failed=True връща и провалените
    (status != 'ok') — за повторен опит, напр. след добавяне на fetch-услуга.
    """
    if retry_failed:
        cond = "(body_status IS NULL OR body_status != 'ok')"
    else:
        cond = "body_status IS NULL"
    rows = conn.execute(
        f"SELECT id, source, headline, url FROM articles "
        f"WHERE id IN ({_MATCHED_IDS}) AND {cond} "
        f"ORDER BY source, id"
    ).fetchall()
    return [dict(r) for r in rows]


def save_body(conn, article_id, body, status, resolved_url):
    """Записва резултата от извличането (успех или провал)."""
    conn.execute(
        "UPDATE articles SET body = ?, body_status = ?, body_url = ?, "
        "body_fetched_at = ? WHERE id = ?",
        (body, status, resolved_url, _now_iso(), article_id),
    )


def body_report(conn):
    """Разбивка по източник: брой статии в съвпадение по статус на тялото.

    Връща {source: {status: count}}. Подрежда се по източник в извикващия.
    """
    rows = conn.execute(
        f"SELECT source, COALESCE(body_status, 'не-опитан') AS status, "
        f"COUNT(*) AS n FROM articles WHERE id IN ({_MATCHED_IDS}) "
        f"GROUP BY source, status"
    ).fetchall()
    report = {}
    for r in rows:
        report.setdefault(r["source"], {})[r["status"]] = r["n"]
    return report


# ── Обединяване (Фаза 5) ────────────────────────────────────────────────────

# Двойка е готова за обединяване, когато И ДВЕТЕ статии имат успешно тяло.
_BOTH_BODIES_OK = "a.body_status = 'ok' AND b.body_status = 'ok'"


def pairs_to_combine(conn):
    """Съвпадащи двойки с налични тела на ДВЕТЕ статии, още необединени.

    Двойките без тяло (напр. остатъка от Фаза 3) се отсяват тук — затова
    combine.py никога не им праща заявка.
    """
    rows = conn.execute(
        f"SELECT m.id AS match_id, m.similarity, "
        f"  a.source AS a_source, a.headline AS a_headline, a.body AS a_body, "
        f"  b.source AS b_source, b.headline AS b_headline, b.body AS b_body "
        f"FROM matches m "
        f"JOIN articles a ON a.id = m.article_a_id "
        f"JOIN articles b ON b.id = m.article_b_id "
        f"WHERE {_BOTH_BODIES_OK} "
        f"  AND m.id NOT IN (SELECT match_id FROM combined_articles) "
        f"ORDER BY m.id"
    ).fetchall()
    return [dict(r) for r in rows]


def count_pairs_missing_body(conn):
    """Брой необединени двойки, които прескачаме заради липсващо тяло."""
    return conn.execute(
        f"SELECT COUNT(*) AS n FROM matches m "
        f"JOIN articles a ON a.id = m.article_a_id "
        f"JOIN articles b ON b.id = m.article_b_id "
        f"WHERE NOT ({_BOTH_BODIES_OK}) "
        f"  AND m.id NOT IN (SELECT match_id FROM combined_articles)"
    ).fetchone()["n"]


def save_combined(conn, match_id, headline, body, model):
    """Записва обединения материал (статус 'draft'). Идемпотентно по match_id."""
    conn.execute(
        "INSERT OR IGNORE INTO combined_articles "
        "(match_id, headline, body, model, created_at) VALUES (?, ?, ?, ?, ?)",
        (match_id, headline, body, model, _now_iso()),
    )


def combined_count(conn):
    """Общ брой обединени материали в базата."""
    return conn.execute(
        "SELECT COUNT(*) AS n FROM combined_articles").fetchone()["n"]
