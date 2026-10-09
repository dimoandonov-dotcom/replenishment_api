"""
Автоматично опресняване на наличностите от Мистрал на всеки 30 минути
(07:00-22:00 бг. време), вътре в самия API процес - без отделен cron.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

SOFIA = timezone(timedelta(hours=3))
EVERY_MIN = 30
HOURS = (7, 22)
_state = {"last_run": None, "last_result": None, "last_error": None, "running": False}


def status() -> dict:
    f = lambda d: d.astimezone(SOFIA).strftime("%d.%m %H:%M") if d else None  # noqa: E731
    return {"auto_every_min": EVERY_MIN, "auto_hours": f"{HOURS[0]:02d}:00–{HOURS[1]:02d}:00",
            "auto_last_run": f(_state["last_run"]), "auto_last_error": _state["last_error"],
            "auto_last_inserted": (_state["last_result"] or {}).get("inserted")}


def _tick():
    from . import mistral
    from .db import SessionLocal
    if not mistral.configured():
        return
    with SessionLocal() as db:
        try:
            _state["last_result"] = mistral.sync_stock(db)
            _state["last_error"] = None
            # на всеки 2 часа: движенията и документите за 16 дни -> поправените аномалии стават зелени още същия ден
            n = _state.get("ticks", 0) + 1
            _state["ticks"] = n
            if n % 4 == 1:
                from . import anomalies
                anomalies.sync(db, 16)
                anomalies.detect_duplicate_docs(db, 16)
        except Exception as e:  # пробваме пак след 30 мин
            _state["last_error"] = f"{type(e).__name__}: {e}"[:300]
    _state["last_run"] = datetime.now(timezone.utc)


def _loop():
    time.sleep(60)  # нека сървърът тръгне напълно
    while True:
        now = datetime.now(SOFIA)
        if HOURS[0] <= now.hour < HOURS[1]:
            last = _state["last_run"]
            if last is None or datetime.now(timezone.utc) - last >= timedelta(minutes=EVERY_MIN):
                _tick()
        time.sleep(60)


def start():
    if _state["running"]:
        return
    _state["running"] = True
    threading.Thread(target=_loop, name="stock-refresher", daemon=True).start()
