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
            "auto_last_inserted": (_state["last_result"] or {}).get("inserted"),
            "dispatch_0930": _state.get("dispatch_result"), "ocr_alerts": _state.get("ocr_alerts"), "pilot_report": _state.get("pilot_report")}


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
            from . import anomalies
            if n % 4 == 1:
                anomalies.sync(db, 16)
                anomalies.detect_duplicate_docs(db, 16)
            elif 8 <= datetime.now(SOFIA).hour < 20:
                anomalies.sync(db, 3)          # днешните фактури -> сигнал за OCR замяна до 30 мин
        except Exception as e:  # пробваме пак след 30 мин
            db.rollback()
            _state["last_error"] = f"{type(e).__name__}: {e}"[:300]
        # пилотните магазини под лупа: справка в 10:00 и 18:00
        now_ = datetime.now(SOFIA)
        pslot = max([h for h in (10, 18) if now_.hour >= h], default=None)
        if pslot is not None and _state.get("pilot_slot") != (now_.date(), pslot):
            _state["pilot_slot"] = (now_.date(), pslot)
            try:
                from . import pilotreport
                _state["pilot_report"] = {"at": now_.strftime("%d.%m %H:%M"), **{k: v for k, v in pilotreport.send(db).items() if k == "sent"}}
            except Exception as e:
                db.rollback()
                _state["pilot_report"] = f"{type(e).__name__}: {e}"[:300]
        # грешно заведени фактури на НДК -> съобщение до Наде (и Димо)
        if 8 <= datetime.now(SOFIA).hour < 20:
            try:
                from . import ocralert
                r = ocralert.scan(db)
                _state["ocr_alerts"] = {"at": datetime.now(SOFIA).strftime("%d.%m %H:%M"), "found": r["found"],
                                        "new": r["new"], "sent_to": r["sent_to"], "patterns": len(r["patterns"])}
            except Exception as e:
                db.rollback()
                _state["ocr_alerts"] = f"{type(e).__name__}: {e}"[:300]
        # в 08:00, 12:00 и 16:00: вчерашните продажби пак (закъснели бележки, особено 24-часовите) и отчетът за оборотите
        now = datetime.now(SOFIA)
        slot = max([h for h in (8, 12, 16) if now.hour >= h], default=None)   # 08:00, 12:00, 16:00 - закъснели бонове
        if slot is not None and _state.get("morning") != (now.date(), slot):
            try:
                from . import turnover
                _state["morning_result"] = turnover.morning(db)
                _state["morning"] = (now.date(), slot)
            except Exception as e:
                db.rollback()
                _state["morning_result"] = f"{type(e).__name__}: {e}"[:300]
                _state["morning"] = (now.date(), slot)
        # 12:30 - сутрешната справка с оборотите в Telegram/Viber (на Асен)
        if (now.hour, now.minute) >= (12, 30) and _state.get("viber_day") != now.date():
            _state["viber_day"] = now.date()
            try:
                from . import telegram, viber
                _state["telegram_result"] = telegram.daily(db)
                _state["viber_result"] = viber.daily(db)
            except Exception as e:
                db.rollback()
                _state["viber_result"] = f"{type(e).__name__}: {e}"[:300]
        # 13:00 - дневният преглед на заявките (MinMaxAI срещу Ани) в Telegram на Димо
        if (now.hour, now.minute) >= (13, 0) and _state.get("review_day") != now.date():
            _state["review_day"] = now.date()
            try:
                from . import compare, telegram
                compare.recalc_day(db, now.date())
                _state["review_result"] = telegram.review_daily(db)
            except Exception as e:
                db.rollback()
                _state["review_result"] = f"{type(e).__name__}: {e}"[:300]
    _state["last_run"] = datetime.now(timezone.utc)


def _dispatch_check():
    """09:30 българско време: дневната заявка на MinMaxAI (за утре), ако днес още не е пусната.
    НДК изписва фактурите за днешната доставка към 8-9 ч. -> в 09:30 наличността вече ги включва."""
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Europe/Sofia")
    loc = datetime.now(tz)
    if not ((loc.hour, loc.minute) >= (9, 30) and loc.hour < 13) or _state.get("dispatch_day") == loc.date():
        return
    from sqlalchemy import select
    from . import models as m, mistral, service, alerts
    from .db import SessionLocal
    start = datetime.combine(loc.date(), datetime.min.time(), tz)
    with SessionLocal() as db:
        try:
            if db.execute(select(m.DispatchRun.id).where(m.DispatchRun.started_at >= start)).first():
                _state["dispatch_day"] = loc.date()          # днес вече е пусната (ръчно или от cron)
                return
            if mistral.configured():
                mistral.sync_stock(db)                        # свежа наличност - с днешните фактури
            run = service.run_dispatch(db, loc.date(), None, None, True)
            _state["dispatch_day"] = loc.date()
            _state["dispatch_result"] = {"run_id": run.id, "orders": run.orders_created,
                                         "lines": run.order_lines_created, "at": loc.strftime("%d.%m %H:%M")}
            try:
                alerts.scan_all(db, None, False)
            except Exception:
                db.rollback()
        except Exception as e:
            db.rollback()
            _state["dispatch_result"] = f"{type(e).__name__}: {e}"[:300]
            _state["dispatch_day"] = loc.date()               # да не се върти на всяка минута; виж статуса


def _loop():
    time.sleep(60)  # нека сървърът тръгне напълно
    while True:
        try:
            _dispatch_check()
        except Exception as e:
            _state["dispatch_result"] = f"{type(e).__name__}: {e}"[:300]
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
