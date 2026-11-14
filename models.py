from datetime import datetime, timezone
from sqlalchemy import func, text

from extensions import db


class Account(db.Model):
    __tablename__ = "accounts"

    id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    kind = db.Column(db.String(20), nullable=False)
    currency = db.Column(db.String(3), nullable=False, default="TWD", server_default="TWD")
    opening_balance = db.Column(
        db.Numeric(16, 4),
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    statement_day = db.Column(db.Integer, nullable=True)
    payment_day = db.Column(db.Integer, nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        server_default=text("timezone('utc', now())"),
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        server_default=text("timezone('utc', now())"),
        onupdate=func.timezone("utc", func.now()),
    )

    __table_args__ = (
        db.CheckConstraint(
            "kind IN ('cash', 'bank', 'credit_card', 'broker')",
            name="ck_accounts_kind",
        ),
        db.CheckConstraint(
            "currency ~ '^[A-Z]{3}$'",
            name="ck_accounts_currency",
        ),
        db.CheckConstraint(
            "statement_day IS NULL OR (statement_day >= 1 AND statement_day <= 31)",
            name="ck_accounts_statement_day",
        ),
        db.CheckConstraint(
            "payment_day IS NULL OR (payment_day >= 1 AND payment_day <= 31)",
            name="ck_accounts_payment_day",
        ),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "currency": self.currency,
            "opening_balance": float(self.opening_balance) if self.opening_balance is not None else 0.0,
            "statement_day": self.statement_day,
            "payment_day": self.payment_day,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self) -> str:
        return f"<Account {self.id}: {self.name} ({self.kind})>"


class Transfer(db.Model):
    __tablename__ = "transfers"

    id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    transferred_on = db.Column(db.Date, nullable=False)
    amount = db.Column(db.Numeric(16, 4), nullable=False)
    currency = db.Column(db.String(3), nullable=False)
    fx_rate_to_twd = db.Column(db.Numeric(12, 6), nullable=False)
    from_account_id = db.Column(
        db.BigInteger,
        db.ForeignKey("accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    to_account_id = db.Column(
        db.BigInteger,
        db.ForeignKey("accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    note = db.Column(db.Text, nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        server_default=text("timezone('utc', now())"),
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        server_default=text("timezone('utc', now())"),
        onupdate=func.timezone("utc", func.now()),
    )

    from_account = db.relationship(
        "Account",
        foreign_keys=[from_account_id],
        backref=db.backref("outgoing_transfers", lazy="select"),
    )
    to_account = db.relationship(
        "Account",
        foreign_keys=[to_account_id],
        backref=db.backref("incoming_transfers", lazy="select"),
    )

    __table_args__ = (
        db.CheckConstraint(
            "from_account_id <> to_account_id",
            name="ck_transfers_different_accounts",
        ),
        db.CheckConstraint(
            "amount > 0",
            name="ck_transfers_amount_positive",
        ),
        db.CheckConstraint(
            "fx_rate_to_twd > 0",
            name="ck_transfers_fx_rate_positive",
        ),
        db.CheckConstraint(
            "currency ~ '^[A-Z]{3}$'",
            name="ck_transfers_currency",
        ),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "transferred_on": self.transferred_on.isoformat() if self.transferred_on else None,
            "amount": float(self.amount) if self.amount is not None else 0.0,
            "currency": self.currency,
            "fx_rate_to_twd": float(self.fx_rate_to_twd) if self.fx_rate_to_twd is not None else 1.0,
            "from_account_id": self.from_account_id,
            "to_account_id": self.to_account_id,
            "note": self.note,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self) -> str:
        return (
            f"<Transfer {self.id}: {self.amount} {self.currency} "
            f"from {self.from_account_id} to {self.to_account_id}>"
        )


__all__ = ["Account", "Transfer"]
