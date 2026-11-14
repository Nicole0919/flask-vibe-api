import calendar
import re
from datetime import date, datetime, timezone
from decimal import Decimal

from flask import Blueprint, current_app, jsonify, request
from sqlalchemy.exc import IntegrityError, OperationalError

from extensions import db
from models import (
    Account,
    DrawPeriod,
    DrawPrize,
    Expense,
    ExpenseCategory,
    Income,
    IncomeCategory,
    Instrument,
    Invoice,
    InvoiceItem,
    Trade,
    Transfer,
)

api_bp = Blueprint("api", __name__, url_prefix="/api")

VALID_ACCOUNT_KINDS = {"cash", "bank", "credit_card", "broker"}
CURRENCY_REGEX = re.compile(r"^[A-Z]{3}$")
INVOICE_NUMBER_REGEX = re.compile(r"^[A-Z]{2}[0-9]{8}$")
BAN_REGEX = re.compile(r"^[0-9]{8}$")
RANDOM_CODE_REGEX = re.compile(r"^[0-9]{4}$")
PERIOD_KEY_REGEX = re.compile(r"^[0-9]{4}-(01|03|05|07|09|11)$")
MONTH_REGEX = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")
EIGHT_DIGIT_REGEX = re.compile(r"^[0-9]{8}$")
VALID_DRAW_TIERS = {
    "special",
    "grand",
    "first",
    "second",
    "third",
    "fourth",
    "fifth",
    "sixth",
    "extra_sixth",
}
VALID_MARKETS = {"tw_stock", "us_stock", "fund", "other"}
VALID_TRADE_TYPES = {
    "buy",
    "sell",
    "cash_dividend",
    "stock_dividend",
    "drip",
    "split",
    "merge",
    "capital_reduction",
}


def compute_instrument_holding(instrument_id: int) -> tuple[Decimal, Decimal]:
    trades = (
        Trade.query.filter(
            Trade.instrument_id == instrument_id,
            Trade.voided_at.is_(None),
        )
        .order_by(Trade.traded_on.asc(), Trade.id.asc())
        .all()
    )

    shares = Decimal("0")
    total_cost = Decimal("0")

    for t in trades:
        qty = Decimal(str(t.quantity or 0))
        price = Decimal(str(t.price or 0))
        ratio = Decimal(str(t.ratio or 1))

        if t.trade_type in ("buy", "drip"):
            shares += qty
            total_cost += qty * price
        elif t.trade_type == "stock_dividend":
            shares += qty
        elif t.trade_type in ("split", "merge"):
            shares = shares * ratio
        elif t.trade_type in ("sell", "capital_reduction"):
            if qty > Decimal("0"):
                if shares > Decimal("0"):
                    avg_cost = total_cost / shares
                    shares -= qty
                    total_cost -= qty * avg_cost
                    if shares <= Decimal("0"):
                        shares = Decimal("0")
                        total_cost = Decimal("0")
                else:
                    shares = Decimal("0")
                    total_cost = Decimal("0")

    avg_cost = (total_cost / shares) if shares > Decimal("0") else Decimal("0")
    return shares, avg_cost


def get_period_key(d: date) -> str:
    month = d.month
    start_month = month if month % 2 != 0 else month - 1
    return f"{d.year}-{start_month:02d}"


def get_period_date_range(period_key: str) -> tuple[date, date]:
    parts = period_key.strip().split("-")
    if len(parts) != 2:
        raise ValueError("Invalid period format")
    year, month = int(parts[0]), int(parts[1])
    last_day = calendar.monthrange(year, month + 1)[1]
    return date(year, month, 1), date(year, month + 1, last_day)


def check_invoice_prize_match(inv_digits: str, prize: DrawPrize) -> bool:
    target = prize.matching_number.strip()
    tier = prize.tier

    if tier in ("special", "grand", "first"):
        return inv_digits == target

    tier_suffix_len = {
        "second": 7,
        "third": 6,
        "fourth": 5,
        "fifth": 4,
        "sixth": 3,
        "extra_sixth": 3,
    }
    req_len = tier_suffix_len.get(tier)
    if req_len:
        if len(target) >= req_len:
            return inv_digits.endswith(target[-req_len:])
        return inv_digits.endswith(target)

    return inv_digits == target


def make_response(code: int, message: str, data=None):
    return jsonify({"code": code, "message": message, "data": data}), code


@api_bp.errorhandler(OperationalError)
def handle_db_operational_error(e):
    current_app.logger.error("資料庫連線失敗: %s", e)
    return make_response(500, "資料庫暫時連不上", None)


@api_bp.errorhandler(IntegrityError)
def handle_db_integrity_error(e):
    db.session.rollback()
    current_app.logger.warning("資料庫約束衝突: %s", e)
    return make_response(400, "資料不合法或違反約束條件", None)


# ---------------------------------------------------------------------
# Accounts API
# ---------------------------------------------------------------------


@api_bp.get("/accounts")
def list_accounts():
    accounts = Account.query.order_by(Account.id.asc()).all()
    return make_response(200, "查詢成功", [acc.to_dict() for acc in accounts])


@api_bp.post("/accounts")
def create_account():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return make_response(400, "請求格式必須為 JSON 物件", None)

    # 1. 必填 name
    raw_name = payload.get("name")
    if raw_name is None or not isinstance(raw_name, str) or not raw_name.strip():
        return make_response(400, "帳戶名稱為必填欄位", None)
    name = raw_name.strip()

    # 檢查名稱重複
    existing = Account.query.filter_by(name=name).first()
    if existing:
        return make_response(400, "帳戶名稱已存在，請使用其他名稱", None)

    # 2. 必填 kind
    raw_kind = payload.get("kind")
    if raw_kind is None or not isinstance(raw_kind, str):
        return make_response(400, "帳戶種類為必填欄位", None)
    kind = raw_kind.strip()
    if kind not in VALID_ACCOUNT_KINDS:
        return make_response(
            400, "帳戶種類不合法，只允許 cash、bank、credit_card 或 broker", None
        )

    # 3. 選填 currency（預設 TWD）
    raw_currency = payload.get("currency")
    if raw_currency is None:
        currency = "TWD"
    else:
        if not isinstance(raw_currency, str) or not CURRENCY_REGEX.match(raw_currency):
            return make_response(400, "幣別格式不正確，必須為 3 碼大寫英文字母", None)
        currency = raw_currency

    # 4. 選填 opening_balance（預設 0）
    raw_opening = payload.get("opening_balance")
    if raw_opening is None:
        opening_balance = Decimal("0")
    else:
        try:
            opening_balance = Decimal(str(raw_opening))
        except Exception:
            return make_response(400, "期初餘額數值格式不正確", None)

    # 5. 選填 statement_day (1 ~ 31)
    statement_day = payload.get("statement_day")
    if statement_day is not None:
        if (
            not isinstance(statement_day, int)
            or isinstance(statement_day, bool)
            or statement_day < 1
            or statement_day > 31
        ):
            return make_response(400, "結帳日必須介於 1 到 31 之間", None)

    # 6. 選填 payment_day (1 ~ 31)
    payment_day = payload.get("payment_day")
    if payment_day is not None:
        if (
            not isinstance(payment_day, int)
            or isinstance(payment_day, bool)
            or payment_day < 1
            or payment_day > 31
        ):
            return make_response(400, "繳款日必須介於 1 到 31 之間", None)

    account = Account(
        name=name,
        kind=kind,
        currency=currency,
        opening_balance=opening_balance,
        statement_day=statement_day,
        payment_day=payment_day,
    )
    db.session.add(account)
    db.session.commit()

    return make_response(201, "帳戶建立成功", account.to_dict())


@api_bp.get("/accounts/<int:account_id>")
def get_account(account_id: int):
    account = db.session.get(Account, account_id)
    if not account:
        return make_response(404, "找不到此帳戶", None)
    return make_response(200, "查詢成功", account.to_dict())


@api_bp.patch("/accounts/<int:account_id>")
def update_account(account_id: int):
    account = db.session.get(Account, account_id)
    if not account:
        return make_response(404, "找不到此帳戶", None)

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return make_response(400, "請求格式必須為 JSON 物件", None)

    # 可更新欄位：name, statement_day, payment_day
    if "name" in payload:
        raw_name = payload.get("name")
        if raw_name is None or not isinstance(raw_name, str) or not raw_name.strip():
            return make_response(400, "帳戶名稱不能為空", None)
        new_name = raw_name.strip()
        if new_name != account.name:
            existing = Account.query.filter_by(name=new_name).first()
            if existing:
                return make_response(400, "帳戶名稱已存在，請使用其他名稱", None)
            account.name = new_name

    if "statement_day" in payload:
        statement_day = payload.get("statement_day")
        if statement_day is not None:
            if (
                not isinstance(statement_day, int)
                or isinstance(statement_day, bool)
                or statement_day < 1
                or statement_day > 31
            ):
                return make_response(400, "結帳日必須介於 1 到 31 之間", None)
        account.statement_day = statement_day

    if "payment_day" in payload:
        payment_day = payload.get("payment_day")
        if payment_day is not None:
            if (
                not isinstance(payment_day, int)
                or isinstance(payment_day, bool)
                or payment_day < 1
                or payment_day > 31
            ):
                return make_response(400, "繳款日必須介於 1 到 31 之間", None)
        account.payment_day = payment_day

    account.updated_at = datetime.now(timezone.utc)
    db.session.commit()

    return make_response(200, "帳戶更新成功", account.to_dict())


@api_bp.get("/accounts/<int:account_id>/balance")
def get_account_balance(account_id: int):
    account = db.session.get(Account, account_id)
    if not account:
        return make_response(404, "找不到此帳戶", None)

    in_transfers = Transfer.query.filter_by(to_account_id=account_id).all()
    out_transfers = Transfer.query.filter_by(from_account_id=account_id).all()
    account_incomes = Income.query.filter_by(account_id=account_id).all()
    account_expenses = Expense.query.filter_by(account_id=account_id).all()
    account_trades = Trade.query.filter(
        Trade.account_id == account_id,
        Trade.voided_at.is_(None),
    ).all()

    opening = Decimal(str(account.opening_balance or 0))
    in_sum = sum((Decimal(str(t.amount)) for t in in_transfers), Decimal("0"))
    out_sum = sum((Decimal(str(t.amount)) for t in out_transfers), Decimal("0"))
    income_sum = sum((Decimal(str(inc.amount)) for inc in account_incomes), Decimal("0"))
    expense_sum = sum((Decimal(str(exp.amount)) for exp in account_expenses), Decimal("0"))

    trade_buy_sum = Decimal("0")
    trade_buy_sum_twd = Decimal("0")
    trade_sell_sum = Decimal("0")
    trade_sell_sum_twd = Decimal("0")
    trade_cash_sum = Decimal("0")
    trade_cash_sum_twd = Decimal("0")

    for tr in account_trades:
        fx = Decimal(str(tr.fx_rate_to_twd or 1))
        fee = Decimal(str(tr.fee or 0))
        tax = Decimal(str(tr.tax or 0))
        qty = Decimal(str(tr.quantity or 0))
        price = Decimal(str(tr.price or 0))

        if tr.trade_type == "buy":
            cost = (qty * price) + fee + tax
            trade_buy_sum += cost
            trade_buy_sum_twd += cost * fx
        elif tr.trade_type == "sell":
            proceeds = (qty * price) - fee - tax
            trade_sell_sum += proceeds
            trade_sell_sum_twd += proceeds * fx
        elif tr.trade_type in ("cash_dividend", "capital_reduction"):
            if tr.income is None:
                cash = Decimal(str(tr.cash_amount or 0))
                trade_cash_sum += cash
                trade_cash_sum_twd += cash * fx

    balance = (
        opening
        + in_sum
        - out_sum
        + income_sum
        - expense_sum
        - trade_buy_sum
        + trade_sell_sum
        + trade_cash_sum
    )

    in_sum_twd = sum(
        (Decimal(str(t.amount)) * Decimal(str(t.fx_rate_to_twd)) for t in in_transfers),
        Decimal("0"),
    )
    out_sum_twd = sum(
        (Decimal(str(t.amount)) * Decimal(str(t.fx_rate_to_twd)) for t in out_transfers),
        Decimal("0"),
    )
    income_sum_twd = sum(
        (
            Decimal(str(inc.amount)) * Decimal(str(inc.fx_rate_to_twd))
            for inc in account_incomes
        ),
        Decimal("0"),
    )
    expense_sum_twd = sum(
        (
            Decimal(str(exp.amount)) * Decimal(str(exp.fx_rate_to_twd))
            for exp in account_expenses
        ),
        Decimal("0"),
    )
    opening_twd = opening
    balance_twd = (
        opening_twd
        + in_sum_twd
        - out_sum_twd
        + income_sum_twd
        - expense_sum_twd
        - trade_buy_sum_twd
        + trade_sell_sum_twd
        + trade_cash_sum_twd
    )

    data = {
        "account_id": account.id,
        "currency": account.currency,
        "balance": float(balance),
        "balance_twd": float(balance_twd),
    }
    return make_response(200, "查詢餘額成功", data)


# ---------------------------------------------------------------------
# Transfers API
# ---------------------------------------------------------------------


@api_bp.get("/transfers")
def list_transfers():
    transfers = Transfer.query.order_by(
        Transfer.transferred_on.desc(), Transfer.id.desc()
    ).all()
    return make_response(200, "查詢成功", [t.to_dict() for t in transfers])


@api_bp.post("/transfers")
def create_transfer():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return make_response(400, "請求格式必須為 JSON 物件", None)

    # 1. 移轉日期 (transferred_on 或 date)
    raw_date = payload.get("transferred_on") or payload.get("date")
    if not raw_date:
        return make_response(400, "移轉日期為必填欄位", None)
    try:
        transferred_on = date.fromisoformat(str(raw_date))
    except Exception:
        return make_response(400, "移轉日期格式必須為 YYYY-MM-DD", None)

    # 2. 金額 (amount > 0)
    raw_amount = payload.get("amount")
    if raw_amount is None:
        return make_response(400, "移轉金額為必填欄位", None)
    try:
        amount = Decimal(str(raw_amount))
    except Exception:
        return make_response(400, "移轉金額數值格式不正確", None)
    if amount <= Decimal("0"):
        return make_response(400, "移轉金額必須大於 0", None)

    # 3. 幣別 (currency: 3碼大寫)
    raw_currency = payload.get("currency")
    if not raw_currency or not isinstance(raw_currency, str):
        return make_response(400, "幣別為必填欄位", None)
    currency = raw_currency.strip()
    if not CURRENCY_REGEX.match(currency):
        return make_response(400, "幣別格式不正確，必須為 3 碼大寫英文字母", None)

    # 4. 匯率 (fx_rate_to_twd 或 exchange_rate_to_twd > 0)
    raw_fx = payload.get("fx_rate_to_twd")
    if raw_fx is None:
        raw_fx = payload.get("exchange_rate_to_twd")
    if raw_fx is None:
        return make_response(400, "匯率為必填欄位", None)
    try:
        fx_rate_to_twd = Decimal(str(raw_fx))
    except Exception:
        return make_response(400, "匯率數值格式不正確", None)
    if fx_rate_to_twd <= Decimal("0"):
        return make_response(400, "匯率必須大於 0", None)

    # 5. 轉出帳戶 (from_account_id)
    raw_from = payload.get("from_account_id")
    if raw_from is None or not isinstance(raw_from, int) or isinstance(raw_from, bool):
        return make_response(400, "轉出帳戶為必填欄位", None)
    from_account_id = raw_from

    # 6. 轉入帳戶 (to_account_id)
    raw_to = payload.get("to_account_id")
    if raw_to is None or not isinstance(raw_to, int) or isinstance(raw_to, bool):
        return make_response(400, "轉入帳戶為必填欄位", None)
    to_account_id = raw_to

    # 檢查兩帳戶不可相同
    if from_account_id == to_account_id:
        return make_response(400, "轉出帳戶與轉入帳戶不可相同", None)

    # 檢查兩帳戶是否存在於資料庫
    from_account = db.session.get(Account, from_account_id)
    to_account = db.session.get(Account, to_account_id)
    if not from_account or not to_account:
        return make_response(400, "指定的轉出或轉入帳戶不存在", None)

    # 7. 備註 (note, 可空)
    note = payload.get("note")
    if note is not None:
        note = str(note)

    transfer = Transfer(
        transferred_on=transferred_on,
        amount=amount,
        currency=currency,
        fx_rate_to_twd=fx_rate_to_twd,
        from_account_id=from_account_id,
        to_account_id=to_account_id,
        note=note,
    )
    db.session.add(transfer)
    db.session.commit()

    return make_response(201, "移轉記錄建立成功", transfer.to_dict())


@api_bp.get("/transfers/<int:transfer_id>")
def get_transfer(transfer_id: int):
    transfer = db.session.get(Transfer, transfer_id)
    if not transfer:
        return make_response(404, "找不到此移轉記錄", None)
    return make_response(200, "查詢成功", transfer.to_dict())


# ---------------------------------------------------------------------
# Income Categories API
# ---------------------------------------------------------------------


@api_bp.get("/income-categories")
def list_income_categories():
    categories = IncomeCategory.query.order_by(IncomeCategory.id.asc()).all()
    return make_response(200, "查詢成功", [c.to_dict() for c in categories])


@api_bp.post("/income-categories")
def create_income_category():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return make_response(400, "請求格式必須為 JSON 物件", None)

    raw_name = payload.get("name")
    if raw_name is None or not isinstance(raw_name, str) or not raw_name.strip():
        return make_response(400, "分類名稱為必填欄位", None)
    name = raw_name.strip()

    existing = IncomeCategory.query.filter_by(name=name).first()
    if existing:
        return make_response(400, "入帳分類名稱已存在，請使用其他名稱", None)

    raw_desc = payload.get("description")
    description = str(raw_desc).strip() if (raw_desc is not None and str(raw_desc).strip()) else None

    category = IncomeCategory(name=name, description=description)
    db.session.add(category)
    db.session.commit()

    return make_response(201, "入帳分類建立成功", category.to_dict())


@api_bp.get("/income-categories/<int:category_id>")
def get_income_category(category_id: int):
    category = db.session.get(IncomeCategory, category_id)
    if not category:
        return make_response(404, "找不到此入帳分類", None)
    return make_response(200, "查詢成功", category.to_dict())


@api_bp.patch("/income-categories/<int:category_id>")
def update_income_category(category_id: int):
    category = db.session.get(IncomeCategory, category_id)
    if not category:
        return make_response(404, "找不到此入帳分類", None)

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return make_response(400, "請求格式必須為 JSON 物件", None)

    if "name" in payload:
        raw_name = payload.get("name")
        if raw_name is None or not isinstance(raw_name, str) or not raw_name.strip():
            return make_response(400, "分類名稱不能為空", None)
        new_name = raw_name.strip()
        if new_name != category.name:
            existing = IncomeCategory.query.filter_by(name=new_name).first()
            if existing:
                return make_response(400, "入帳分類名稱已存在，請使用其他名稱", None)
            category.name = new_name

    if "description" in payload:
        raw_desc = payload.get("description")
        category.description = str(raw_desc).strip() if (raw_desc is not None and str(raw_desc).strip()) else None

    category.updated_at = datetime.now(timezone.utc)
    db.session.commit()

    return make_response(200, "入帳分類更新成功", category.to_dict())


# ---------------------------------------------------------------------
# Incomes API
# ---------------------------------------------------------------------


@api_bp.get("/incomes")
def list_incomes():
    query = Income.query

    account_id_param = request.args.get("account_id")
    if account_id_param is not None and account_id_param.strip() != "":
        try:
            query = query.filter(Income.account_id == int(account_id_param))
        except (ValueError, TypeError):
            return make_response(400, "帳戶 ID 必須為整數", None)

    category_id_param = request.args.get("category_id")
    if category_id_param is not None and category_id_param.strip() != "":
        try:
            query = query.filter(Income.category_id == int(category_id_param))
        except (ValueError, TypeError):
            return make_response(400, "分類 ID 必須為整數", None)

    date_from_param = request.args.get("date_from")
    if date_from_param is not None and date_from_param.strip() != "":
        try:
            d_from = date.fromisoformat(date_from_param.strip())
            query = query.filter(Income.received_on >= d_from)
        except (ValueError, TypeError):
            return make_response(400, "起始日期格式必須為 YYYY-MM-DD", None)

    date_to_param = request.args.get("date_to")
    if date_to_param is not None and date_to_param.strip() != "":
        try:
            d_to = date.fromisoformat(date_to_param.strip())
            query = query.filter(Income.received_on <= d_to)
        except (ValueError, TypeError):
            return make_response(400, "結束日期格式必須為 YYYY-MM-DD", None)

    incomes = query.order_by(Income.received_on.desc(), Income.id.desc()).all()
    return make_response(200, "查詢成功", [inc.to_dict() for inc in incomes])


@api_bp.post("/incomes")
def create_income():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return make_response(400, "請求格式必須為 JSON 物件", None)

    # 1. 入帳日期
    raw_date = payload.get("received_on") or payload.get("date")
    if not raw_date:
        return make_response(400, "入帳日期為必填欄位", None)
    try:
        received_on = date.fromisoformat(str(raw_date))
    except Exception:
        return make_response(400, "入帳日期格式必須為 YYYY-MM-DD", None)

    # 2. 金額 (amount > 0)
    raw_amount = payload.get("amount")
    if raw_amount is None:
        return make_response(400, "入帳金額為必填欄位", None)
    try:
        amount = Decimal(str(raw_amount))
    except Exception:
        return make_response(400, "入帳金額數值格式不正確", None)
    if amount <= Decimal("0"):
        return make_response(400, "入帳金額必須大於 0", None)

    # 3. 幣別 (currency: 3碼大寫)
    raw_currency = payload.get("currency")
    if not raw_currency or not isinstance(raw_currency, str):
        return make_response(400, "幣別為必填欄位", None)
    currency = raw_currency.strip()
    if not CURRENCY_REGEX.match(currency):
        return make_response(400, "幣別格式不正確，必須為 3 碼大寫英文字母", None)

    # 4. 匯率 (fx_rate_to_twd 或 exchange_rate_to_twd > 0)
    raw_fx = payload.get("fx_rate_to_twd")
    if raw_fx is None:
        raw_fx = payload.get("exchange_rate_to_twd")
    if raw_fx is None:
        return make_response(400, "匯率為必填欄位", None)
    try:
        fx_rate_to_twd = Decimal(str(raw_fx))
    except Exception:
        return make_response(400, "匯率數值格式不正確", None)
    if fx_rate_to_twd <= Decimal("0"):
        return make_response(400, "匯率必須大於 0", None)

    # 5. 分類 (category_id)
    raw_category = payload.get("category_id")
    if raw_category is None or not isinstance(raw_category, int) or isinstance(raw_category, bool):
        return make_response(400, "入帳分類為必填欄位", None)
    category_id = raw_category
    category = db.session.get(IncomeCategory, category_id)
    if not category:
        return make_response(400, "指定的入帳分類不存在", None)

    # 6. 帳戶 (account_id)
    raw_account = payload.get("account_id")
    if raw_account is None or not isinstance(raw_account, int) or isinstance(raw_account, bool):
        return make_response(400, "入帳帳戶為必填欄位", None)
    account_id = raw_account
    account = db.session.get(Account, account_id)
    if not account:
        return make_response(400, "指定的帳戶不存在", None)

    # 7. source 與 note 至少一個有內容
    raw_source = payload.get("source")
    raw_note = payload.get("note")
    source = str(raw_source).strip() if (raw_source is not None and str(raw_source).strip()) else None
    note = str(raw_note).strip() if (raw_note is not None and str(raw_note).strip()) else None
    if source is None and note is None:
        return make_response(400, "來源與備註至少需填寫一項", None)

    # 8. invoice_id (可選)
    raw_invoice = payload.get("invoice_id")
    invoice_id = None
    if raw_invoice is not None:
        if not isinstance(raw_invoice, int) or isinstance(raw_invoice, bool):
            return make_response(400, "發票 ID 格式不正確", None)
        inv = db.session.get(Invoice, raw_invoice)
        if not inv:
            return make_response(400, "指定的發票不存在", None)
        existing_income = Income.query.filter_by(invoice_id=raw_invoice).first()
        if existing_income:
            return make_response(400, "該發票已關聯至其他入帳紀錄", None)
        invoice_id = raw_invoice

    income = Income(
        received_on=received_on,
        amount=amount,
        currency=currency,
        fx_rate_to_twd=fx_rate_to_twd,
        category_id=category_id,
        account_id=account_id,
        source=source,
        note=note,
        invoice_id=invoice_id,
    )
    db.session.add(income)
    db.session.commit()

    return make_response(201, "入帳紀錄建立成功", income.to_dict())


@api_bp.get("/incomes/<int:income_id>")
def get_income(income_id: int):
    income = db.session.get(Income, income_id)
    if not income:
        return make_response(404, "找不到此入帳紀錄", None)
    return make_response(200, "查詢成功", income.to_dict())


@api_bp.patch("/incomes/<int:income_id>")
def update_income(income_id: int):
    income = db.session.get(Income, income_id)
    if not income:
        return make_response(404, "找不到此入帳紀錄", None)

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return make_response(400, "請求格式必須為 JSON 物件", None)

    # 不可改主鍵與 invoice_id，若傳入直接忽略

    # 1. 更新 received_on / date
    if "received_on" in payload or "date" in payload:
        raw_date = payload.get("received_on") if "received_on" in payload else payload.get("date")
        if not raw_date:
            return make_response(400, "入帳日期不能為空", None)
        try:
            income.received_on = date.fromisoformat(str(raw_date))
        except Exception:
            return make_response(400, "入帳日期格式必須為 YYYY-MM-DD", None)

    # 2. 更新 amount
    if "amount" in payload:
        raw_amount = payload.get("amount")
        if raw_amount is None:
            return make_response(400, "入帳金額不能為空", None)
        try:
            amt = Decimal(str(raw_amount))
        except Exception:
            return make_response(400, "入帳金額數值格式不正確", None)
        if amt <= Decimal("0"):
            return make_response(400, "入帳金額必須大於 0", None)
        income.amount = amt

    # 3. 更新 currency
    if "currency" in payload:
        raw_currency = payload.get("currency")
        if not raw_currency or not isinstance(raw_currency, str):
            return make_response(400, "幣別不能為空", None)
        curr = raw_currency.strip()
        if not CURRENCY_REGEX.match(curr):
            return make_response(400, "幣別格式不正確，必須為 3 碼大寫英文字母", None)
        income.currency = curr

    # 4. 更新 fx_rate_to_twd
    if "fx_rate_to_twd" in payload or "exchange_rate_to_twd" in payload:
        raw_fx = payload.get("fx_rate_to_twd") if "fx_rate_to_twd" in payload else payload.get("exchange_rate_to_twd")
        if raw_fx is None:
            return make_response(400, "匯率不能為空", None)
        try:
            fx = Decimal(str(raw_fx))
        except Exception:
            return make_response(400, "匯率數值格式不正確", None)
        if fx <= Decimal("0"):
            return make_response(400, "匯率必須大於 0", None)
        income.fx_rate_to_twd = fx

    # 5. 更新 category_id
    if "category_id" in payload:
        raw_category = payload.get("category_id")
        if raw_category is None or not isinstance(raw_category, int) or isinstance(raw_category, bool):
            return make_response(400, "入帳分類 ID 格式不正確", None)
        category = db.session.get(IncomeCategory, raw_category)
        if not category:
            return make_response(400, "指定的入帳分類不存在", None)
        income.category_id = raw_category

    # 6. 更新 account_id
    if "account_id" in payload:
        raw_account = payload.get("account_id")
        if raw_account is None or not isinstance(raw_account, int) or isinstance(raw_account, bool):
            return make_response(400, "帳戶 ID 格式不正確", None)
        account = db.session.get(Account, raw_account)
        if not account:
            return make_response(400, "指定的帳戶不存在", None)
        income.account_id = raw_account

    # 7. 更新 source 與 note（需保證更新後兩者不可同時為空）
    if "source" in payload or "note" in payload:
        new_source = payload.get("source") if "source" in payload else income.source
        new_note = payload.get("note") if "note" in payload else income.note
        final_source = str(new_source).strip() if (new_source is not None and str(new_source).strip()) else None
        final_note = str(new_note).strip() if (new_note is not None and str(new_note).strip()) else None
        if final_source is None and final_note is None:
            return make_response(400, "來源與備註至少需填寫一項", None)
        if "source" in payload:
            income.source = final_source
        if "note" in payload:
            income.note = final_note

    income.updated_at = datetime.now(timezone.utc)
    db.session.commit()

    return make_response(200, "入帳紀錄更新成功", income.to_dict())


# ---------------------------------------------------------------------
# Expense Categories API
# ---------------------------------------------------------------------


@api_bp.get("/expense-categories")
def list_expense_categories():
    categories = ExpenseCategory.query.order_by(ExpenseCategory.id.asc()).all()
    return make_response(200, "查詢成功", [c.to_dict() for c in categories])


@api_bp.post("/expense-categories")
def create_expense_category():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return make_response(400, "請求格式必須為 JSON 物件", None)

    raw_name = payload.get("name")
    if raw_name is None or not isinstance(raw_name, str) or not raw_name.strip():
        return make_response(400, "分類名稱為必填欄位", None)
    name = raw_name.strip()

    existing = ExpenseCategory.query.filter_by(name=name).first()
    if existing:
        return make_response(400, "花費分類名稱已存在，請使用其他名稱", None)

    raw_desc = payload.get("description")
    description = str(raw_desc).strip() if (raw_desc is not None and str(raw_desc).strip()) else None

    category = ExpenseCategory(name=name, description=description)
    db.session.add(category)
    db.session.commit()

    return make_response(201, "花費分類建立成功", category.to_dict())


@api_bp.get("/expense-categories/<int:category_id>")
def get_expense_category(category_id: int):
    category = db.session.get(ExpenseCategory, category_id)
    if not category:
        return make_response(404, "找不到此花費分類", None)
    return make_response(200, "查詢成功", category.to_dict())


@api_bp.patch("/expense-categories/<int:category_id>")
def update_expense_category(category_id: int):
    category = db.session.get(ExpenseCategory, category_id)
    if not category:
        return make_response(404, "找不到此花費分類", None)

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return make_response(400, "請求格式必須為 JSON 物件", None)

    if "name" in payload:
        raw_name = payload.get("name")
        if raw_name is None or not isinstance(raw_name, str) or not raw_name.strip():
            return make_response(400, "分類名稱不能為空", None)
        new_name = raw_name.strip()
        if new_name != category.name:
            existing = ExpenseCategory.query.filter_by(name=new_name).first()
            if existing:
                return make_response(400, "花費分類名稱已存在，請使用其他名稱", None)
            category.name = new_name

    if "description" in payload:
        raw_desc = payload.get("description")
        category.description = str(raw_desc).strip() if (raw_desc is not None and str(raw_desc).strip()) else None

    category.updated_at = datetime.now(timezone.utc)
    db.session.commit()

    return make_response(200, "花費分類更新成功", category.to_dict())


# ---------------------------------------------------------------------
# Expenses API
# ---------------------------------------------------------------------


@api_bp.get("/expenses")
def list_expenses():
    query = Expense.query

    account_id_param = request.args.get("account_id")
    if account_id_param is not None and account_id_param.strip() != "":
        try:
            query = query.filter(Expense.account_id == int(account_id_param))
        except (ValueError, TypeError):
            return make_response(400, "帳戶 ID 必須為整數", None)

    category_id_param = request.args.get("category_id")
    if category_id_param is not None and category_id_param.strip() != "":
        try:
            query = query.filter(Expense.category_id == int(category_id_param))
        except (ValueError, TypeError):
            return make_response(400, "分類 ID 必須為整數", None)

    invoice_id_param = request.args.get("invoice_id")
    if invoice_id_param is not None and invoice_id_param.strip() != "":
        try:
            query = query.filter(Expense.invoice_id == int(invoice_id_param))
        except (ValueError, TypeError):
            return make_response(400, "發票 ID 必須為整數", None)

    date_from_param = request.args.get("date_from")
    if date_from_param is not None and date_from_param.strip() != "":
        try:
            d_from = date.fromisoformat(date_from_param.strip())
            query = query.filter(Expense.spent_on >= d_from)
        except (ValueError, TypeError):
            return make_response(400, "起始日期格式必須為 YYYY-MM-DD", None)

    date_to_param = request.args.get("date_to")
    if date_to_param is not None and date_to_param.strip() != "":
        try:
            d_to = date.fromisoformat(date_to_param.strip())
            query = query.filter(Expense.spent_on <= d_to)
        except (ValueError, TypeError):
            return make_response(400, "結束日期格式必須為 YYYY-MM-DD", None)

    expenses = query.order_by(Expense.spent_on.desc(), Expense.id.desc()).all()
    return make_response(200, "查詢成功", [exp.to_dict() for exp in expenses])


@api_bp.post("/expenses")
def create_expense():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return make_response(400, "請求格式必須為 JSON 物件", None)

    # 1. 消費日 (spent_on 或 date)
    raw_date = payload.get("spent_on") or payload.get("date")
    if not raw_date:
        return make_response(400, "消費日期為必填欄位", None)
    try:
        spent_on = date.fromisoformat(str(raw_date))
    except Exception:
        return make_response(400, "消費日期格式必須為 YYYY-MM-DD", None)

    # 2. 結算扣款日 (settled_on，可選)
    raw_settled = payload.get("settled_on")
    settled_on = None
    if raw_settled is not None and str(raw_settled).strip():
        try:
            settled_on = date.fromisoformat(str(raw_settled).strip())
        except Exception:
            return make_response(400, "結算扣款日格式必須為 YYYY-MM-DD", None)

    # 3. 金額 (amount > 0)
    raw_amount = payload.get("amount")
    if raw_amount is None:
        return make_response(400, "花費金額為必填欄位", None)
    try:
        amount = Decimal(str(raw_amount))
    except Exception:
        return make_response(400, "花費金額數值格式不正確", None)
    if amount <= Decimal("0"):
        return make_response(400, "花費金額必須大於 0", None)

    # 4. 幣別 (currency: 3碼大寫)
    raw_currency = payload.get("currency")
    if not raw_currency or not isinstance(raw_currency, str):
        return make_response(400, "幣別為必填欄位", None)
    currency = raw_currency.strip()
    if not CURRENCY_REGEX.match(currency):
        return make_response(400, "幣別格式不正確，必須為 3 碼大寫英文字母", None)

    # 5. 匯率 (fx_rate_to_twd 或 exchange_rate_to_twd > 0)
    raw_fx = payload.get("fx_rate_to_twd")
    if raw_fx is None:
        raw_fx = payload.get("exchange_rate_to_twd")
    if raw_fx is None:
        return make_response(400, "匯率為必填欄位", None)
    try:
        fx_rate_to_twd = Decimal(str(raw_fx))
    except Exception:
        return make_response(400, "匯率數值格式不正確", None)
    if fx_rate_to_twd <= Decimal("0"):
        return make_response(400, "匯率必須大於 0", None)

    # 6. 分類 (category_id)
    raw_category = payload.get("category_id")
    if raw_category is None or not isinstance(raw_category, int) or isinstance(raw_category, bool):
        return make_response(400, "花費分類為必填欄位", None)
    category_id = raw_category
    category = db.session.get(ExpenseCategory, category_id)
    if not category:
        return make_response(400, "指​​定的花費分類不存在", None)

    # 7. 帳戶 (account_id)
    raw_account = payload.get("account_id")
    if raw_account is None or not isinstance(raw_account, int) or isinstance(raw_account, bool):
        return make_response(400, "支付帳戶為必填欄位", None)
    account_id = raw_account
    account = db.session.get(Account, account_id)
    if not account:
        return make_response(400, "指定的帳戶不存在", None)

    # 8. merchant 與 note 至少一個有內容
    raw_merchant = payload.get("merchant")
    raw_note = payload.get("note")
    merchant = str(raw_merchant).strip() if (raw_merchant is not None and str(raw_merchant).strip()) else None
    note = str(raw_note).strip() if (raw_note is not None and str(raw_note).strip()) else None
    if merchant is None and note is None:
        return make_response(400, "商家與備註至少需填寫一項", None)

    # 9. invoice_id (可選，多對一，不需金額相等)
    raw_invoice = payload.get("invoice_id")
    invoice_id = None
    if raw_invoice is not None:
        if not isinstance(raw_invoice, int) or isinstance(raw_invoice, bool):
            return make_response(400, "發票 ID 格式不正確", None)
        inv = db.session.get(Invoice, raw_invoice)
        if not inv:
            return make_response(400, "指定的發票不存在", None)
        invoice_id = raw_invoice

    expense = Expense(
        spent_on=spent_on,
        settled_on=settled_on,
        amount=amount,
        currency=currency,
        fx_rate_to_twd=fx_rate_to_twd,
        category_id=category_id,
        account_id=account_id,
        merchant=merchant,
        note=note,
        invoice_id=invoice_id,
    )
    db.session.add(expense)
    db.session.commit()

    return make_response(201, "花費紀錄建立成功", expense.to_dict())


@api_bp.get("/expenses/<int:expense_id>")
def get_expense(expense_id: int):
    expense = db.session.get(Expense, expense_id)
    if not expense:
        return make_response(404, "找不到此花費紀錄", None)
    return make_response(200, "查詢成功", expense.to_dict())


@api_bp.patch("/expenses/<int:expense_id>")
def update_expense(expense_id: int):
    expense = db.session.get(Expense, expense_id)
    if not expense:
        return make_response(404, "找不到此花費紀錄", None)

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return make_response(400, "請求格式必須為 JSON 物件", None)

    # 1. 更新 spent_on / date
    if "spent_on" in payload or "date" in payload:
        raw_date = payload.get("spent_on") if "spent_on" in payload else payload.get("date")
        if not raw_date:
            return make_response(400, "消費日期不能為空", None)
        try:
            expense.spent_on = date.fromisoformat(str(raw_date))
        except Exception:
            return make_response(400, "消費日期格式必須為 YYYY-MM-DD", None)

    # 2. 更新 settled_on
    if "settled_on" in payload:
        raw_settled = payload.get("settled_on")
        if raw_settled is None or not str(raw_settled).strip():
            expense.settled_on = None
        else:
            try:
                expense.settled_on = date.fromisoformat(str(raw_settled).strip())
            except Exception:
                return make_response(400, "結算扣款日格式必須為 YYYY-MM-DD", None)

    # 3. 更新 amount
    if "amount" in payload:
        raw_amount = payload.get("amount")
        if raw_amount is None:
            return make_response(400, "花費金額不能為空", None)
        try:
            amt = Decimal(str(raw_amount))
        except Exception:
            return make_response(400, "花費金額數值格式不正確", None)
        if amt <= Decimal("0"):
            return make_response(400, "花費金額必須大於 0", None)
        expense.amount = amt

    # 4. 更新 currency
    if "currency" in payload:
        raw_currency = payload.get("currency")
        if not raw_currency or not isinstance(raw_currency, str):
            return make_response(400, "幣別不能為空", None)
        curr = raw_currency.strip()
        if not CURRENCY_REGEX.match(curr):
            return make_response(400, "幣別格式不正確，必須為 3 碼大寫英文字母", None)
        expense.currency = curr

    # 5. 更新 fx_rate_to_twd
    if "fx_rate_to_twd" in payload or "exchange_rate_to_twd" in payload:
        raw_fx = payload.get("fx_rate_to_twd") if "fx_rate_to_twd" in payload else payload.get("exchange_rate_to_twd")
        if raw_fx is None:
            return make_response(400, "匯率不能為空", None)
        try:
            fx = Decimal(str(raw_fx))
        except Exception:
            return make_response(400, "匯率數值格式不正確", None)
        if fx <= Decimal("0"):
            return make_response(400, "匯率必須大於 0", None)
        expense.fx_rate_to_twd = fx

    # 6. 更新 category_id
    if "category_id" in payload:
        raw_category = payload.get("category_id")
        if raw_category is None or not isinstance(raw_category, int) or isinstance(raw_category, bool):
            return make_response(400, "花費分類 ID 格式不正確", None)
        category = db.session.get(ExpenseCategory, raw_category)
        if not category:
            return make_response(400, "指定的花費分類不存在", None)
        expense.category_id = raw_category

    # 7. 更新 account_id
    if "account_id" in payload:
        raw_account = payload.get("account_id")
        if raw_account is None or not isinstance(raw_account, int) or isinstance(raw_account, bool):
            return make_response(400, "帳戶 ID 格式不正確", None)
        account = db.session.get(Account, raw_account)
        if not account:
            return make_response(400, "指定的帳戶不存在", None)
        expense.account_id = raw_account

    # 8. 更新 invoice_id (可設為 None)
    if "invoice_id" in payload:
        raw_invoice = payload.get("invoice_id")
        if raw_invoice is None:
            expense.invoice_id = None
        else:
            if not isinstance(raw_invoice, int) or isinstance(raw_invoice, bool):
                return make_response(400, "發票 ID 格式不正確", None)
            inv = db.session.get(Invoice, raw_invoice)
            if not inv:
                return make_response(400, "指定的發票不存在", None)
            expense.invoice_id = raw_invoice

    # 9. 更新 merchant 與 note（需保證更新後兩者不可同時為空）
    if "merchant" in payload or "note" in payload:
        new_merchant = payload.get("merchant") if "merchant" in payload else expense.merchant
        new_note = payload.get("note") if "note" in payload else expense.note
        final_merchant = str(new_merchant).strip() if (new_merchant is not None and str(new_merchant).strip()) else None
        final_note = str(new_note).strip() if (new_note is not None and str(new_note).strip()) else None
        if final_merchant is None and final_note is None:
            return make_response(400, "商家與備註至少需填寫一項", None)
        if "merchant" in payload:
            expense.merchant = final_merchant
        if "note" in payload:
            expense.note = final_note

    expense.updated_at = datetime.now(timezone.utc)
    db.session.commit()

    return make_response(200, "花費紀錄更新成功", expense.to_dict())


# ---------------------------------------------------------------------
# Invoices API
# ---------------------------------------------------------------------


@api_bp.post("/invoices")
def create_invoice():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return make_response(400, "請求格式必須為 JSON 物件", None)

    # 1. 必填號碼 (2碼大寫英文+8碼數字)
    raw_number = payload.get("number")
    if not raw_number or not isinstance(raw_number, str):
        return make_response(400, "發票號碼為必填欄位", None)
    number = raw_number.strip().upper()
    if not INVOICE_NUMBER_REGEX.match(number):
        return make_response(400, "發票號碼格式不正確，必須為 2 碼大寫英文字母加 8 碼數字", None)

    existing = Invoice.query.filter_by(number=number).first()
    if existing:
        return make_response(400, "發票號碼已存在，請勿重複建立", None)

    # 2. 必填開立日 (issued_on 或 date)
    raw_issued = payload.get("issued_on") or payload.get("date")
    if not raw_issued:
        return make_response(400, "發票開立日期為必填欄位", None)
    try:
        issued_on = date.fromisoformat(str(raw_issued).strip())
    except Exception:
        return make_response(400, "開立日期格式必須為 YYYY-MM-DD", None)

    # 3. 必填金額 (amount > 0)
    raw_amount = payload.get("amount")
    if raw_amount is None:
        return make_response(400, "發票金額為必填欄位", None)
    try:
        amount = Decimal(str(raw_amount))
    except Exception:
        return make_response(400, "發票金額數值格式不正確", None)
    if amount <= Decimal("0"):
        return make_response(400, "發票金額必須大於 0", None)

    # 4. 必填種類 (paper, electronic)
    raw_kind = payload.get("kind")
    if not raw_kind or not isinstance(raw_kind, str):
        return make_response(400, "發票種類為必填欄位", None)
    kind = raw_kind.strip().lower()
    if kind not in ("paper", "electronic"):
        return make_response(400, "發票種類必須為 paper 或 electronic", None)

    # 5. 幣別 (未送時預設 TWD)
    raw_currency = payload.get("currency")
    if raw_currency is None or not str(raw_currency).strip():
        currency = "TWD"
    else:
        currency = str(raw_currency).strip().upper()
        if not CURRENCY_REGEX.match(currency):
            return make_response(400, "幣別格式不正確，必須為 3 碼大寫英文字母", None)

    # 6. 匯率 (匯率未送且幣別為 TWD 時用 1)
    raw_fx = payload.get("fx_rate_to_twd")
    if raw_fx is None:
        raw_fx = payload.get("exchange_rate_to_twd")
    if raw_fx is None or not str(raw_fx).strip():
        if currency == "TWD":
            fx_rate_to_twd = Decimal("1.000000")
        else:
            return make_response(400, "非台幣發票必須填寫對台幣匯率", None)
    else:
        try:
            fx_rate_to_twd = Decimal(str(raw_fx))
        except Exception:
            return make_response(400, "匯率數值格式不正確", None)
        if fx_rate_to_twd <= Decimal("0"):
            return make_response(400, "匯率必須大於 0", None)

    # 7. 選填 random_code (4碼數字)
    raw_random = payload.get("random_code")
    random_code = None
    if raw_random is not None and str(raw_random).strip():
        random_code = str(raw_random).strip()
        if not RANDOM_CODE_REGEX.match(random_code):
            return make_response(400, "隨機碼必須為 4 碼數字", None)

    # 8. 選填 seller_name
    raw_seller_name = payload.get("seller_name")
    seller_name = str(raw_seller_name).strip() if (raw_seller_name is not None and str(raw_seller_name).strip()) else None

    # 9. 選填 seller_ban (8碼數字統編)
    raw_seller_ban = payload.get("seller_ban")
    seller_ban = None
    if raw_seller_ban is not None and str(raw_seller_ban).strip():
        seller_ban = str(raw_seller_ban).strip()
        if not BAN_REGEX.match(seller_ban):
            return make_response(400, "賣方統一編號必須為 8 碼數字", None)

    invoice = Invoice(
        number=number,
        issued_on=issued_on,
        amount=amount,
        kind=kind,
        currency=currency,
        fx_rate_to_twd=fx_rate_to_twd,
        random_code=random_code,
        seller_name=seller_name,
        seller_ban=seller_ban,
        prize_status="unchecked",
    )
    db.session.add(invoice)
    db.session.commit()

    return make_response(201, "發票建立成功", invoice.to_dict())


@api_bp.get("/invoices")
def list_invoices():
    query = Invoice.query

    period_key_param = request.args.get("period_key")
    if period_key_param is not None and period_key_param.strip() != "":
        try:
            start_date, end_date = get_period_date_range(period_key_param.strip())
            query = query.filter(Invoice.issued_on >= start_date, Invoice.issued_on <= end_date)
        except Exception:
            return make_response(400, "期別格式不正確，必須為 YYYY-MM", None)

    prize_status_param = request.args.get("prize_status")
    if prize_status_param is not None and prize_status_param.strip() != "":
        query = query.filter(Invoice.prize_status == prize_status_param.strip())

    invoices = query.order_by(Invoice.issued_on.desc(), Invoice.id.desc()).all()
    return make_response(200, "查詢成功", [inv.to_dict() for inv in invoices])


@api_bp.get("/invoices/<int:invoice_id>")
def get_invoice(invoice_id: int):
    invoice = db.session.get(Invoice, invoice_id)
    if not invoice:
        return make_response(404, "找不到此發票", None)

    data = invoice.to_dict()
    data["items"] = [item.to_dict() for item in invoice.items]
    data["expense_ids"] = [exp.id for exp in invoice.expenses]
    return make_response(200, "查詢成功", data)


@api_bp.patch("/invoices/<int:invoice_id>")
def update_invoice(invoice_id: int):
    invoice = db.session.get(Invoice, invoice_id)
    if not invoice:
        return make_response(404, "找不到此發票", None)

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return make_response(400, "請求格式必須為 JSON 物件", None)

    # 不可改號碼與主鍵，傳入自動忽略

    if "seller_name" in payload:
        raw_seller_name = payload.get("seller_name")
        invoice.seller_name = str(raw_seller_name).strip() if (raw_seller_name is not None and str(raw_seller_name).strip()) else None

    if "seller_ban" in payload:
        raw_seller_ban = payload.get("seller_ban")
        if raw_seller_ban is None or not str(raw_seller_ban).strip():
            invoice.seller_ban = None
        else:
            ban = str(raw_seller_ban).strip()
            if not BAN_REGEX.match(ban):
                return make_response(400, "賣方統一編號必須為 8 碼數字", None)
            invoice.seller_ban = ban

    if "random_code" in payload:
        raw_random = payload.get("random_code")
        if raw_random is None or not str(raw_random).strip():
            invoice.random_code = None
        else:
            code = str(raw_random).strip()
            if not RANDOM_CODE_REGEX.match(code):
                return make_response(400, "隨機碼必須為 4 碼數字", None)
            invoice.random_code = code

    if "prize_status" in payload:
        val = payload.get("prize_status")
        if val == "void":
            invoice.prize_status = "void"
        elif val in ("unchecked", "lost", "won", "claimed"):
            invoice.prize_status = val
        else:
            return make_response(400, "無效的兌獎狀態", None)

    invoice.updated_at = datetime.now(timezone.utc)
    db.session.commit()

    return make_response(200, "發票更新成功", invoice.to_dict())


# ---------------------------------------------------------------------
# Invoice Items API
# ---------------------------------------------------------------------


@api_bp.post("/invoices/<int:invoice_id>/items")
def create_invoice_item(invoice_id: int):
    invoice = db.session.get(Invoice, invoice_id)
    if not invoice:
        return make_response(404, "找不到此發票", None)

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return make_response(400, "請求格式必須為 JSON 物件", None)

    raw_name = payload.get("name")
    if not raw_name or not isinstance(raw_name, str) or not raw_name.strip():
        return make_response(400, "品項名稱為必填欄位", None)
    name = raw_name.strip()

    raw_quantity = payload.get("quantity")
    if raw_quantity is None:
        return make_response(400, "數量為必填欄位", None)
    try:
        quantity = Decimal(str(raw_quantity))
    except Exception:
        return make_response(400, "數量數值格式不正確", None)
    if quantity <= Decimal("0"):
        return make_response(400, "數量必須大於 0", None)

    raw_unit_price = payload.get("unit_price")
    if raw_unit_price is None:
        return make_response(400, "單價為必填欄位", None)
    try:
        unit_price = Decimal(str(raw_unit_price))
    except Exception:
        return make_response(400, "單價數值格式不正確", None)
    if unit_price <= Decimal("0"):
        return make_response(400, "單價必須大於 0", None)

    raw_line_amount = payload.get("line_amount")
    if raw_line_amount is None:
        return make_response(400, "小計金額為必填欄位", None)
    try:
        line_amount = Decimal(str(raw_line_amount))
    except Exception:
        return make_response(400, "小計金額數值格式不正確", None)
    if line_amount <= Decimal("0"):
        return make_response(400, "小計金額必須大於 0", None)

    item = InvoiceItem(
        invoice_id=invoice.id,
        name=name,
        quantity=quantity,
        unit_price=unit_price,
        line_amount=line_amount,
    )
    db.session.add(item)
    db.session.commit()

    return make_response(201, "品項新增成功", item.to_dict())


@api_bp.get("/invoices/<int:invoice_id>/items")
def list_invoice_items(invoice_id: int):
    invoice = db.session.get(Invoice, invoice_id)
    if not invoice:
        return make_response(404, "找不到此發票", None)

    items = InvoiceItem.query.filter_by(invoice_id=invoice_id).order_by(InvoiceItem.id.asc()).all()
    return make_response(200, "查詢成功", [item.to_dict() for item in items])


# ---------------------------------------------------------------------
# Draw Periods & Prizes API
# ---------------------------------------------------------------------


@api_bp.post("/draw-periods")
def create_draw_period():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return make_response(400, "請求格式必須為 JSON 物件", None)

    raw_key = payload.get("period_key")
    if not raw_key or not isinstance(raw_key, str) or not raw_key.strip():
        return make_response(400, "開獎期別 (period_key) 為必填欄位", None)
    period_key = raw_key.strip()
    if not PERIOD_KEY_REGEX.match(period_key):
        return make_response(400, "開獎期別格式必須為 YYYY-MM，且月份必須為 01、03、05、07、09、11", None)

    existing = DrawPeriod.query.filter_by(period_key=period_key).first()
    if existing:
        return make_response(400, "開獎期別已存在，請勿重複建立", None)

    period = DrawPeriod(period_key=period_key)
    db.session.add(period)
    db.session.commit()

    return make_response(201, "開獎期別建立成功", period.to_dict())


@api_bp.post("/draw-periods/<int:period_id>/prizes")
def create_draw_prize(period_id: int):
    period = db.session.get(DrawPeriod, period_id)
    if not period:
        return make_response(404, "找不到此開獎期別", None)

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return make_response(400, "請求格式必須為 JSON 物件", None)

    raw_tier = payload.get("tier")
    if not raw_tier or not isinstance(raw_tier, str):
        return make_response(400, "獎項級別 (tier) 為必填欄位", None)
    tier = raw_tier.strip().lower()
    if tier not in VALID_DRAW_TIERS:
        return make_response(400, "無效的獎項級別", None)

    raw_number = payload.get("matching_number")
    if not raw_number or not isinstance(raw_number, str) or not raw_number.strip():
        return make_response(400, "中獎號碼 (matching_number) 為必填欄位", None)
    matching_number = raw_number.strip()

    if tier in ("special", "grand", "first"):
        if not EIGHT_DIGIT_REGEX.match(matching_number):
            return make_response(400, "特別獎、特獎、頭獎的中獎號碼必須為 8 碼數字", None)

    raw_amount = payload.get("amount")
    if raw_amount is None:
        return make_response(400, "獎金金額 (amount) 為必填欄位", None)
    try:
        amount = Decimal(str(raw_amount))
    except Exception:
        return make_response(400, "獎金金額數值格式不正確", None)
    if amount < Decimal("0"):
        return make_response(400, "獎金金額不能小於 0", None)

    existing = DrawPrize.query.filter_by(
        draw_period_id=period.id,
        tier=tier,
        matching_number=matching_number,
    ).first()
    if existing:
        return make_response(400, "該期別已有相同獎項與獎號設定，請勿重複新增", None)

    prize = DrawPrize(
        draw_period_id=period.id,
        tier=tier,
        matching_number=matching_number,
        amount=amount,
    )
    db.session.add(prize)
    db.session.commit()

    return make_response(201, "獎項設定新增成功", prize.to_dict())


@api_bp.post("/draw-periods/<int:period_id>/check")
def check_draw_period_invoices(period_id: int):
    period = db.session.get(DrawPeriod, period_id)
    if not period:
        return make_response(404, "找不到此開獎期別", None)

    try:
        start_date, end_date = get_period_date_range(period.period_key)
    except Exception as e:
        return make_response(400, f"期別解析失敗: {e}", None)

    invoices = Invoice.query.filter(
        Invoice.issued_on >= start_date,
        Invoice.issued_on <= end_date,
        Invoice.prize_status != "void",
    ).all()

    prizes = DrawPrize.query.filter_by(draw_period_id=period.id).all()

    checked_count = len(invoices)
    won_count = 0

    for inv in invoices:
        inv_digits = inv.number[2:]
        matched = [p for p in prizes if check_invoice_prize_match(inv_digits, p)]
        if matched:
            won_count += 1
            if inv.prize_status != "claimed":
                inv.prize_status = "won"
        else:
            if inv.prize_status != "claimed":
                inv.prize_status = "lost"
        inv.updated_at = datetime.now(timezone.utc)

    db.session.commit()

    return make_response(
        200,
        "發票開獎對獎完成",
        {"checked_count": checked_count, "won_count": won_count},
    )


@api_bp.post("/invoices/<int:invoice_id>/prize-income")
def claim_invoice_prize_income(invoice_id: int):
    invoice = db.session.get(Invoice, invoice_id)
    if not invoice:
        return make_response(404, "找不到此發票", None)

    if invoice.prize_status != "won":
        return make_response(400, "發票狀態必須為中獎 (won) 方可建立中獎入帳", None)

    existing_income = Income.query.filter_by(invoice_id=invoice_id).first()
    if existing_income:
        return make_response(400, "此發票已有中獎入帳紀錄，請勿重複請領", None)

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return make_response(400, "請求格式必須為 JSON 物件", None)

    raw_category = payload.get("category_id")
    if raw_category is None or not isinstance(raw_category, int) or isinstance(raw_category, bool):
        return make_response(400, "入帳分類 ID (category_id) 為必填欄位", None)
    category = db.session.get(IncomeCategory, raw_category)
    if not category:
        return make_response(400, "指定的入帳分類不存在", None)

    raw_account = payload.get("account_id")
    if raw_account is None or not isinstance(raw_account, int) or isinstance(raw_account, bool):
        return make_response(400, "入款帳戶 ID (account_id) 為必填欄位", None)
    account = db.session.get(Account, raw_account)
    if not account:
        return make_response(400, "指定的帳戶不存在", None)

    # 查出該發票對應的中獎金額（只讀 draw_prizes.amount）
    period_key = get_period_key(invoice.issued_on)
    period = DrawPeriod.query.filter_by(period_key=period_key).first()
    if not period:
        return make_response(400, f"找不到開獎期別 {period_key} 之獎項設定", None)

    inv_digits = invoice.number[2:]
    matched = [p for p in period.prizes if check_invoice_prize_match(inv_digits, p)]
    if not matched:
        return make_response(400, "該發票號碼未對中任何有效獎項", None)

    best_prize = max(matched, key=lambda p: p.amount)
    prize_amount = best_prize.amount

    # 同一筆交易內建立入帳並改為 claimed
    raw_date = payload.get("received_on") or payload.get("date")
    if raw_date:
        try:
            received_on = date.fromisoformat(str(raw_date).strip())
        except Exception:
            return make_response(400, "入帳日期格式必須為 YYYY-MM-DD", None)
    else:
        received_on = date.today()

    source = f"統一發票中獎 ({invoice.number})"
    raw_note = payload.get("note")
    note = str(raw_note).strip() if (raw_note is not None and str(raw_note).strip()) else f"期別 {period_key} 中獎獎金"

    try:
        invoice.prize_status = "claimed"
        invoice.updated_at = datetime.now(timezone.utc)

        income = Income(
            received_on=received_on,
            amount=prize_amount,
            currency=invoice.currency,
            fx_rate_to_twd=invoice.fx_rate_to_twd,
            category_id=category.id,
            account_id=account.id,
            source=source,
            note=note,
            invoice_id=invoice.id,
        )
        db.session.add(income)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        current_app.logger.error("建立中獎入帳交易失敗: %s", e)
        return make_response(500, "建立中獎入帳失敗，交易已完整回復", None)

    return make_response(201, "中獎入帳建立成功", income.to_dict())


# ---------------------------------------------------------------------
# Instruments API
# ---------------------------------------------------------------------


@api_bp.post("/instruments")
def create_instrument():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return make_response(400, "請求格式必須為 JSON 物件", None)

    raw_market = payload.get("market")
    if not raw_market or not isinstance(raw_market, str):
        return make_response(400, "交易市場 (market) 為必填欄位", None)
    market = raw_market.strip().lower()
    if market not in VALID_MARKETS:
        return make_response(400, "無效的交易市場，只允許 tw_stock、us_stock、fund 或 other", None)

    raw_symbol = payload.get("symbol")
    if not raw_symbol or not isinstance(raw_symbol, str) or not raw_symbol.strip():
        return make_response(400, "標的代號 (symbol) 為必填欄位", None)
    symbol = raw_symbol.strip().upper()

    raw_name = payload.get("name")
    if not raw_name or not isinstance(raw_name, str) or not raw_name.strip():
        return make_response(400, "標的名稱 (name) 為必填欄位", None)
    name = raw_name.strip()

    raw_currency = payload.get("currency")
    if not raw_currency or not isinstance(raw_currency, str):
        return make_response(400, "計價幣別 (currency) 為必填欄位", None)
    currency = raw_currency.strip().upper()
    if not CURRENCY_REGEX.match(currency):
        return make_response(400, "幣別格式不正確，必須為 3 碼大寫英文字母", None)

    existing = Instrument.query.filter_by(market=market, symbol=symbol).first()
    if existing:
        return make_response(400, "此市場與代號的投資標的已存在，請勿重複建立", None)

    instrument = Instrument(
        market=market,
        symbol=symbol,
        name=name,
        currency=currency,
    )
    db.session.add(instrument)
    db.session.commit()

    return make_response(201, "投資標的建立成功", instrument.to_dict())


@api_bp.get("/instruments")
def list_instruments():
    instruments = Instrument.query.order_by(Instrument.id.asc()).all()
    return make_response(200, "查詢成功", [inst.to_dict() for inst in instruments])


@api_bp.get("/instruments/<int:instrument_id>")
def get_instrument(instrument_id: int):
    instrument = db.session.get(Instrument, instrument_id)
    if not instrument:
        return make_response(404, "找不到此投資標的", None)
    return make_response(200, "查詢成功", instrument.to_dict())


@api_bp.get("/instruments/<int:instrument_id>/holding")
def get_instrument_holding(instrument_id: int):
    instrument = db.session.get(Instrument, instrument_id)
    if not instrument:
        return make_response(404, "找不到此投資標的", None)

    shares, avg_cost = compute_instrument_holding(instrument_id)
    data = {
        "instrument_id": instrument.id,
        "currency": instrument.currency,
        "quantity": float(shares),
        "average_cost": float(avg_cost),
    }
    return make_response(200, "查詢持股庫存成功", data)


# ---------------------------------------------------------------------
# Trades API
# ---------------------------------------------------------------------


@api_bp.post("/trades")
def create_trade():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return make_response(400, "請求格式必須為 JSON 物件", None)

    # 1. 必填標的 (instrument_id)
    raw_inst = payload.get("instrument_id")
    if raw_inst is None or not isinstance(raw_inst, int) or isinstance(raw_inst, bool):
        return make_response(400, "投資標的 ID (instrument_id) 為必填欄位", None)
    instrument = db.session.get(Instrument, raw_inst)
    if not instrument:
        return make_response(400, "指定的投資標的不存在", None)

    # 2. 必填帳戶 (account_id)
    raw_acc = payload.get("account_id")
    if raw_acc is None or not isinstance(raw_acc, int) or isinstance(raw_acc, bool):
        return make_response(400, "交割帳戶 ID (account_id) 為必填欄位", None)
    account = db.session.get(Account, raw_acc)
    if not account:
        return make_response(400, "指定的帳戶不存在", None)

    # 3. 必填種類 (trade_type)
    raw_type = payload.get("trade_type")
    if not raw_type or not isinstance(raw_type, str):
        return make_response(400, "交易種類 (trade_type) 為必填欄位", None)
    trade_type = raw_type.strip().lower()
    if trade_type not in VALID_TRADE_TYPES:
        return make_response(400, "無效的交易種類", None)

    # 4. 必填日期 (traded_on 或 date)
    raw_date = payload.get("traded_on") or payload.get("date")
    if not raw_date:
        return make_response(400, "交易日期為必填欄位", None)
    try:
        traded_on = date.fromisoformat(str(raw_date).strip())
    except Exception:
        return make_response(400, "交易日期格式必須為 YYYY-MM-DD", None)

    # 5. 必填幣別 (currency)
    raw_curr = payload.get("currency")
    if not raw_curr or not isinstance(raw_curr, str):
        return make_response(400, "幣別 (currency) 為必填欄位", None)
    currency = raw_curr.strip().upper()
    if not CURRENCY_REGEX.match(currency):
        return make_response(400, "幣別格式不正確，必須為 3 碼大寫英文字母", None)

    # 6. 必填匯率 (fx_rate_to_twd 或 exchange_rate_to_twd > 0)
    raw_fx = payload.get("fx_rate_to_twd")
    if raw_fx is None:
        raw_fx = payload.get("exchange_rate_to_twd")
    if raw_fx is None or not str(raw_fx).strip():
        return make_response(400, "對台幣匯率 (fx_rate_to_twd) 為必填欄位", None)
    try:
        fx_rate_to_twd = Decimal(str(raw_fx))
    except Exception:
        return make_response(400, "匯率數值格式不正確", None)
    if fx_rate_to_twd <= Decimal("0"):
        return make_response(400, "匯率必須大於 0", None)

    # 7. 依種類解析欄位數值
    quantity = None
    price = None
    cash_amount = None
    ratio = None
    fee = None
    tax = None

    if payload.get("fee") is not None and str(payload.get("fee")).strip():
        try:
            fee = Decimal(str(payload.get("fee")))
            if fee < Decimal("0"):
                return make_response(400, "手續費不能為負數", None)
        except Exception:
            return make_response(400, "手續費數值格式不正確", None)

    if payload.get("tax") is not None and str(payload.get("tax")).strip():
        try:
            tax = Decimal(str(payload.get("tax")))
            if tax < Decimal("0"):
                return make_response(400, "交易稅不能為負數", None)
        except Exception:
            return make_response(400, "交易稅數值格式不正確", None)

    if trade_type in ("buy", "sell", "drip"):
        raw_qty = payload.get("quantity")
        if raw_qty is None:
            return make_response(400, f"{trade_type} 交易必須填寫數量 (quantity)", None)
        try:
            quantity = Decimal(str(raw_qty))
            if quantity <= Decimal("0"):
                return make_response(400, "交易數量必須大於 0", None)
        except Exception:
            return make_response(400, "交易數量數值格式不正確", None)

        raw_price = payload.get("price")
        if raw_price is None:
            return make_response(400, f"{trade_type} 交易必須填寫每股單價 (price)", None)
        try:
            price = Decimal(str(raw_price))
            if price <= Decimal("0"):
                return make_response(400, "每股單價必須大於 0", None)
        except Exception:
            return make_response(400, "每股單價數值格式不正確", None)

        if payload.get("cash_amount") is not None and str(payload.get("cash_amount")).strip():
            try:
                cash_amount = Decimal(str(payload.get("cash_amount")))
            except Exception:
                return make_response(400, "現金金額數值格式不正確", None)
        else:
            cash_amount = quantity * price

    elif trade_type == "stock_dividend":
        raw_qty = payload.get("quantity")
        if raw_qty is None:
            return make_response(400, "股票股利交易必須填寫配股股數 (quantity)", None)
        try:
            quantity = Decimal(str(raw_qty))
            if quantity <= Decimal("0"):
                return make_response(400, "配股股數必須大於 0", None)
        except Exception:
            return make_response(400, "配股股數數值格式不正確", None)

    elif trade_type in ("split", "merge"):
        raw_ratio = payload.get("ratio")
        if raw_ratio is None:
            return make_response(400, f"{trade_type} 交易必須填寫比例 (ratio)", None)
        try:
            ratio = Decimal(str(raw_ratio))
            if ratio <= Decimal("0"):
                return make_response(400, "分割或合併比例必須大於 0", None)
        except Exception:
            return make_response(400, "比例數值格式不正確", None)

    elif trade_type == "cash_dividend":
        raw_cash = payload.get("cash_amount")
        if raw_cash is None:
            return make_response(400, "現金股利交易必須填寫現金金額 (cash_amount)", None)
        try:
            cash_amount = Decimal(str(raw_cash))
            if cash_amount <= Decimal("0"):
                return make_response(400, "現金金額必須大於 0", None)
        except Exception:
            return make_response(400, "現金金額數值格式不正確", None)

    elif trade_type == "capital_reduction":
        if payload.get("quantity") is not None and str(payload.get("quantity")).strip():
            try:
                quantity = Decimal(str(payload.get("quantity")))
                if quantity < Decimal("0"):
                    return make_response(400, "減資股數不能為負數", None)
            except Exception:
                return make_response(400, "減資股數數值格式不正確", None)
        if payload.get("cash_amount") is not None and str(payload.get("cash_amount")).strip():
            try:
                cash_amount = Decimal(str(payload.get("cash_amount")))
                if cash_amount < Decimal("0"):
                    return make_response(400, "退還現金金額不能為負數", None)
            except Exception:
                return make_response(400, "退還現金金額數值格式不正確", None)

    # 8. 賣出與減資不可使持有小於 0
    if trade_type in ("sell", "capital_reduction"):
        reduce_qty = quantity if quantity is not None else Decimal("0")
        if reduce_qty > Decimal("0"):
            current_shares, _ = compute_instrument_holding(instrument.id)
            if current_shares < reduce_qty:
                return make_response(400, "欲扣減之股數大於目前持有股數", None)

    # 9. 檢查 post_income 欄位
    post_income = payload.get("post_income") is True
    income_category = None
    if post_income:
        if trade_type not in ("cash_dividend", "capital_reduction") or not cash_amount or cash_amount <= Decimal("0"):
            return make_response(400, "只有現金股利或退還現金之減資才可同步建立入帳", None)
        raw_inc_cat = payload.get("income_category_id")
        if raw_inc_cat is None or not isinstance(raw_inc_cat, int) or isinstance(raw_inc_cat, bool):
            return make_response(400, "同步建立入帳時，入帳分類 ID (income_category_id) 為必填欄位", None)
        income_category = db.session.get(IncomeCategory, raw_inc_cat)
        if not income_category:
            return make_response(400, "指定的入帳分類不存在", None)

    trade = Trade(
        instrument_id=instrument.id,
        account_id=account.id,
        trade_type=trade_type,
        traded_on=traded_on,
        quantity=quantity,
        price=price,
        cash_amount=cash_amount,
        ratio=ratio,
        fee=fee,
        tax=tax,
        currency=currency,
        fx_rate_to_twd=fx_rate_to_twd,
    )

    try:
        db.session.add(trade)
        db.session.flush()

        if post_income and income_category:
            income = Income(
                received_on=trade.traded_on,
                amount=cash_amount,
                currency=trade.currency,
                fx_rate_to_twd=trade.fx_rate_to_twd,
                category_id=income_category.id,
                account_id=trade.account_id,
                source=f"投資收益 ({instrument.symbol} {trade.trade_type})",
                note=payload.get("note") or f"{instrument.name} {trade.trade_type}",
                trade_id=trade.id,
            )
            db.session.add(income)

        db.session.commit()
    except Exception as e:
        db.session.rollback()
        current_app.logger.error("新增交易失敗: %s", e)
        return make_response(500, "新增交易失敗，交易已完整回復", None)

    return make_response(201, "投資交易建立成功", trade.to_dict())


@api_bp.get("/trades")
def list_trades():
    query = Trade.query

    inst_param = request.args.get("instrument_id")
    if inst_param is not None and inst_param.strip() != "":
        try:
            query = query.filter(Trade.instrument_id == int(inst_param))
        except (ValueError, TypeError):
            return make_response(400, "標的 ID 必須為整數", None)

    acc_param = request.args.get("account_id")
    if acc_param is not None and acc_param.strip() != "":
        try:
            query = query.filter(Trade.account_id == int(acc_param))
        except (ValueError, TypeError):
            return make_response(400, "帳戶 ID 必須為整數", None)

    trades = query.order_by(Trade.traded_on.desc(), Trade.id.desc()).all()
    return make_response(200, "查詢成功", [tr.to_dict() for tr in trades])


@api_bp.post("/trades/<int:trade_id>/void")
def void_trade(trade_id: int):
    trade = db.session.get(Trade, trade_id)
    if not trade:
        return make_response(404, "找不到此投資交易", None)

    existing_income = Income.query.filter_by(trade_id=trade_id).first()
    if existing_income:
        return make_response(400, "此交易已產生入帳紀錄，不可作廢", None)

    trade.voided_at = datetime.now(timezone.utc)
    trade.updated_at = datetime.now(timezone.utc)
    db.session.commit()

    return make_response(200, "交易已成功作廢", trade.to_dict())


# ---------------------------------------------------------------------
# Summary API
# ---------------------------------------------------------------------


@api_bp.get("/summary")
def get_monthly_summary():
    month_param = request.args.get("month")
    if not month_param or not MONTH_REGEX.match(month_param.strip()):
        return make_response(400, "請提供正確的月份參數 (month)，格式為 YYYY-MM", None)

    parts = month_param.strip().split("-")
    year, month = int(parts[0]), int(parts[1])
    last_day = calendar.monthrange(year, month)[1]
    start_date = date(year, month, 1)
    end_date = date(year, month, last_day)

    month_incomes = Income.query.filter(
        Income.received_on >= start_date,
        Income.received_on <= end_date,
    ).all()
    income_twd = sum(
        (
            Decimal(str(inc.amount)) * Decimal(str(inc.fx_rate_to_twd))
            for inc in month_incomes
        ),
        Decimal("0"),
    )

    month_expenses = Expense.query.filter(
        Expense.spent_on >= start_date,
        Expense.spent_on <= end_date,
    ).all()
    expense_twd = sum(
        (
            Decimal(str(exp.amount)) * Decimal(str(exp.fx_rate_to_twd))
            for exp in month_expenses
        ),
        Decimal("0"),
    )

    data = {
        "month": month_param.strip(),
        "income_twd": float(income_twd),
        "expense_twd": float(expense_twd),
    }
    return make_response(200, "查詢月度收支摘要成功", data)




