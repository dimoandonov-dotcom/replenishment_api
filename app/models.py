"""SQLAlchemy модели, съответстващи на schema.sql."""
from __future__ import annotations

from datetime import datetime, date

from sqlalchemy import (
    BigInteger, Boolean, Date, DateTime, ForeignKey, Integer,
    Numeric, SmallInteger, String, Text, Time, UniqueConstraint, func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Store(Base):
    __tablename__ = "stores"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True)
    address: Mapped[str | None] = mapped_column(Text)
    mistral_host: Mapped[str | None] = mapped_column(Text)
    mistral_db_path: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Supplier(Base):
    __tablename__ = "suppliers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True)
    contact_email: Mapped[str | None] = mapped_column(Text)
    order_format: Mapped[str] = mapped_column(Text, default="email")
    replenishment_mode: Mapped[str] = mapped_column(Text, default="below_min")
    apply_weekend_buffer: Mapped[bool] = mapped_column(Boolean, default=False)
    order_cutoff_time: Mapped[str | None] = mapped_column(Time)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class StoreSupplierSchedule(Base):
    __tablename__ = "store_supplier_schedule"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"))
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id", ondelete="CASCADE"))
    weekday: Mapped[int] = mapped_column(SmallInteger)
    delivery_weekday: Mapped[int | None] = mapped_column(SmallInteger)
    order_cutoff_time: Mapped[str | None] = mapped_column(Time)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (UniqueConstraint("store_id", "supplier_id", "weekday"),)


class SupplierClosure(Base):
    __tablename__ = "supplier_closures"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id", ondelete="CASCADE"))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    reason: Mapped[str | None] = mapped_column(Text)
    buffer_days: Mapped[int] = mapped_column(Integer, default=0)


class Article(Base):
    __tablename__ = "articles"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sku: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text)
    default_supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id"))
    pack_size: Mapped[int] = mapped_column(Integer, default=1)
    pack_type: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class StoreArticleSetting(Base):
    __tablename__ = "store_article_settings"
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"), primary_key=True)
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id"))
    min_stock: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    max_stock: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    auto_adjust: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class SettingsReviewQueue(Base):
    __tablename__ = "settings_review_queue"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"))
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"))
    reason: Mapped[str] = mapped_column(Text)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint("store_id", "article_id"),)


class StockSnapshot(Base):
    __tablename__ = "stock_snapshots"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"))
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"))
    quantity: Mapped[float] = mapped_column(Numeric(12, 2))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SalesHistory(Base):
    __tablename__ = "sales_history"
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"), primary_key=True)
    sale_date: Mapped[date] = mapped_column(Date, primary_key=True)
    quantity_sold: Mapped[float] = mapped_column(Numeric(12, 2), default=0)


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"))
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"))
    status: Mapped[str] = mapped_column(Text, default="draft")
    dispatch_run_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lines: Mapped[list["PurchaseOrderLine"]] = relationship(back_populates="order", cascade="all, delete-orphan")


class PurchaseOrderLine(Base):
    __tablename__ = "purchase_order_lines"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    purchase_order_id: Mapped[int] = mapped_column(ForeignKey("purchase_orders.id", ondelete="CASCADE"))
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"))
    current_stock: Mapped[float] = mapped_column(Numeric(12, 2))
    min_stock: Mapped[float] = mapped_column(Numeric(12, 2))
    max_stock: Mapped[float] = mapped_column(Numeric(12, 2))
    effective_max: Mapped[float] = mapped_column(Numeric(12, 2))
    suggested_quantity: Mapped[float] = mapped_column(Numeric(12, 2))
    ordered_quantity: Mapped[int] = mapped_column(Integer)
    pack_size: Mapped[int] = mapped_column(Integer, default=1)
    notes: Mapped[str | None] = mapped_column(Text)
    order: Mapped["PurchaseOrder"] = relationship(back_populates="lines")


class DispatchRun(Base):
    __tablename__ = "dispatch_runs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text, default="running")
    stores_processed: Mapped[int | None] = mapped_column(Integer)
    orders_created: Mapped[int | None] = mapped_column(Integer)
    order_lines_created: Mapped[int | None] = mapped_column(Integer)
    emails_sent: Mapped[int | None] = mapped_column(Integer)
    notes: Mapped[str | None] = mapped_column(Text)


class ArticleAlert(Base):
    __tablename__ = "article_alerts"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"))
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"))
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id"))
    alert_type: Mapped[str] = mapped_column(Text)
    details: Mapped[str | None] = mapped_column(Text)
    max_adjusted: Mapped[bool] = mapped_column(Boolean, default=False)
    old_max: Mapped[float | None] = mapped_column(Numeric(12, 2))
    new_max: Mapped[float | None] = mapped_column(Numeric(12, 2))
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class StoreAlias(Base):
    __tablename__ = "store_aliases"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"))
    alias_normalized: Mapped[str] = mapped_column(Text, unique=True)
