from datetime import datetime, timezone

from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException

app = Flask(__name__)
app.json.ensure_ascii = False

tasks = [
    {"id": 1, "title": "完成 API 基礎建置", "description": "建立 Flask 任務管理端點"},
    {"id": 2, "title": "撰寫測試", "description": "驗證 API 回應與狀態碼"},
]


def make_response(status, message, data, http_status):
    return jsonify({"status": status, "message": message, "data": data}), http_status


@app.errorhandler(HTTPException)
def handle_http_error(error):
    return make_response("error", error.description, None, error.code)


@app.get("/api/health")
def health_check():
    return make_response(
        "success",
        "服務運作正常",
        {"status": "running", "timestamp": datetime.now(timezone.utc).isoformat()},
        200,
    )


@app.get("/api/tasks")
def get_tasks():
    return make_response("success", "任務清單取得成功", tasks, 200)


@app.post("/api/tasks")
def create_task():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return make_response("error", "請求內容必須是合法的 JSON 物件", None, 400)

    title = payload.get("title")
    if not isinstance(title, str) or not title.strip():
        return make_response("error", "必填欄位 title 不可為空", None, 400)

    task = {
        "id": max((item["id"] for item in tasks), default=0) + 1,
        "title": title.strip(),
        "description": payload.get("description", ""),
    }
    tasks.append(task)
    return make_response("success", "任務新增成功", task, 201)


@app.get("/api/tasks/<int:task_id>")
def get_task(task_id):
    task = next((item for item in tasks if item["id"] == task_id), None)
    if task is None:
        return make_response("error", "找不到指定任務", None, 404)
    return make_response("success", "任務取得成功", task, 200)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)