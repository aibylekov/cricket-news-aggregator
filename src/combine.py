#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Обединяване + дедупликация на събития (Фаза 5/6).

За всяка необработена съвпадаща двойка с налични тела на ДВЕТЕ статии:
  1. Чисти scrape-furniture от телата (clean_body) — bylines, агенции,
     „Updated:/Published:“ редове, „Recommended Stories“ блокове и подобни.
  2. ДЕДУПЛИКАЦИЯ НА СЪБИТИЯ (Фаза 6): ако коя да е от двете статии прилича
     (embeddings + косинус ≥ 0.8) на статия от вече публикувано събитие в
     последните 12 часа, двойката се прескача (без обединяване, без разход).
  3. Праща двата изчистени текста на gpt-5.4-mini със синтез-промпта v2
     (`prompts/synthesis_system_prompt.md`) за НОВ оригинален материал
     (заглавие + чисто тяло + EDITOR NOTES блок).
  4. ПОСТ-ОБРАБОТКА (детерминистична, не работа на модела): маха em dash от
     тялото и нормализира EDITOR NOTES (празният случай → точно
     „EDITOR NOTES: none.“). Тялото остава без маркери.
  5. Записва материала (статус 'draft') и маркира събитието като публикувано,
     за да хваща по-късните дубликати.

Решения (Фаза 6): модел gpt-5.4-mini; прозорци за съвпадение/събитие 12 часа.

Идемпотентно: всяка двойка се обработва веднъж (combine_status), повторно
пускане не праща нови заявки. Дисциплина за цената: заявка тръгва само за нова,
неприлична на публикувано събитие двойка.

Стартиране:
    python -m src.combine
    python -m src.combine --reset    # САМО ТЕСТ: трие старите обединения и почва наново
    python -m src.combine --limit 5
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

import numpy as np

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
    from . import notify
    from .retry import retry_call
except ImportError:  # позволява и директно `python src/combine.py`
    import db
    import notify
    from retry import retry_call


load_dotenv()

MODEL = "gpt-5.4-mini"      # решение Фаза 6
TEMPERATURE = 0.7          # модели, които не я поддържат, се пускат без нея
MAX_TOKENS = 4000          # статия + EDITOR NOTES (+ резерв за reasoning модели)
MAX_BODY_CHARS = 8000      # таван на входа от източник (телата ни са под това)

SIM_THRESHOLD = 0.8        # косинус, над който две статии са „същото събитие“
DEDUP_WINDOW_HOURS = 12    # колко назад гледаме за вече публикувани събития

# Предпазен таван на LLM-извикванията за едно пускане (Фаза 7) — за да не може
# избягал цикъл да натрупа разход, докато системата работи без надзор.
# Конфигурируем през средата (MAX_COMBINES_PER_RUN), по подразбиране малък.
DEFAULT_MAX_COMBINES = 10


def _max_combines():
    try:
        return int(os.getenv("MAX_COMBINES_PER_RUN", str(DEFAULT_MAX_COMBINES)))
    except ValueError:
        return DEFAULT_MAX_COMBINES

PROMPT_FILE = Path(__file__).resolve().parent.parent / "prompts" \
    / "synthesis_system_prompt.md"

USER_TEMPLATE = (
    "Merge the two sources below into ONE article, following the system "
    "instructions exactly.\n\n"
    "Begin your reply with a single line `HEADLINE: <a fresh, original "
    "headline>` (this line is metadata, not part of the article body). Then a "
    "blank line, then produce your response exactly as the system instructions "
    "specify: the clean article, then a line with only `---`, then the EDITOR "
    "NOTES block.\n\n"
    "SOURCE A:\n{a_body}\n\n"
    "SOURCE B:\n{b_body}\n"
)


# ─────────────────────────────────────────────────────────────────────────
# A. Чистене на scrape-furniture от телата (преди да ги пратим на модела)
# ─────────────────────────────────────────────────────────────────────────

_AGENCY = r"(?:Press Trust of India|PTI|ANI|IANS|Reuters|AFP|AP|Agencies)"
_BLOCK_HEADERS = re.compile(
    r"(?i)^(recommended stories|related stories|related news|also read|"
    r"read more|trending|top stories|topics|more from|you may also like|"
    r"advertisement|sponsored)\b")
_TIMESTAMP = re.compile(
    r"(?i)^(updated|published|last updated|edited|created|posted)\s*[:\-]")
_AGENCY_ONLY = re.compile(rf"(?i)^{_AGENCY}\.?$")
_BYLINE_WORD = re.compile(r"(?i)^(written|reported|edited|curated|compiled)\s+by\b")
_BYLINE_NAME = re.compile(
    r"^By\s+[A-Z][A-Za-z.\-]+(?:\s+[A-Z][A-Za-z.\-]+){0,3}$")
_AUTHOR_BIO = re.compile(
    r"(?i)^[A-Z][\w.\-]+(?:\s+[A-Z][\w.\-]+){0,3}\s+is\s+an?\s+.*"
    r"\b(journalist|correspondent|reporter|editor|writer)\b")
_INLINE_AGENCY = re.compile(rf"\s*[\(\-—]\s*{_AGENCY}\s*\)?\s*$")


def clean_body(text):
    """Маха не-статийната метаинформация от едно тяло (reusable, best-effort).

    Премахва bylines, кредити на агенции, „Updated/Published“ редове и
    „Recommended Stories / Also Read“ блокове — точно нещата, които иначе
    замърсяват EDITOR NOTES. Пази абзаците и реалния текст.
    """
    if not text:
        return text

    out, skipping = [], False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            skipping = False           # празен ред затваря furniture блок
            out.append("")
            continue
        if skipping:
            continue                   # вътре в „Recommended/Also Read“ блок
        if _BLOCK_HEADERS.match(line):
            skipping = True
            continue
        if (_TIMESTAMP.match(line) or _AGENCY_ONLY.match(line)
                or _BYLINE_WORD.match(line) or _AUTHOR_BIO.match(line)):
            continue
        if _BYLINE_NAME.match(line) and len(line) <= 50:
            continue
        line = _INLINE_AGENCY.sub("", line)   # инлайн „(PTI)“ в края
        out.append(line)

    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    return cleaned


# ─────────────────────────────────────────────────────────────────────────
# C. Пост-обработка (детерминистична — не работа на модела)
# ─────────────────────────────────────────────────────────────────────────

_EDITOR_NONE = "EDITOR NOTES: none."


def strip_em_dashes(text):
    """Маха em dash от тялото с разумен заместител (запетая), без да чупи реда."""
    if not text:
        return text
    # em dash / horizontal bar → запетая; не поглъщаме нови редове
    text = re.sub(r"[ \t]*[—―][ \t]*", ", ", text)
    text = re.sub(r"[ \t]+([,.;:!?])", r"\1", text)   # без интервал преди пункт.
    text = re.sub(r",\s*,", ", ", text)               # без двойни запетаи
    text = re.sub(r"[ \t]+\n", "\n", text)            # без увиснали интервали
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def normalize_editor_notes(notes):
    """Нормализира EDITOR NOTES: празният случай → точно „EDITOR NOTES: none.“.

    Истинските бележки се пазят дословно; само вариантите на „нищо за флагване“
    се свеждат до каноничния ред.
    """
    if not notes or not notes.strip():
        return _EDITOR_NONE

    has_content = False
    for line in notes.strip().splitlines():
        s = line.strip().lstrip("-*•").strip()
        if not s:
            continue
        low = s.lower()
        if low.startswith("editor notes"):
            after = s.split(":", 1)[1].strip().rstrip(".").strip() if ":" in s else ""
            if after and after.lower() != "none":
                has_content = True
            continue
        # етикети на кофи, евентуално с „none“
        if re.match(r"(?i)^(single[- ]source facts?|conflicts?)\s*:?\s*"
                    r"(none\.?)?$", s):
            continue
        if low in ("none", "none.", "n/a", "nothing to flag", "nothing"):
            continue
        has_content = True

    return notes.strip() if has_content else _EDITOR_NONE


# ─────────────────────────────────────────────────────────────────────────
# Извикване на модела + парсване на изхода
# ─────────────────────────────────────────────────────────────────────────

def load_system_prompt(path=PROMPT_FILE):
    """Изважда съдържанието на ПЪРВИЯ ограден код-блок (```) от .md файла."""
    blocks = re.findall(r"```(.*?)```", Path(path).read_text(encoding="utf-8"),
                        re.DOTALL)
    if not blocks:
        raise ValueError(f"Няма код-блок в {path}")
    return blocks[0].strip("\n")


def _client():
    # max_retries=0 — нашият retry (src/retry.py) е единственият механизъм.
    from openai import OpenAI
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError(
            "Липсва OPENAI_API_KEY. Копирай .env.example в .env и сложи ключа.")
    return OpenAI(max_retries=0)


def _reasoning_model(model):
    """Моделите от gpt-5.x / o-серията приемат само temperature по подразбиране."""
    return model.lower().startswith(("gpt-5", "o1", "o3", "o4"))


def call_model(client, model, system, user, temperature, max_tokens):
    """Извиква chat.completions устойчиво към разликите между моделите.

    Всяко извикване е обвито в retry за преходни OpenAI грешки. Адаптацията на
    параметрите (max_tokens/temperature) остава — тя реагира на ПОСТОЯННИ
    BadRequest грешки, които retry не повтаря, тъй че няма двойно повтаряне.
    """
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": user}]
    kwargs = {"model": model, "messages": messages,
              "max_completion_tokens": max_tokens}
    if not _reasoning_model(model):
        kwargs["temperature"] = temperature

    def _create(kw):
        return retry_call(lambda: client.chat.completions.create(**kw),
                          label="openai chat")

    try:
        return _create(kwargs)
    except Exception as ex:
        msg = str(ex).lower()
        param_issue = ("max_completion_tokens" in msg or "max_tokens" in msg
                       or "temperature" in msg)
        if not param_issue:
            raise   # преходните вече са изчерпани от retry; други → нагоре
        if "max_completion_tokens" in msg or "max_tokens" in msg:
            kwargs.pop("max_completion_tokens", None)
            kwargs["max_tokens"] = max_tokens
        if "temperature" in msg:
            kwargs.pop("temperature", None)
        return _create(kwargs)


def parse_output(text):
    """Разделя изхода на (headline, body, editor_notes)."""
    if not text:
        return None, None, None
    text = text.strip()

    hm = re.search(r"(?im)^\s*HEADLINE:\s*(.+?)\s*$", text)
    headline = hm.group(1).strip().strip('"').strip() if hm else None

    parts = re.split(r"(?m)^\s*-{3,}\s*$", text, maxsplit=1)
    head, notes = parts[0], (parts[1].strip() if len(parts) > 1 else "")

    if len(parts) == 1:  # няма ред „---“ — пробваме да отрежем по EDITOR NOTES
        m2 = re.search(r"(?im)^\s*EDITOR NOTES\b.*$", head)
        if m2:
            notes = head[m2.start():].strip()
            head = head[:m2.start()]

    body = head[hm.end():] if hm else head
    return headline, body.strip(), notes


def combine_pair(pair, client, system_prompt):
    """Чисти телата, праща ги на модела и връща изчистения резултат или None."""
    a_body = clean_body(pair["a_body"])[:MAX_BODY_CHARS]
    b_body = clean_body(pair["b_body"])[:MAX_BODY_CHARS]
    user = USER_TEMPLATE.format(a_body=a_body, b_body=b_body)

    resp = call_model(client, MODEL, system_prompt, user, TEMPERATURE, MAX_TOKENS)
    headline, body, notes = parse_output(resp.choices[0].message.content)
    if not headline or not body:
        return None

    return {
        "headline": headline,
        "body": strip_em_dashes(body),            # тялото остава без маркери
        "editor_notes": normalize_editor_notes(notes),
    }


# ─────────────────────────────────────────────────────────────────────────
# B. Дедупликация на събития (numpy, груба сила върху скорошните публикувани)
# ─────────────────────────────────────────────────────────────────────────

def _normalize(vec):
    arr = np.asarray(vec, dtype=np.float64)
    n = np.linalg.norm(arr)
    return arr / n if n else arr


def _max_cosine(vec, published_matrix):
    """Най-голямата косинусова прилика на vec спрямо публикуваните вектори."""
    if published_matrix is None or len(published_matrix) == 0:
        return 0.0
    return float(np.max(published_matrix @ _normalize(vec)))


def run_combine(conn, window_hours=DEDUP_WINDOW_HOURS, threshold=SIM_THRESHOLD,
                limit=None, max_combines=None):
    """Дедупликация + обединяване на готовите двойки. Връща отчет.

    max_combines: предпазен таван на LLM-извикванията за това пускане (None →
    MAX_COMBINES_PER_RUN от средата, по подразбиране 10). Дублиращите се двойки
    (без извикване) НЕ броят срещу тавана.
    """
    cap = max_combines if max_combines is not None else _max_combines()
    todo = db.pairs_to_combine(conn)
    skipped_missing = db.count_pairs_missing_body(conn)
    if limit:
        todo = todo[:limit]

    # Котва: статии от вече публикувани събития (минали пускания, скорошни).
    published = [_normalize(e["embedding"])
                 for e in db.published_event_embeddings(conn, window_hours)]
    pub_matrix = np.array(published) if published else None

    system_prompt = load_system_prompt()
    client = _client() if todo else None

    produced, duplicates, failed, calls, cap_hit = [], 0, 0, 0, False
    for pair in todo:
        label = f"#{pair['match_id']} [{pair['a_source']} + {pair['b_source']}]"
        a_emb, b_emb = json.loads(pair["a_emb"]), json.loads(pair["b_emb"])

        # B. Дедупликация: прилича ли на вече публикувано събитие?
        if (_max_cosine(a_emb, pub_matrix) >= threshold
                or _max_cosine(b_emb, pub_matrix) >= threshold):
            db.mark_combine_status(conn, pair["match_id"], "duplicate")
            conn.commit()
            duplicates += 1
            print(f"  ⤫ {label}: дубликат на публикувано събитие — прескочено")
            continue

        # Предпазен таван: спираме преди следващото LLM-извикване.
        if calls >= cap:
            cap_hit = True
            print(f"  ⚠ достигнат таван за пускане ({cap} обединявания) — "
                  f"спирам; останалите двойки чакат следващия цикъл")
            break

        try:
            result = combine_pair(pair, client, system_prompt)
            calls += 1
        except Exception as ex:
            calls += 1  # извикването е направено (разход) дори при грешка
            print(f"  ✗ {label}: грешка от модела — {type(ex).__name__}: {ex}")
            failed += 1
            continue
        if not result:
            print(f"  ✗ {label}: неуспешно парсване на изхода")
            failed += 1
            continue

        db.save_combined(conn, pair["match_id"], result["headline"],
                         result["body"], MODEL, result["editor_notes"],
                         pair["a_url"], pair["b_url"])
        db.mark_combine_status(conn, pair["match_id"], "combined")
        conn.commit()
        produced.append({"pair": pair, **result})

        # Добавяме статиите към котвата → хваща дубли в СЪЩОТО пускане.
        published.extend([_normalize(a_emb), _normalize(b_emb)])
        pub_matrix = np.array(published)
        print(f"  ✓ {label}: {result['headline']}")

    return {
        "produced": produced,
        "duplicates": duplicates,
        "skipped_missing_body": skipped_missing,
        "failed": failed,
        "llm_calls": calls,
        "cap": cap,
        "cap_hit": cap_hit,
    }


def _print_sample(produced, n=1):
    """Печата 1 обединен материал изцяло — за бърза преценка."""
    if not produced:
        return
    print(f"\n{'═'*72}\n  ПРИМЕРЕН ОБЕДИНЕН МАТЕРИАЛ\n{'═'*72}")
    for item in produced[:n]:
        p = item["pair"]
        print(f"\n— Източници:")
        print(f"    [{p['a_source']}] {p['a_headline']}")
        print(f"    [{p['b_source']}] {p['b_headline']}")
        print(f"\n  ЗАГЛАВИЕ: {item['headline']}\n")
        print(f"  ТЯЛО:\n")
        for para in item["body"].split("\n"):
            print(f"    {para}")
        print(f"\n  {item['editor_notes'].splitlines()[0] if item['editor_notes'] else ''}")
        print(f"{'─'*72}")


def main():
    parser = argparse.ArgumentParser(description="Обединяване + дедупликация (Фаза 6).")
    parser.add_argument("--reset", action="store_true",
                        help="САМО ТЕСТ: трие обединените и нулира combine_status")
    parser.add_argument("--limit", type=int, default=None,
                        help="максимум двойки за ОБЕДИНЯВАНЕ този път")
    parser.add_argument("--send-limit", type=int, default=None,
                        help="максимум материали за ПРАЩАНЕ този път (напр. 1)")
    parser.add_argument("--dry-run", action="store_true",
                        help="ревю-имейлите само се логват, не се пращат")
    args = parser.parse_args()

    print("\n=== Обединяване + дедупликация + ревю-имейл (Фаза 6) ===\n")
    conn = db.connect()

    if args.reset:
        db.reset_combinations(conn)
        print("--reset: изтрити стари обединения, combine_status нулиран.\n")

    skipped_missing = db.count_pairs_missing_body(conn)
    if skipped_missing:
        print(f"Без тяло (прескачаме): {skipped_missing} двойки\n")

    report = run_combine(conn, limit=args.limit)

    # D. Ревю-имейл за новопроизведените (непратени) материали.
    print("\nРевю-имейл:")
    email_report = notify.send_unsent(conn, limit=args.send_limit,
                                      dry_run=args.dry_run)

    _print_sample(report["produced"], n=1)

    print(f"\n── Отчет (Фаза 6) ──")
    print(f"Обединени този път      : {len(report['produced'])}")
    print(f"Прескочени (дубликат)   : {report['duplicates']}")
    print(f"Прескочени (без тяло)   : {report['skipped_missing_body']}")
    print(f"Неуспешни (модел/парс)  : {report['failed']}")
    print(f"LLM извиквания / таван  : {report['llm_calls']} / {report['cap']}"
          + ("  ⚠ ТАВАНЪТ Е ДОСТИГНАТ" if report["cap_hit"] else ""))
    mode = "would-email (dry-run)" if email_report["dry_run"] else "пратени"
    print(f"Имейли ({mode}): "
          f"{email_report['sent'] + email_report['would_send']}")
    print(f"Общо обединени в база    : {db.combined_count(conn)}")
    print(f"Общо дубликати в база    : {db.duplicate_count(conn)}")
    print()

    conn.close()


if __name__ == "__main__":
    main()
