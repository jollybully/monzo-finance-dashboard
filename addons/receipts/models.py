from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class ReceiptsBase(DeclarativeBase):
    pass


class SourceAuth(ReceiptsBase):
    """Per-source credentials (e.g. rotating Lidl refresh token)."""

    __tablename__ = "source_auth"

    source: Mapped[str] = mapped_column(String(32), primary_key=True)
    refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class Receipt(ReceiptsBase):
    __tablename__ = "receipts"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_receipts_source_external"),
        Index("ix_receipts_purchased_at", "purchased_at"),
        Index("ix_receipts_source_merchant", "source", "merchant_key"),
        Index("ix_receipts_transaction_id", "transaction_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    purchased_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    purchase_date: Mapped[date] = mapped_column(Date, nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="GBP")
    merchant_key: Mapped[str] = mapped_column(String(64), nullable=False)
    store_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    store_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    store_locality: Mapped[str | None] = mapped_column(String(128), nullable=True)
    store_postcode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    transaction_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    items: Mapped[list[ReceiptItem]] = relationship(
        "ReceiptItem",
        back_populates="receipt",
        cascade="all, delete-orphan",
        order_by="ReceiptItem.net_total.desc()",
    )


class ReceiptItem(ReceiptsBase):
    __tablename__ = "receipt_items"
    __table_args__ = (
        Index("ix_receipt_items_product_id", "product_id"),
        Index("ix_receipt_items_description", "description"),
        Index("ix_receipt_items_receipt_id", "receipt_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    receipt_id: Mapped[int] = mapped_column(
        ForeignKey("receipts.id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    description: Mapped[str] = mapped_column(String(512), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(
        Numeric(12, 3), nullable=False, default=Decimal("1")
    )
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    line_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    discount_total: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00")
    )
    net_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    is_weight: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    tax_type: Mapped[str | None] = mapped_column(String(32), nullable=True)

    receipt: Mapped[Receipt] = relationship("Receipt", back_populates="items")
