import re
from datetime import date, datetime, timezone
from decimal import Decimal

from flask import Blueprint, current_app, jsonify, request
from sqlalchemy.exc import IntegrityError, OperationalError

from extensions import db
from models import Account, Income, IncomeCategory, Invoice, Transfer

api_bp = Blueprint("api", __name__, url_prefix="/api")

VALID_ACCOUNT_KINDS = {"cash", "bank", "credit_card", "broker"}
CURRENCY_REGEX = re.compile(r"^[A-Z]{3}$")


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

    opening = Decimal(str(account.opening_balance or 0))
    in_sum = sum((Decimal(str(t.amount)) for t in in_transfers), Decimal("0"))
    out_sum = sum((Decimal(str(t.amount)) for t in out_transfers), Decimal("0"))
    income_sum = sum((Decimal(str(inc.amount)) for inc in account_incomes), Decimal("0"))
    balance = opening + in_sum - out_sum + income_sum

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
    opening_twd = opening
    balance_twd = opening_twd + in_sum_twd - out_sum_twd + income_sum_twd

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
