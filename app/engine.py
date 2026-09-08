"""
Ядро на логиката за автоматични заявки.

Умишлено НЯМА зависимост от база данни или FastAPI - това са чисти функции,
които могат да се тестват изолирано и да се ползват от всяка част на системата.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta

# ----------------------------------------
# Входни структури
# ----------------------------------------


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


# ----------------------------------------
# Базови изчисления
# ----------------------------------------


def round_up_to_pack(
    quantity: float, pack_size: int, force_min_pack: bool = True
) -> int:
    """
    Математическо закръгляне до брой опаковки:
      до X.5 опаковки вкл. -> надолу (1.5 стека = 1 стек)
      над X.5              -> нагоре (1.6 стека = 2 стека)

    force_min_pack=True: ако резултатът е 0 опаковки, но количество е нужно,
    връща 1 опаковка. Ползва се, когато сме ПОД минимума - иначе артикулът
    никога няма да се допълни.

    force_min_pack=False: връща 0 и нуждата се натрупва за следващия ден.
    Ползва се при ежедневно допълване, за да не пращаме цял стек заради
    една продадена бройка.
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


def needs_reorder(
    current_stock: float,
    min_stock: float,
    mode: str = "below_min",
    max_stock: float | None = None,
) -> bool:
    """
    Кога изобщо разглеждаме артикула за поръчка.

    mode="below_min" - само когато наличността е паднала ПОД минимума.
        Подходящо за доставчици, които идват веднъж-два пъти седмично.

    mode="daily_topup" - всеки път, когато сме под максимума, т.е. след
        всяка продажба. Подходящо за доставчици с ежедневни доставки (НДК).
        Минимумът остава като предпазен праг: под него се поръчва поне
        една опаковка, дори нуждата да е малка.
    """
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
    """
    Изчислява един ред от заявка. Връща None, ако не се налага поръчка.

    Количеството е ВИНАГИ допълване до максимума:
        нужда  = максимум - наличност
        заявка = закръглено по опаковката
    """
    max_target = (
        setting.max_stock if effective_max is None else effective_max
    )

    if not needs_reorder(
        current_stock, setting.min_stock, mode, max_target
    ):
        return None

    suggested = max(max_target - current_stock, 0.0)

    # Под минимума сме -> гарантираме поне една опаковка.
    # При ежедневно допълване над минимума малките нужди се натрупват.
    below_min = current_stock < setting.min_stock
    ordered = round_up_to_pack(
        suggested, setting.pack_size, force_min_pack=below_min
    )

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


# ----------------------------------------
# Логика за периоди на затваряне (Коледа, Великден)
# ----------------------------------------


def weekday_gap_to_next_order(
    order_date: date, order_weekdays: set[int]
) -> int:
    """
    Колко дни оставаме БЕЗ нова заявка след order_date, преди следващия
    ден по график. За Пон-Пет график, петък връща 2 (събота + неделя) -
    понеделник вече се покрива от нормалния еднодневен буфер в max.

    Връща 0, ако утре пак е ден за заявка (нормален делничен ритъм).
    """
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
    """
    Намира първата дата СЛЕД from_date, на която доставчикът реално доставя -
    т.е. съвпада с деня за доставка по график И не пада в период на затваряне.
    """
    for offset in range(1, max_lookahead_days + 1):
        candidate = from_date + timedelta(days=offset)
        if candidate.isoweekday() != delivery_weekday:
            continue
        blocked = any(
            c.supplier_id == supplier_id and c.covers(candidate)
            for c in closures
        )
        if not blocked:
            return candidate
    return None


    
def closure_gap_days(
        order_date: date,
    schedule: ScheduleEntry,
    closures: list[ClosurePeriod],
) -> int:
    """
    Колко дни магазинът трябва да "изкара" без нова доставка.

    Нормално това е интервалът до следващата доставка. Ако междувременно
    доставчикът е затворен, интервалът се удължава автоматично, защото
    next_delivery_date прескача блокираните дни.

    Връща 0, ако няма удължаване спрямо нормалния ритъм.
    """
    this_delivery = next_delivery_date(
        order_date, schedule.delivery_weekday, [], schedule.supplier_id
    )
    if this_delivery is None:
        return 0

    # следваща доставка при нормален график (без затваряния)
    normal_next = next_delivery_date(
        this_delivery, schedule.delivery_weekday, [], schedule.supplier_id
    )
    # следваща доставка с отчитане на затварянията
    actual_next = next_delivery_date(
        this_delivery,
        schedule.delivery_weekday,
        closures,
        schedule.supplier_id,
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
    """
    Връща (effective_max, добавка). Само за конкретната заявка -
    max_stock в базата НЕ се променя.
    """
    if extra_days <= 0 or avg_daily_sales <= 0:
        return setting.max_stock, 0.0
    extra = avg_daily_sales * extra_days
    return setting.max_stock + extra, extra


# ----------------------------------------
# Генериране на цяла заявка
# ----------------------------------------


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
    """
    Основната функция: за списък настройки + текущи наличности връща
    редовете, които трябва да се поръчат.
    mode="below_min"    - заявка само при падане под минимума
    mode="daily_topup"  - ежедневно допълване до максимума (НДК)

    schedules - dict[(store,supplier)] -> списък ВСИЧКИ дни от графика
        (не само днешния), за да може да се засече колко дни оставаме
        без заявка след order_date (напр. петък -> събота+неделя).

    weekend_buffer - при mode="daily_topup" автоматично вдига ефективния
        max за дните, следвани от "дупка" в графика (уикенди), с
        avg_daily_sales * брой дни без заявка. Работи и БЕЗ запис в
        supplier_closures - това е приблизителен ("Вариант А") буфер,
        докато не дойдат точните часове на доставка по маршрут.
    """
    schedules = schedules or {}
    closures = closures or []
    avg_daily_sales = avg_daily_sales or {}

    result = ReplenishmentResult(order_date=order_date)

    for s in settings:
        if s.article_id not in stock_by_article:
            result.skipped_no_stock_data.append(s.sku)
            continue

        current = stock_by_article[s.article_id]

        effective_max = s.max_stock
        adjustment = 0.0
        notes = ""

        entries = schedules.get((s.store_id, s.supplier_id)) or []
        sched_today = next(
            (
                e
                for e in entries
                if e.order_weekday == order_date.isoweekday()
            ),
            None,
        )

        # 1) явен период на затваряне (Коледа/Великден) - приоритетен
        # буфер
        if sched_today and closures:
            extra_days = closure_gap_days(
                order_date, sched_today, closures
            )
            if extra_days > 0:
                adr = avg_daily_sales.get((s.store_id, s.article_id), 0.0)
                effective_max, adjustment = apply_closure_buffer(
                    s, adr, extra_days
                )
                if adjustment > 0:
                    notes = (
                        f"Предпразнична добавка за "
                        f"{extra_days} дни без доставка"
                    )

        # 2) редовен седмичен пропуск (уикенд) - само daily_topup,
        # само ако
        #    няма вече по-голяма добавка от т.1
        if (
            mode == "daily_topup"
            and weekend_buffer
            and adjustment == 0
            and entries
        ):
            order_weekdays = {e.order_weekday for e in entries}
            gap = weekday_gap_to_next_order(order_date, order_weekdays)
            if gap > 0:
                adr = avg_daily_sales.get((s.store_id, s.article_id), 0.0)
                effective_max, adjustment = apply_closure_buffer(
                    s, adr, gap
                )
                if adjustment > 0:
                    notes = (
                        f"Уикенд добавка за {gap} "
                        f"дни без заявка"
                    )

        if not needs_reorder(current, s.min_stock, mode, effective_max):
            result.skipped_above_min += 1
            continue

        line = calculate_line(
            s,
            current,
            effective_max=effective_max,
            closure_adjustment=adjustment,
            notes=notes,
            mode=mode,
        )
        if line:
            result.lines.append(line)
        else:
            result.skipped_above_min += 1

    return result


def group_lines_by_supplier(
    lines: list[OrderLine],
) -> dict[int, list[OrderLine]]:
    """Групира редовете по доставчик - всяка фирма получава своя заявка."""
    grouped: dict[int, list[OrderLine]] = {}
    for line in lines:
        grouped.setdefault(line.supplier_id, []).append(line)
    return grouped


def is_order_day(order_date: date, schedule: ScheduleEntry) -> bool:
    """Проверява дали днес е ден за подаване на заявка към този доставчик."""
    return order_date.isoweekday() == schedule.order_weekday


