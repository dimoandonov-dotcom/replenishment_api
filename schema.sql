-- =====================================================================
-- Схема за система за автоматични заявки към доставчици
-- 60 магазина / 100 фирми / 5000 артикула
-- Наличност -> от Mistral (Firebird, read-only)
-- Min/Max, история, поръчки -> управлявани изцяло тук
-- =====================================================================

CREATE TABLE stores (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,        -- напр. 'АНДЖИ', 'БАЛКАН'
    address         TEXT,                        -- ползва се в името на файла със заявката
    mistral_host    TEXT,                        -- IP на Firebird сървъра в обекта
    mistral_db_path TEXT,                        -- път до .FDB файла
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE suppliers (
    id                  SERIAL PRIMARY KEY,
    name                TEXT NOT NULL UNIQUE,     -- напр. 'АЙВА ЕООД'
    contact_email       TEXT,
    order_format        TEXT NOT NULL DEFAULT 'email', -- email / xml / api / file
    replenishment_mode  TEXT NOT NULL DEFAULT 'below_min', -- below_min / daily_topup
    apply_weekend_buffer BOOLEAN NOT NULL DEFAULT FALSE, -- вдига ли max преди пропуск в графика
    order_cutoff_time   TIME,                     -- до колко часа приема заявка
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- График за доставка: КОМБИНАЦИЯ магазин+доставчик, защото един доставчик
-- може да има различни дни/часове на доставка по различните си маршрути.
CREATE TABLE store_supplier_schedule (
    id                  SERIAL PRIMARY KEY,
    store_id            INTEGER NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    supplier_id         INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
    weekday             SMALLINT NOT NULL CHECK (weekday BETWEEN 1 AND 7), -- ден, в който се подава заявка
    order_cutoff_time   TIME,       -- до колко часа за деня се приема заявка
    delivery_weekday    SMALLINT CHECK (delivery_weekday BETWEEN 1 AND 7), -- в кой ден пристига доставката
    confirmed           BOOLEAN NOT NULL DEFAULT FALSE, -- потвърден ли е графикът с доставчика
    UNIQUE (store_id, supplier_id, weekday)
);

-- Периоди, в които доставчик НЕ работи (Коледа, Великден и т.н.)
-- Използва се за планиране на предварителни заявки преди затварянето.
CREATE TABLE supplier_closures (
    id              SERIAL PRIMARY KEY,
    supplier_id     INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
    start_date      DATE NOT NULL,      -- първи ден без доставки
    end_date        DATE NOT NULL,      -- последен ден без доставки (вкл.)
    reason          TEXT,               -- напр. 'Коледа 2026'
    buffer_days     INTEGER NOT NULL DEFAULT 0, -- 0 = точния период на затваряне, без допълнителен буфер
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (end_date >= start_date)
);
CREATE INDEX idx_supplier_closures_dates ON supplier_closures (supplier_id, start_date, end_date);

CREATE TABLE articles (
    id              SERIAL PRIMARY KEY,
    sku             TEXT NOT NULL UNIQUE,        -- код на артикула (както в Mistral)
    name            TEXT NOT NULL,
    default_supplier_id INTEGER REFERENCES suppliers(id),
    pack_size       INTEGER NOT NULL DEFAULT 1,  -- бр. в опаковка/стек (цигари = 10)
    pack_type       TEXT,                        -- Стек / Каса / Кашон / брой
    category        TEXT,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Основната таблица: min/max по магазин+артикул, управлявани САМО от нас
CREATE TABLE store_article_settings (
    store_id        INTEGER NOT NULL REFERENCES stores(id),
    article_id      INTEGER NOT NULL REFERENCES articles(id),
    supplier_id     INTEGER REFERENCES suppliers(id),  -- може да override-не default_supplier_id
    min_stock       INTEGER NOT NULL DEFAULT 0,
    max_stock       INTEGER NOT NULL DEFAULT 0,
    auto_adjust     BOOLEAN NOT NULL DEFAULT TRUE,  -- дали да участва в автоматичната корекция
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (store_id, article_id)
);

-- Артикули, за които няма зададени min/max в изходните данни -
-- изключени от автоматични заявки, докато не се прегледат ръчно.
CREATE TABLE settings_review_queue (
    id              SERIAL PRIMARY KEY,
    store_id        INTEGER NOT NULL REFERENCES stores(id),
    article_id      INTEGER NOT NULL REFERENCES articles(id),
    reason          TEXT NOT NULL,
    resolved        BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at     TIMESTAMPTZ,
    UNIQUE (store_id, article_id)
);

CREATE TABLE stock_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    store_id        INTEGER NOT NULL REFERENCES stores(id),
    article_id      INTEGER NOT NULL REFERENCES articles(id),
    quantity        NUMERIC(12,2) NOT NULL,
    captured_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_stock_snapshots_lookup ON stock_snapshots (store_id, article_id, captured_at DESC);

-- Дневна история на продажби (за анализа на min/max)
CREATE TABLE sales_history (
    store_id        INTEGER NOT NULL REFERENCES stores(id),
    article_id      INTEGER NOT NULL REFERENCES articles(id),
    sale_date       DATE NOT NULL,
    quantity_sold   NUMERIC(12,2) NOT NULL DEFAULT 0,
    PRIMARY KEY (store_id, article_id, sale_date)
);

-- Генерирани поръчки (header)
CREATE TABLE purchase_orders (
    id              BIGSERIAL PRIMARY KEY,
    store_id        INTEGER NOT NULL REFERENCES stores(id),
    supplier_id     INTEGER NOT NULL REFERENCES suppliers(id),
    status          TEXT NOT NULL DEFAULT 'draft', -- draft / sent / confirmed / delivered / cancelled
    dispatch_run_id BIGINT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at         TIMESTAMPTZ
);

-- Редове на поръчка
CREATE TABLE purchase_order_lines (
    id                  BIGSERIAL PRIMARY KEY,
    purchase_order_id   BIGINT NOT NULL REFERENCES purchase_orders(id) ON DELETE CASCADE,
    article_id          INTEGER NOT NULL REFERENCES articles(id),
    current_stock       NUMERIC(12,2) NOT NULL,
    min_stock           NUMERIC(12,2) NOT NULL,
    max_stock           NUMERIC(12,2) NOT NULL,
    effective_max       NUMERIC(12,2) NOT NULL,
    suggested_quantity  NUMERIC(12,2) NOT NULL,
    ordered_quantity    
