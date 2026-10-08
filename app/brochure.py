"""
Качване на брошура (Excel или PDF): чете датите и артикулите и ги свързва с нашите кодове.
Excel - по колона с код (сигурно). PDF - по текста: име + разфасовка се сравняват с
имената на артикулите (не е 100% - затова има преглед преди запис).
"""
from __future__ import annotations

import io
import re
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models as m

SOFIA = timezone(timedelta(hours=3))

_DATE_RANGE = re.compile(
    r"(\d{1,2})\s*[./]\s*(\d{1,2})(?:\s*[./]\s*(\d{2,4}))?\.?\s*(?:г\.?)?\s*(?:-|–|—|до)\s*"
    r"(\d{1,2})\s*[./]\s*(\d{1,2})(?:\s*[./]\s*(\d{2,4}))?", re.I)

_LAT = {"A": "А", "B": "Б", "C": "К", "D": "Д", "E": "Е", "F": "Ф", "G": "Г", "H": "Х", "I": "И", "J": "Й",
        "K": "К", "L": "Л", "M": "М", "N": "Н", "O": "О", "P": "П", "Q": "К", "R": "Р", "S": "С", "T": "Т",
        "U": "У", "V": "В", "W": "В", "X": "КС", "Y": "Й", "Z": "З"}
_STOP = {"БИРА", "НАПИТКА", "ГАЗИРАНА", "ВОДА", "КЕН", "СТЪКЛО", "ПЕТ", "БУТИЛКА", "БР", "ЗА", "И", "С", "ОТ",
         "ЛВ", "ЕВРО", "МЛ", "Л", "ГР", "КГ", "БР", "ЦЕНА", "ПРОМО", "НОВО", "САМО", "СОК", "ПВЦ", "ПРЕП", "СЕЗОН", "ЛЯТО"}


def _year(y: str | None, today: date) -> int:
    if not y:
        return today.year
    y = int(y)
    return y + 2000 if y < 100 else y


def find_dates(text_: str, today: date | None = None) -> tuple[date, date] | None:
    """Първият правдоподобен период „дд.мм – дд.мм(.гггг)“ в текста."""
    today = today or datetime.now(SOFIA).date()
    for g in _DATE_RANGE.finditer(text_ or ""):
        d1, m1, y1, d2, m2, y2 = g.groups()
        try:
            ye = _year(y2 or y1, today)
            e = date(ye, int(m2), int(d2))
            s = date(_year(y1, today) if y1 else ye, int(m1), int(d1))
            if s > e:
                if y2 or y1:
                    s = date(s.year - 1, s.month, s.day)
                else:
                    e = date(e.year + 1, e.month, e.day)
        except ValueError:
            continue
        if 0 <= (e - s).days <= 62 and abs((s - today).days) <= 90:
            return s, e
    return None


def _norm(t: str) -> str:
    t = (t or "").upper().replace("Ё", "Е")
    t = "".join(_LAT.get(c, c) for c in t)
    t = re.sub(r"(\d)[,.](\d)", r"\1.\2", t)
    return t


def _vol(t: str) -> str | None:
    """Разфасовка в мл: 330МЛ, 0.5Л, 2Л, 1,5 л -> '330', '500', '2000', '1500'."""
    g = re.search(r"(\d+(?:\.\d+)?)\s*(МЛ|Л)(?![А-Я])", t)
    if not g:
        return None
    v = float(g.group(1)) * (1 if g.group(2) == "МЛ" else 1000)
    return str(int(round(v)))


def _words(t: str) -> set[str]:
    return {w for w in re.findall(r"[А-Я]{2,}", t) if w not in _STOP}


def _articles(db: Session) -> list[dict]:
    out = []
    for a in db.execute(select(m.Article).where(m.Article.is_active.is_(True))).scalars().all():
        n = _norm(a.supplier_name or a.name)
        n = re.sub(r"/.*$", "", n)       # без кодовете след „/“
        out.append({"sku": a.sku, "name": a.supplier_name or a.name, "words": _words(n), "vol": _vol(n)})
    return out


def match_lines(db: Session, lines: list[str]) -> list[dict]:
    """Всеки ред от брошурата -> най-добрият артикул (по думи + разфасовка)."""
    arts = _articles(db)
    best: dict[str, dict] = {}
    for raw in lines:
        t = _norm(raw)
        w, v = _words(t), _vol(t)
        if len(w) < 1:
            continue
        price = None
        pg = re.findall(r"(\d+[.,]\d{2})", raw)
        if pg:
            price = float(pg[-1].replace(",", "."))
        for a in arts:
            common = w & a["words"]
            if not common:
                continue
            if v and a["vol"] and v != a["vol"]:
                continue
            score = len(common) / max(len(a["words"]), 1)
            if v and a["vol"] == v:
                score += 0.35
            if score < 0.6:
                continue
            cur = best.get(a["sku"])
            if cur is None or score > cur["score"]:
                best[a["sku"]] = {"sku": a["sku"], "name": a["name"], "found": raw.strip()[:120],
                                  "price": price, "score": round(min(score, 1.5), 2)}
    rows = sorted(best.values(), key=lambda r: -r["score"])
    for r in rows:
        r["sure"] = r["score"] >= 1.0
    return rows


def parse_xlsx(db: Session, data: bytes) -> dict:
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    known = {a.sku: (a.supplier_name or a.name) for a in db.execute(select(m.Article)).scalars().all()}
    texts, items, lines = [], {}, []
    for ws in wb.worksheets:
        texts.append(ws.title)
        code_col = name_col = price_col = None
        for row in ws.iter_rows(values_only=True):
            cells = ["" if c is None else str(c).strip() for c in row]
            texts.extend(c for c in cells if c)
            low = [c.lower() for c in cells]
            if code_col is None and any(k in c for c in low for k in ("код", "мат", "sku", "артикулен")):
                code_col = next(i for i, c in enumerate(low) if any(k in c for k in ("код", "мат", "sku", "артикулен")))
                name_col = next((i for i, c in enumerate(low) if any(k in c for k in ("име", "наимен", "артикул", "стока"))
                                 and i != code_col), None)
                price_col = next((i for i, c in enumerate(low) if "цена" in c or "промо" in c), None)
                continue
            if code_col is not None and code_col < len(cells):
                c = re.sub(r"\.0$", "", cells[code_col])
                if c in known:
                    pr = None
                    if price_col is not None and price_col < len(cells):
                        try:
                            pr = float(cells[price_col].replace(",", "."))
                        except ValueError:
                            pr = None
                    items[c] = {"sku": c, "name": known[c], "found": cells[name_col] if name_col is not None
                                and name_col < len(cells) else "", "price": pr, "score": 1.5, "sure": True}
            else:
                lines.append(" ".join(cells))
    rows = list(items.values()) if items else match_lines(db, lines)
    return {"source": "xlsx", "by_code": bool(items), "dates": find_dates(" ".join(texts)), "items": rows}


def parse_pdf(db: Session, data: bytes) -> dict:
    import pdfplumber
    lines, chars = [], 0
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        pages = len(pdf.pages)
        for pg in pdf.pages:
            t = pg.extract_text() or ""
            chars += len(t.strip())
            lines.extend(x for x in t.splitlines() if x.strip())
    # имената често са на 2 реда - сливаме съседните
    joined = lines + [lines[i] + " " + lines[i + 1] for i in range(len(lines) - 1)]
    return {"source": "pdf", "pages": pages, "has_text": chars > 50 * max(pages, 1) // 4,
            "dates": find_dates(" ".join(lines)), "items": match_lines(db, joined) if lines else []}


def parse(db: Session, filename: str, data: bytes) -> dict:
    fn = (filename or "").lower()
    if fn.endswith(".pdf"):
        r = parse_pdf(db, data)
    elif fn.endswith((".xlsx", ".xlsm")):
        r = parse_xlsx(db, data)
    else:
        raise ValueError("Качете Excel (.xlsx) или PDF")
    d = r.pop("dates") or find_dates(filename)
    r["start"], r["end"] = (d[0].isoformat(), d[1].isoformat()) if d else (None, None)
    return r
