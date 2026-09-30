"""
Генериране на заявки във формата, който доставчикът очаква.

Бланка (по образец АНДЖИ-ГЛАДСТОН 12.xlsx):
    ArtNomer | Artikul | MerEd | Kol | Zabelejka

Име на файла:  <ОБЕКТ>-<АДРЕС>.xlsx   (напр. АНДЖИ-ГЛАДСТОН 10.xlsx)
Адресът влиза САМО в името на файла, не и в съдържанието.
"""
from __future__ import annotations

import re
import zipfile
from io import BytesIO

import openpyxl
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models as m

# Бланка по образец от НДК (АНДЖИ-ГЛАДСТОН 10.xlsx): 4 колони.
# Името на артикула е както е в ценовата листа на доставчика
# (supplier_name), ако е заредено - иначе нашето име.
HEADERS = ["ArtNomer", "Artikul", "MerEd", "Kol"]
UNIT_LABEL = "броя"


def _short_store_name(name: str) -> str:
    n = name.strip()
    n = re.sub(r"^\d+\.\s*", "", n)
    n = re.sub(r"^(м-н|магазин|супермаркет)\s+", "", n, flags=re.I)
    n = re.sub(r"^Алкохол и цигари\s*/\s*", "", n, flags=re.I)
    n = n.strip("/ ").strip()
    n = re.sub(r"\s*-\s*(пл\.|кв\.)\s*", " ", n)
    return n.upper()


def order_filename(store: m.Store) -> str:
    base = _short_store_name(store.name)
    if store.address:
        base = f"{base}-{store.address.strip().upper()}"
    base = re.sub(r'[\\/:*?"<>|]', "-", base)
    return f"{base}.xlsx"


def _write_workbook(rows) -> bytes:
    """rows: [(sku, name, qty)] -> .xlsx по бланката."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet 1"
    ws.append(HEADERS)
    out = []
    for sku, name, qty in rows:
        sku_val = int(sku) if str(sku).isdigit() else sku
        out.append((sku_val, name, UNIT_LABEL, int(qty)))
    out.sort(key=lambda r: (isinstance(r[0], str), r[0]))
    for r in out:
        ws.append(list(r))
    widths = {"A": 12, "B": 55, "C": 8, "D": 8}
    for col, w in widths.items():
        ws.column_dimensions[col].width = w
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _display_name(a) -> str:
    return (a.supplier_name or a.name or "").strip()


def build_order_workbook(db: Session, order: m.PurchaseOrder) -> bytes:
    art_ids = [l.article_id for l in order.lines]
    arts = {
        a.id: a for a in db.execute(
            select(m.Article).where(m.Article.id.in_(art_ids))
        ).scalars().all()
    }
    rows = []
    for line in order.lines:
        a = arts.get(line.article_id)
        if a is None or int(line.ordered_quantity) <= 0:
            continue
        rows.append((a.sku, _display_name(a), line.ordered_quantity))
    return _write_workbook(rows)


def export_order(db: Session, order_id: int) -> tuple[str, bytes]:
    order = db.get(m.PurchaseOrder, order_id)
    if order is None:
        raise ValueError(f"Няма заявка с id {order_id}")
    store = db.get(m.Store, order.store_id)
    return order_filename(store), build_order_workbook(db, order)


def export_orders_zip(
    db: Session,
    dispatch_run_id: int | None = None,
    supplier_id: int | None = None,
    status: str | None = None,
) -> bytes:
    stmt = select(m.PurchaseOrder)
    if dispatch_run_id is not None:
        stmt = stmt.where(m.PurchaseOrder.dispatch_run_id == dispatch_run_id)
    if supplier_id is not None:
        stmt = stmt.where(m.PurchaseOrder.supplier_id == supplier_id)
    if status:
        stmt = stmt.where(m.PurchaseOrder.status == status)
    orders = db.execute(stmt).scalars().all()

    buf = BytesIO()
    used: dict[str, int] = {}
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for order in orders:
            if not order.lines:
                continue
            store = db.get(m.Store, order.store_id)
            name = order_filename(store)
            if name in used:
                used[name] += 1
                stem = name[:-5]
                name = f"{stem} ({used[name]}).xlsx"
            else:
                used[name] = 1
            zf.writestr(name, build_order_workbook(db, order))
    return buf.getvalue()


def build_workbook_from_lines(lines, db: Session | None = None) -> bytes:
    """
    .xlsx директно от изчислени (не задължително записани) редове -
    за износ на преглед, без да се създава заявка в базата.
    """
    names = {}
    if db is not None and lines:
        skus = [ln.sku for ln in lines]
        for a in db.execute(
            select(m.Article).where(m.Article.sku.in_(skus))
        ).scalars().all():
            names[a.sku] = _display_name(a)
    rows = [
        (ln.sku, names.get(ln.sku, ln.name), ln.ordered_quantity)
        for ln in lines if int(ln.ordered_quantity) > 0
    ]
    return _write_workbook(rows)
