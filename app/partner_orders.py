"""
Заявки от партньори през таен линк (без вход): Бургас (досега по Viber), Мария - Хляб и кифли (досега по имейл).
Всеки линк вижда само своите обекти и артикули. Предлага бройките от последната му заявка за същия ден от седмицата
(не от MinMaxAI - за да е честно сравнението). Записаното влиза в сравнението „Бандитс vs Мони“ като истинската им заявка.
"""
from __future__ import annotations

import json
import secrets
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from .db import get_db

SOFIA = timezone(timedelta(hours=3))
router = APIRouter()
WD = ["понеделник", "вторник", "сряда", "четвъртък", "петък", "събота", "неделя"]


def ensure(db: Session):
    from . import bandits_orders as bo
    bo.ensure(db)
    db.execute(text("""CREATE TABLE IF NOT EXISTS bd_partner (token TEXT PRIMARY KEY, name TEXT, stores TEXT, codes TEXT,
                       created_at TIMESTAMPTZ DEFAULT now())"""))
    db.execute(text("""CREATE TABLE IF NOT EXISTS bd_porders (delivery_day DATE, store_id INT, code INT, qty NUMERIC(10,2),
                       partner TEXT, at TIMESTAMPTZ DEFAULT now(), PRIMARY KEY (delivery_day, store_id, code))"""))
    db.commit()


def create(db: Session, name: str, stores: list[int] | None, codes: list[int] | None) -> dict:
    ensure(db)
    tok = secrets.token_urlsafe(12)
    db.execute(text("INSERT INTO bd_partner(token, name, stores, codes) VALUES (:t,:n,:s,:c)"),
               {"t": tok, "n": name, "s": json.dumps(stores or []), "c": json.dumps(codes or [])})
    db.commit()
    return {"name": name, "token": tok, "path": f"/z/{tok}"}


def listing(db: Session) -> list[dict]:
    ensure(db)
    return [{"name": n, "path": f"/z/{t}", "stores": len(json.loads(s or "[]")), "codes": json.loads(c or "[]")}
            for t, n, s, c in db.execute(text("SELECT token, name, stores, codes FROM bd_partner ORDER BY created_at")).all()]


def _partner(db: Session, tok: str):
    ensure(db)
    r = db.execute(text("SELECT name, stores, codes FROM bd_partner WHERE token = :t"), {"t": tok}).first()
    if not r:
        raise HTTPException(404, "Няма такъв линк")
    return r[0], set(json.loads(r[1] or "[]")), set(json.loads(r[2] or "[]"))


def _form_data(db: Session, tok: str, day: date) -> dict:
    from . import bandits_orders as bo
    name, stores, codes = _partner(db, tok)
    st = {i: n for i, n in db.execute(text("SELECT loc, name FROM bd_stores WHERE active ORDER BY name")).all() if not stores or i in stores}
    if codes and not stores:   # напр. Мария: само обектите, които получават нейните артикули
        got_st = {s_ for (s_,) in db.execute(text("SELECT DISTINCT store_id FROM bd_soft WHERE bcode = ANY(:c) AND day >= :d"),
                                              {"c": list(codes), "d": day - timedelta(days=35)}).all()}
        st = {i: n for i, n in st.items() if i in got_st}
    sch = bo.schedule(db)
    st = {i: n for i, n in st.items() if day.weekday() in (sch.get(i) or {0, 1, 3, 4})}
    arts = {k: (n, s) for k, n, s in db.execute(text("SELECT code, name, sub FROM bd_articles WHERE active ORDER BY sub, name")).all()
            if (not codes or k in codes) and k not in bo.STOPPED}
    if not codes:   # без изрично зададени артикули - тези, които обектите реално получават
        got = {c for (c,) in db.execute(text("SELECT DISTINCT bcode FROM bd_soft WHERE store_id = ANY(:s) AND day >= :d"),
                                        {"s": list(st), "d": day - timedelta(days=35)}).all()}
        arts = {k: v for k, v in arts.items() if k in got and k not in bo.VEGA}
    saved = {(s_, c): float(q) for s_, c, q in db.execute(text(
        "SELECT store_id, code, qty FROM bd_porders WHERE delivery_day = :d"), {"d": day}).all()}
    # предложение: тяхната заявка за същия ден от миналата седмица, иначе изписаното тогава
    prev = {}
    for back in (7, 14):
        pd = day - timedelta(days=back)
        for s_, c, q in db.execute(text("SELECT store_id, code, qty FROM bd_porders WHERE delivery_day = :d"), {"d": pd}).all():
            prev.setdefault((s_, c), float(q))
        for s_, c, q in db.execute(text("SELECT store_id, bcode, delivered FROM bd_soft WHERE day = :d"), {"d": pd}).all():
            prev.setdefault((s_, c), float(q or 0))
        if prev:
            break
    rows = []
    for i, n in st.items():
        rows.append({"store_id": i, "store": n, "items": [
            {"code": k, "qty": saved.get((i, k), prev.get((i, k), 0))} for k in arts]})
    return {"name": name, "day": day.isoformat(), "weekday": WD[day.weekday()], "saved": bool(saved),
            "articles": [{"code": k, "name": v[0], "sub": v[1] or ""} for k, v in arts.items()], "stores": rows}


class OrderIn(BaseModel):
    day: str
    lines: list[dict]


@router.get("/z/{tok}/data")
def z_data(tok: str, day: str | None = None, db: Session = Depends(get_db)):
    d = date.fromisoformat(day) if day else datetime.now(SOFIA).date() + timedelta(days=1)
    return _form_data(db, tok, d)


@router.post("/z/{tok}/order")
def z_order(tok: str, p: OrderIn, db: Session = Depends(get_db)):
    name, stores, codes = _partner(db, tok)
    d = date.fromisoformat(p.day)
    if d < datetime.now(SOFIA).date():
        raise HTTPException(400, "Заявка за минала дата не може да се променя")
    allowed_st = {i for (i,) in db.execute(text("SELECT loc FROM bd_stores")).all() if not stores or i in stores}
    n = 0
    for ln in p.lines:
        s_, c, q = int(ln.get("store_id")), int(ln.get("code")), float(ln.get("qty") or 0)
        if s_ not in allowed_st or (codes and c not in codes) or q < 0 or q > 999:
            continue
        db.execute(text("""INSERT INTO bd_porders(delivery_day, store_id, code, qty, partner) VALUES (:d,:s,:c,:q,:p)
                           ON CONFLICT (delivery_day, store_id, code) DO UPDATE SET qty = EXCLUDED.qty, partner = EXCLUDED.partner, at = now()"""),
                   {"d": d, "s": s_, "c": c, "q": q, "p": name})
        n += 1
    db.commit()
    try:   # известие до Димо
        from . import telegram
        tot = sum(float(ln.get("qty") or 0) for ln in p.lines)
        if telegram.token():
            telegram._ensure(db)
            for cid, u in db.execute(text("SELECT chat_id, username FROM tg_users WHERE username IS NOT NULL")).all():
                if (u or "").lower() == "dimo":
                    telegram.send(int(cid), f"🧾 {name} пусна заявка за {WD[d.weekday()]} {d.strftime('%d.%m')}: {tot:g} бр. ({n} реда)")
    except Exception:
        db.rollback()
    return {"ok": True, "lines": n}


@router.get("/z/{tok}", response_class=HTMLResponse)
def z_page(tok: str, db: Session = Depends(get_db)):
    name, _, _ = _partner(db, tok)
    return HTMLResponse(PAGE.replace("__NAME__", name).replace("__TOK__", tok))


PAGE = """<!doctype html><html lang="bg"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>Заявка Бандитс - __NAME__</title>
<style>
:root{--ink:#1c1917;--mute:#78716c;--line:#e7e5e4;--acc:#B45309;--bg:#fafaf9}
body{margin:0;font:16px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--ink)}
.w{max-width:900px;margin:0 auto;padding:16px}
h1{font-size:22px;margin:0 0 4px}.m{color:var(--mute);font-size:14px}
.bar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:14px 0}
input[type=date],button{font:inherit;padding:10px 12px;border-radius:10px;border:1px solid var(--line)}
button.go{background:var(--acc);color:#fff;border-color:var(--acc);font-weight:700;padding:12px 18px}
.st{background:#fff;border:1px solid var(--line);border-radius:12px;margin:0 0 10px;padding:10px 12px}
.st h2{font-size:16px;margin:0 0 8px}
.g{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:6px 12px}
.it{display:flex;justify-content:space-between;align-items:center;gap:8px;font-size:14px}
.it input{width:64px;font:inherit;padding:6px;border:1px solid var(--line);border-radius:8px;text-align:right}
.ok{background:#ecfdf3;border:1px solid #16a34a;color:#166534;padding:10px 12px;border-radius:10px}
.sticky{position:sticky;bottom:0;background:var(--bg);padding:10px 0;border-top:1px solid var(--line)}
</style></head><body><div class="w">
<h1>🥪 Заявка към Бандитс</h1><div class="m">__NAME__ · попълни бройките и натисни „Изпрати заявката“. Предложени са бройките от миналата седмица.</div>
<div class="bar"><label>Доставка на <input type="date" id="day"></label><span class="m" id="info"></span></div>
<div id="msg"></div><div id="list">Зареждам…</div>
<div class="sticky"><button class="go" id="send">Изпрати заявката</button> <span class="m" id="tot"></span></div>
</div><script>
const T="__TOK__"; const $=i=>document.getElementById(i); let D=null;
function tomorrow(){const d=new Date();d.setDate(d.getDate()+1);return d.toISOString().slice(0,10)}
async function load(){ $("list").textContent="Зареждам…"; $("msg").innerHTML="";
  const r=await fetch(`/z/${T}/data?day=${$("day").value}`); D=await r.json();
  $("info").textContent=`${D.weekday}${D.saved?" · вече има изпратена заявка - може да я промениш":""}`;
  if(!D.stores.length){$("list").innerHTML='<p class="m">Няма доставка за този ден.</p>';tot();return}
  const A=Object.fromEntries(D.articles.map(a=>[a.code,a]));
  $("list").innerHTML=D.stores.map(s=>`<div class="st"><h2>${s.store}</h2><div class="g">${s.items.map(i=>`<label class="it"><span>${A[i.code].name}</span><input type="number" min="0" max="999" inputmode="numeric" data-s="${s.store_id}" data-c="${i.code}" value="${i.qty||""}" placeholder="0"></label>`).join("")}</div></div>`).join("");
  document.querySelectorAll("input[data-s]").forEach(x=>x.oninput=tot); tot();
}
function tot(){let t=0;document.querySelectorAll("input[data-s]").forEach(x=>t+=+x.value||0);$("tot").textContent=`общо ${t} бр.`}
$("send").onclick=async()=>{ const lines=[...document.querySelectorAll("input[data-s]")].map(x=>({store_id:+x.dataset.s,code:+x.dataset.c,qty:+x.value||0}));
  $("send").disabled=true; const r=await fetch(`/z/${T}/order`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({day:$("day").value,lines})});
  const j=await r.json(); $("send").disabled=false;
  $("msg").innerHTML=r.ok?`<div class="ok">✅ Заявката е изпратена (${j.lines} реда). Благодарим!</div>`:`<div class="ok" style="background:#fef2f2;border-color:#dc2626;color:#991b1b">Грешка: ${j.detail||r.status}</div>`; scrollTo(0,0);};
$("day").value=tomorrow(); $("day").onchange=load; load();
</script></body></html>"""
