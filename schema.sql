CREATE TABLE stores (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    address         TEXT,
    mistral_host    TEXT,
    mistral_db_path TEXT,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE suppliers (
    id                  SERIAL PRIMARY KEY,
    name                TEXT NOT NULL UNIQUE,
    contact_email       TEXT,
    order_format        TEXT NOT NULL DEFAULT 'email',
    replenishment_mode  TEXT NOT NULL DEFAULT 'below_min',
    apply_weekend_buffer BOOLEAN NOT NULL DEFAULT FALSE,
    order_cutoff_time   TIME,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE store_supplier_schedule (
    id                  SERIAL PRIMARY KEY,
    store_id            INTEGER NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    supplier_id         INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
    weekday             SMALLINT NOT NULL CHECK (weekday BETWEEN 1 AND 7),
    order_cutoff_time   TIME,
    delivery_weekday    SMALLINT CHECK (delivery_weekday BETWEEN 1 AND 7),
    confirmed           BOOLEAN NOT NULL DEFAULT FALSE,
    UNIQUE (store_id, supplier_id, weekday)
);
CREATE TABLE supplier_closures (
    id              SERIAL PRIMARY KEY,
    supplier_id     INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
    start_date      DATE NOT NULL,
    end_date        DATE NOT NULL,
    reason          TEXT,
    buffer_days     INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (end_date >= start_date)
);
CREATE INDEX idx_supplier_closures_dates ON supplier_closures (supplier_id, start_date, end_date);
CREATE TABLE articles (
    id              SERIAL PRIMARY KEY,
    sku             TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    default_supplier_id INTEGER REFERENCES suppliers(id),
    pack_size       INTEGER NOT NULL DEFAULT 1,
    pack_type       TEXT,
    category        TEXT,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE store_article_settings (
    store_id        INTEGER NOT NULL REFERENCES stores(id),
    article_id      INTEGER NOT NULL REFERENCES articles(id),
    supplier_id     INTEGER REFERENCES suppliers(id),
    min_stock       INTEGER NOT NULL DEFAULT 0,
    max_stock       INTEGER NOT NULL DEFAULT 0,
    auto_adjust     BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (store_id, article_id)
);
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
CREATE TABLE sales_history (
    store_id        INTEGER NOT NULL REFERENCES stores(id),
    article_id      INTEGER NOT NULL REFERENCES articles(id),
    sale_date       DATE NOT NULL,
    quantity_sold   NUMERIC(12,2) NOT NULL DEFAULT 0,
    PRIMARY KEY (store_id, article_id, sale_date)
);
CREATE TABLE purchase_orders (
    id              BIGSERIAL PRIMARY KEY,
    store_id        INTEGER NOT NULL REFERENCES stores(id),
    supplier_id     INTEGER NOT NULL REFERENCES suppliers(id),
    status          TEXT NOT NULL DEFAULT 'draft',
    dispatch_run_id BIGINT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at         TIMESTAMPTZ
);
CREATE TABLE purchase_order_lines (
    id                  BIGSERIAL PRIMARY KEY,
    purchase_order_id   BIGINT NOT NULL REFERENCES purchase_orders(id) ON DELETE CASCADE,
    article_id          INTEGER NOT NULL REFERENCES articles(id),
    current_stock       NUMERIC(12,2) NOT NULL,
    min_stock           NUMERIC(12,2) NOT NULL,
    max_stock           NUMERIC(12,2) NOT NULL,
    effective_max       NUMERIC(12,2) NOT NULL,
    suggested_quantity  NUMERIC(12,2) NOT NULL,
    ordered_quantity    INTEGER NOT NULL,
    pack_size           INTEGER NOT NULL DEFAULT 1,
    notes               TEXT
);
CREATE TABLE dispatch_runs (
    id                  BIGSERIAL PRIMARY KEY,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at        TIMESTAMPTZ,
    status              TEXT NOT NULL DEFAULT 'running',
    stores_processed    INTEGER,
    orders_created       INTEGER,
    order_lines_created INTEGER,
    emails_sent         INTEGER,
    notes               TEXT
);
CREATE TABLE article_alerts (
    id              BIGSERIAL PRIMARY KEY,
    store_id        INTEGER NOT NULL REFERENCES stores(id),
    article_id      INTEGER NOT NULL REFERENCES articles(id),
    supplier_id     INTEGER REFERENCES suppliers(id),
    alert_type      TEXT NOT NULL,
    details         TEXT,
    max_adjusted    BOOLEAN NOT NULL DEFAULT FALSE,
    old_max         NUMERIC(12,2),
    new_max         NUMERIC(12,2),
    resolved        BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_article_alerts_open ON article_alerts (store_id, article_id, alert_type) WHERE NOT resolved;
CREATE TABLE store_aliases (
    id                  SERIAL PRIMARY KEY,
    store_id            INTEGER NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    alias_normalized    TEXT NOT NULL UNIQUE
);
