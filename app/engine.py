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
