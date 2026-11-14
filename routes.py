import re
from datetime import date, datetime, timezone
from decimal import Decimal

from flask import Blueprint, current_app, jsonify, request
from sqlalchemy.exc import IntegrityError, OperationalError

from extensions import db
from models import Account, Transfer

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

    opening = Decimal(str(account.opening_balance or 0))
    in_sum = sum((Decimal(str(t.amount)) for t in in_transfers), Decimal("0"))
    out_sum = sum((Decimal(str(t.amount)) for t in out_transfers), Decimal("0"))
    balance = opening + in_sum - out_sum

    in_sum_twd = sum(
        (Decimal(str(t.amount)) * Decimal(str(t.fx_rate_to_twd)) for t in in_transfers),
        Decimal("0"),
    )
    out_sum_twd = sum(
        (Decimal(str(t.amount)) * Decimal(str(t.fx_rate_to_twd)) for t in out_transfers),
        Decimal("0"),
    )
    # 若帳戶幣別即為 TWD，opening 本身即為台幣；非 TWD 帳戶則以原期初基準計入台幣計算
    opening_twd = opening
    balance_twd = opening_twd + in_sum_twd - out_sum_twd

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
