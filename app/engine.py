"""
Ядро на логиката за автоматични заявки.

Умишлено НЯМА зависимост от база данни или FastAPI - това са чисти функции,
които могат да се тестват изолирано и да се ползват от всяка част на системата.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta


@dataclass
class ArticleSetting:
    """Настройки за един артикул в един магазин."""
    store_id: int
    article_id: int
    sku: str
    name: str
    supplier_id: int
    min_stock: float
    max_stock: float
    pack_size: int = 1
    auto_adjust: bool = True

    def __post_init__(self):
        if self.pack_size < 1:
            self.pack_size = 1


@dataclass
class ClosurePeriod:
    """Период, в който доставчикът не работи (Коледа, Великден и т.н.)."""
    supplier_id: int
    start_date: date
    end_date: date
    reason: str = ""

    def covers(self, d: date) -> bool:
        return self.start_date <= d <= self.end_date


@dataclass
class ScheduleEntry:
    """Ден за заявка и ден за доставка за конкретен магазин+доставчик."""
    store_id: int
    supplier_id: int
    order_weekday: int
    delivery_weekday: int


@dataclass
class OrderLine:
    """Изчислен ред за заявка."""
    store_id: int
    article_id: int
    sku: str
    name: str
    supplier_id: int
    current_stock: float
    min_stock: float
    max_stock: float
    effective_max: float
    suggested_quantity: float
    ordered_quantity: int
    pack_size: int
    closure_adjustment: float = 0.0
    notes: str = ""


def round_up_to_pack(quantity: float, pack_size: int, force_min_pack: bool = True) -> int:
    """
    Математическо закръгляне до брой опаковки:
      до X.5 опаковки вкл. -> надолу (1.5 стека = 1 стек)
      над X.5              -> нагоре (1.6 стека = 2 стека)
    """
    if quantity <= 0:
        return 0
    if pack_size < 1:
        pack_size = 1
    packs_exact = quantity / pack_size
    whole = math.floor(packs_exact)
    frac = packs_exact - whole
    packs = whole if frac <= 0.5 else whole + 1
    if packs == 0 and force_min_pack:
        packs = 1
    return packs * pack_size


def needs_reorder(current_stock: float, min_stock: float,
                  mode: str = "below_min", max_stock: float | None = None) -> bool:
    """Кога изобщо разглеждаме артикула за поръчка."""
    if mode == "daily_topup":
        target = max_stock if max_stock is not None else min_stock
        return current_stock < target
    return current_stock < min_stock


def calculate_line(
    setting: ArticleSetting,
    current_stock: float,
    effective_max: float | None = None,
    closure_adjustment: float = 0.0,
    notes: str = "",
    mode: str = "below_min",
) -> OrderLine | None:
    """Изчислява един ред от заявка. Връща None, ако не се налага поръчка."""
    max_target = setting.max_stock if effective_max is None else effective_max

    if not needs_reorder(current_stock, setting.min_stock, mode, max_target):
        return None

    suggested = max(max_target - current_stock, 0.0)

    below_min = current_stock < setting.min_stock
    ordered = round_up_to_pack(suggested, setting.pack_size,
                               force_min_pack=below_min)

    if ordered <= 0:
        return None

    return OrderLine(
        store_id=setting.store_id,
        article_id=setting.article_id,
        sku=setting.sku,
        name=setting.name,
        supplier_id=setting.supplier_id,
        current_stock=current_stock,
        min_stock=setting.min_stock,
        max_stock=setting.max_stock,
        effective_max=max_target,
        suggested_quantity=suggested,
        ordered_quantity=ordered,
        pack_size=setting.pack_size,
        closure_adjustment=closure_adjustment,
        notes=notes,
    )


def weekday_gap_to_next_order(order_date: date, order_weekdays: set[int]) -> int:
    """Колко дни оставаме БЕЗ нова заявка след order_date."""
    if not order_weekdays:
        return 0
    gap = 0
    d = order_date
    for _ in range(7):
        d = d + timedelta(days=1)
        if d.isoweekday() in order_weekdays:
            break
        gap += 1
    return gap


def next_delivery_date(
    from_date: date,
    delivery_weekday: int,
    closures: list[ClosurePeriod],
    supplier_id: int,
    max_lookahead_days: int = 30,
) -> date | None:
    """Намира първата дата СЛЕД from_date, на която доставчикът реално доставя."""
    for offset in range(1, max_lookahead_days + 1):
        candidate = from_date + timedelta(days=offset)
        if candidate.isoweekday() != delivery_weekday:
            continue
        blocked = any(
            c.supplier_id == supplier_id and c.covers(candidate) for c in closures
        )
        if not blocked:
            return candidate
    return None


def closure_gap_days(
    order_date: date,
    schedule: ScheduleEntry,
    closures: list[ClosurePeriod],
) -> int:
    """Колко дни магазинът трябва да "изкара" без нова доставка."""
    this_delivery = next_delivery_date(
        order_date, schedule.delivery_weekday, [], schedule.supplier_id
    )
    if this_delivery is None:
        return 0

    normal_next = next_delivery_date(
        this_delivery, schedule.delivery_weekday, [], schedule.supplier_id
    )
    actual_next = next_delivery_date(
        this_delivery, schedule.delivery_weekday, closures, schedule.supplier_id
    )
    if normal_next is None or actual_next is None:
        return 0

    gap = (actual_next - normal_next).days
    return max(gap, 0)


def apply_closure_buffer(
    setting: ArticleSetting,
    avg_daily_sales: float,
    extra_days: int,
) -> tuple[float, float]:
    """Връща (effective_max, добавка). Само за конкретната заявка."""
    if extra_days <= 0 or avg_daily_sales <= 0:
        return setting.max_stock, 0.0
    extra = avg_daily_sales * extra_days
    return setting.max_stock + extra, extra


@dataclass
class ReplenishmentResult:
    order_date: date
    lines: list[OrderLine] = field(default_factory=list)
    skipped_no_stock_data: list[str] = field(default_factory=list)
    skipped_above_min: int = 0

    @property
    def total_lines(self) -> int:
        return len(self.lines)

    @property
    def total_units(self) -> int:
        return sum(l.ordered_quantity for l in self.lines)


def generate_order_lines(
    settings: list[ArticleSetting],
    stock_by_article: dict[int, float],
    order_date: date,
    schedules: dict[tuple[int, int], list[ScheduleEntry]] | None = None,
    closures: list[ClosurePeriod] | None = None,
    avg_daily_sales: dict[tuple[int, int], float] | None = None,
    mode: str = "below_min",
    weekend_buffer: bool = True,
) -> ReplenishmentResult:
    """Основната функция: за списък настройки + тек
