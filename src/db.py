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
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    article_a_id   INTEGER NOT NULL,
    article_b_id   INTEGER NOT NULL,
    similarity     REAL NOT NULL,
    matched_at     TEXT NOT NULL,
    -- Състояние при обединяване (Фаза 6): NULL = необработена,
    -- 'combined' = обединена в събитие, 'duplicate' = прескочена като дубликат.
    combine_status TEXT,
    UNIQUE(article_a_id, article_b_id),
    FOREIGN KEY(article_a_id) REFERENCES articles(id),
    FOREIGN KEY(article_b_id) REFERENCES articles(id)
);

CREATE TABLE IF NOT EXISTS combined_articles (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id     INTEGER NOT NULL UNIQUE,  -- по едно обединение на двойка (идемпотентно)
    headline     TEXT NOT NULL,
    body         TEXT NOT NULL,            -- чисто тяло, без маркери (Фаза 6 пост-обработка)
    editor_notes TEXT,                     -- блокът EDITOR NOTES, държан отделно от тялото
    source_a_url TEXT,                     -- двата източника — за проверка от ревюъра
    source_b_url TEXT,
    model        TEXT NOT NULL,            -- кой LLM е написал материала
    created_at   TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'draft',
    emailed_at   TEXT,                     -- ISO кога е пратен за ревю (NULL = непратен)
    FOREIGN KEY(match_id) REFERENCES matches(id)
);

CREATE INDEX IF NOT EXISTS idx_articles_first_seen ON articles(first_seen);
CREATE INDEX IF NOT EXISTS idx_articles_published  ON articles(published);
"""


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


# Колони, добавени след първоначалната схема. За вече съществуваща база
# CREATE TABLE IF NOT EXISTS не ги добавя — затова ги долепяме с ALTER.
_MIGRATIONS = {
    "articles": [               # Фаза 3 — тела
        ("body", "TEXT"),
        ("body_status", "TEXT"),
        ("body_url", "TEXT"),
        ("body_fetched_at", "TEXT"),
    ],
    "matches": [                # Фаза 6 — състояние при обединяване
        ("combine_status", "TEXT"),
    ],
    "combined_articles": [      # Фаза 6 — EDITOR NOTES, източници, имейл статус
        ("editor_notes", "TEXT"),
        ("source_a_url", "TEXT"),
        ("source_b_url", "TEXT"),
        ("emailed_at", "TEXT"),
    ],
}


def _migrate(conn):
    """Долепя липсващите колони към стара база (идемпотентно)."""
    for table, cols in _MIGRATIONS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, decl in cols:
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


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


# ── Обединяване и дедупликация на събития (Фаза 5/6) ─────────────────────────

# Двойка е готова за обединяване, когато И ДВЕТЕ статии имат успешно тяло.
_BOTH_BODIES_OK = "a.body_status = 'ok' AND b.body_status = 'ok'"

# Двойка е „необработена“, когато няма combine_status и още не е обединявана.
# (Двойната проверка пази и legacy редове отпреди combine_status.)
_UNPROCESSED = ("m.combine_status IS NULL "
                "AND m.id NOT IN (SELECT match_id FROM combined_articles)")


def pairs_to_combine(conn):
    """Необработени съвпадащи двойки с налични тела на ДВЕТЕ статии.

    Връща и id-тата, разрешените URL-и и embeddings на двете статии — нужни
    за дедупликацията на събития и за имейла, без допълнителни заявки.
    """
    rows = conn.execute(
        f"SELECT m.id AS match_id, m.similarity, "
        f"  a.id AS a_id, a.source AS a_source, a.headline AS a_headline, "
        f"  a.body AS a_body, a.embedding AS a_emb, "
        f"  COALESCE(a.body_url, a.url) AS a_url, "
        f"  b.id AS b_id, b.source AS b_source, b.headline AS b_headline, "
        f"  b.body AS b_body, b.embedding AS b_emb, "
        f"  COALESCE(b.body_url, b.url) AS b_url "
        f"FROM matches m "
        f"JOIN articles a ON a.id = m.article_a_id "
        f"JOIN articles b ON b.id = m.article_b_id "
        f"WHERE {_BOTH_BODIES_OK} AND {_UNPROCESSED} "
        f"ORDER BY m.id"
    ).fetchall()
    return [dict(r) for r in rows]


def count_pairs_missing_body(conn):
    """Брой необработени двойки, които прескачаме заради липсващо тяло."""
    return conn.execute(
        f"SELECT COUNT(*) AS n FROM matches m "
        f"JOIN articles a ON a.id = m.article_a_id "
        f"JOIN articles b ON b.id = m.article_b_id "
        f"WHERE NOT ({_BOTH_BODIES_OK}) AND {_UNPROCESSED}"
    ).fetchone()["n"]


def published_event_embeddings(conn, window_hours):
    """Embeddings на статиите от вече обединени („публикувани“) събития.

    Само скорошните (по published, иначе first_seen — прозорец window_hours),
    защото дубликат се мери срещу актуалните събития. Това е котвата, срещу
    която проверяваме всяка нова двойка преди да я обединим.
    """
    cutoff = datetime.now(timezone.utc).timestamp() - window_hours * 3600
    rows = conn.execute(
        "SELECT DISTINCT a.id, a.embedding, a.published, a.first_seen "
        "FROM articles a WHERE a.embedding IS NOT NULL AND a.id IN ("
        "  SELECT article_a_id FROM matches WHERE combine_status = 'combined' "
        "  UNION "
        "  SELECT article_b_id FROM matches WHERE combine_status = 'combined')"
    ).fetchall()

    out = []
    for r in rows:
        stamp = r["published"] or r["first_seen"]
        try:
            ts = datetime.fromisoformat(stamp).timestamp()
        except (ValueError, TypeError):
            ts = None
        if ts is None or ts >= cutoff:
            out.append({"id": r["id"], "embedding": json.loads(r["embedding"])})
    return out


def mark_combine_status(conn, match_id, status):
    """Маркира двойката като 'combined' или 'duplicate'."""
    conn.execute("UPDATE matches SET combine_status = ? WHERE id = ?",
                 (status, match_id))


def save_combined(conn, match_id, headline, body, model,
                  editor_notes=None, source_a_url=None, source_b_url=None):
    """Записва обединения материал (статус 'draft'). Идемпотентно по match_id."""
    conn.execute(
        "INSERT OR IGNORE INTO combined_articles "
        "(match_id, headline, body, editor_notes, source_a_url, source_b_url, "
        " model, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (match_id, headline, body, editor_notes, source_a_url, source_b_url,
         model, _now_iso()),
    )


def combined_to_email(conn):
    """Обединени материали, които още не са пращани за ревю (emailed_at IS NULL)."""
    rows = conn.execute(
        "SELECT id, match_id, headline, body, editor_notes, "
        "  source_a_url, source_b_url "
        "FROM combined_articles WHERE emailed_at IS NULL ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def mark_emailed(conn, combined_id):
    """Маркира материала като пратен — за да не се праща повторно."""
    conn.execute(
        "UPDATE combined_articles SET emailed_at = ?, status = 'emailed' "
        "WHERE id = ?", (_now_iso(), combined_id))


def reset_combinations(conn):
    """САМО ЗА ТЕСТ: изчиства обединените материали и нулира combine_status."""
    conn.execute("DELETE FROM combined_articles")
    conn.execute("UPDATE matches SET combine_status = NULL")
    conn.commit()


def combined_count(conn):
    """Общ брой обединени материали в базата."""
    return conn.execute(
        "SELECT COUNT(*) AS n FROM combined_articles").fetchone()["n"]


def duplicate_count(conn):
    """Общ брой двойки, прескочени като дубликат на събитие."""
    return conn.execute(
        "SELECT COUNT(*) AS n FROM matches WHERE combine_status = 'duplicate'"
    ).fetchone()["n"]
