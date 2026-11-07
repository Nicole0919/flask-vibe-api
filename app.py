import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify
from sqlalchemy import text
from werkzeug.exceptions import HTTPException

from extensions import db


def make_response(code: int, message: str, data=None):
    return jsonify({"code": code, "message": message, "data": data}), code


def create_app() -> Flask:
    # 讀取環境變數 DATABASE_URL；程序環境裡還沒有時，改讀專案目錄的 .env
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        env_path = Path(__file__).resolve().parent / ".env"
        if env_path.exists():
            load_dotenv(dotenv_path=env_path)
        database_url = os.environ.get("DATABASE_URL")

    if not database_url or not database_url.strip():
        raise RuntimeError(
            "未設定環境變數 DATABASE_URL，無法啟動應用程式。請在環境變數或 .env 檔案中設定 DATABASE_URL。"
        )

    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)

    app = Flask(__name__)
    app.json.ensure_ascii = False

    app.config["SQLALCHEMY_DATABASE_URI"] = database_url
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"connect_args": {"connect_timeout": 3}}

    db.init_app(app)

    @app.errorhandler(404)
    def handle_not_found(error):
        return make_response(404, "找不到這一筆", None)

    @app.errorhandler(HTTPException)
    def handle_http_error(error):
        if error.code == 404:
            return make_response(404, "找不到這一筆", None)
        return make_response(error.code, error.description, None)

    @app.get("/api/health")
    def health_check():
        return make_response(
            200,
            "伺服器運作正常，測試正常可以進行其他任務。",
            {
                "status": "ok",
                "time": datetime.now(timezone.utc).isoformat(),
            },
        )

    @app.get("/api/health/db")
    def health_check_db():
        try:
            db.session.execute(text("SELECT 1"))
            return make_response(200, "資料庫連線正常", {"status": "ok"})
        except Exception as e:
            app.logger.error("資料庫連線檢查失敗: %s", e)
            return make_response(500, "資料庫暫時連不上", None)

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)