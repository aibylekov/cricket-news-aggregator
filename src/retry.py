# -*- coding: utf-8 -*-
"""
Малък преизползваем retry за ПРЕХОДНИ външни грешки (Фаза 8 — заздравяване).

Цел: транзиентните сривове по мрежата (Google News 503, OpenAI
APIConnectionError, Zyte/SMTP хикове) да не свалят цял цикъл, докато системата
работи без надзор.

Правила:
  - До `attempts` опита (по подразбиране 3), експоненциален backoff с jitter
    (~1s, 2s, 4s). Всеки повторен опит се логва, за да се вижда в Actions.
  - Повтаряме САМО при преходни грешки; при постоянни се проваляме веднага
    (лош ключ не се опитва три пъти).

Повтаряме на:
  - мрежови/connection грешки и таймаути (requests, socket, builtin);
  - HTTP 429 и 5xx (по статус кода на изключението);
  - OpenAI: APIConnectionError, APITimeoutError, RateLimitError, InternalServerError.
НЕ повтаряме (fail fast):
  - HTTP 4xx освен 429 (401/403/400/404…);
  - OpenAI: AuthenticationError, BadRequestError, PermissionDeniedError, NotFoundError;
  - SMTP: SMTPAuthenticationError.
"""

import functools
import random
import socket
import smtplib
import time

# requests/openai са в requirements, но внасяме предпазливо, за да е този модул
# годен за import и там, където някой от тях липсва.
try:
    import requests
except ImportError:
    requests = None
try:
    import openai
except ImportError:
    openai = None


DEFAULT_ATTEMPTS = 3       # общо опита (2 повторения)
DEFAULT_BASE_DELAY = 1.0   # секунди преди първото повторение
DEFAULT_FACTOR = 2.0       # 1s → 2s → 4s
DEFAULT_JITTER = 0.3       # до +30% случайно разсейване, за да не удрят в такт


def _status_of(ex):
    """HTTP статус код, ако изключението носи такъв (requests.HTTPError или
    openai.APIStatusError)."""
    resp = getattr(ex, "response", None)
    code = getattr(resp, "status_code", None)
    if code is None:
        code = getattr(ex, "status_code", None)
    return code


def is_transient(ex):
    """True само за ПРЕХОДНИ грешки, които си струва да повторим."""
    # --- изрично ПОСТОЯННИ → провал веднага ---
    if openai is not None and isinstance(ex, (
            openai.AuthenticationError, openai.BadRequestError,
            openai.PermissionDeniedError, openai.NotFoundError)):
        return False
    if isinstance(ex, smtplib.SMTPAuthenticationError):
        return False

    # --- изрично ПРЕХОДНИ → повтаряме ---
    if openai is not None and isinstance(ex, (
            openai.APIConnectionError, openai.APITimeoutError,
            openai.RateLimitError, openai.InternalServerError)):
        return True
    if requests is not None and isinstance(ex, (
            requests.ConnectionError, requests.Timeout)):
        return True
    if isinstance(ex, (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError)):
        return True
    if isinstance(ex, (socket.timeout, ConnectionError, TimeoutError)):
        return True

    # --- по HTTP статус код: 429 и 5xx преходни; други 4xx постоянни ---
    code = _status_of(ex)
    if code is not None:
        return code == 429 or 500 <= code < 600

    # --- SMTP отговор с код: 4xx временно, 5xx постоянно ---
    if isinstance(ex, smtplib.SMTPResponseException):
        return 400 <= ex.smtp_code < 500

    # непозната грешка → не рискуваме сляпо повтаряне
    return False


def retry_call(fn, *, attempts=DEFAULT_ATTEMPTS, base_delay=DEFAULT_BASE_DELAY,
               factor=DEFAULT_FACTOR, jitter=DEFAULT_JITTER, label="",
               sleep=time.sleep):
    """Извиква fn(); повтаря само при преходни грешки, до `attempts` пъти.

    Постоянните грешки се пускат нагоре веднага. Backoff-ът е ограничен (кратък),
    тъй че retry-ите не бавят излишно и не заобикалят предпазните тавани — те
    броят логически операции, не мрежови опити.
    """
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as ex:
            if attempt >= attempts or not is_transient(ex):
                raise
            delay = base_delay * (factor ** (attempt - 1))
            delay += random.uniform(0, jitter * delay)
            print(f"  ↻ retry [{label}] опит {attempt}/{attempts} неуспешен: "
                  f"{type(ex).__name__}: {ex} — нов опит след {delay:.1f}s",
                  flush=True)
            sleep(delay)


def retry(**opts):
    """Декоратор около retry_call (label по подразбиране = името на функцията)."""
    def deco(fn):
        call_opts = {k: v for k, v in opts.items() if k != "label"}
        label = opts.get("label", fn.__name__)

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            return retry_call(lambda: fn(*args, **kwargs), label=label, **call_opts)
        return wrapper
    return deco
