import os
import logging
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# НАСТРОЙКИ — задайте через переменные окружения
# ──────────────────────────────────────────────
BITRIX_WEBHOOK_URL   = os.environ["BITRIX_WEBHOOK_URL"]    # https://портал.bitrix24.ru/rest/1/токен
SMART_PROCESS_ID     = int(os.environ["SMART_PROCESS_ID"]) # entityTypeId смарт-процесса
SMART_STAGE_FIELD    = os.environ["SMART_STAGE_FIELD"]     # поле в смарт-процессе с ID стадии сделки, напр. "UF_CRM_DEAL_STAGE"
SMART_EMPLOYEE_FIELD = os.environ["SMART_EMPLOYEE_FIELD"]  # поле в смарт-процессе с ID сотрудника, напр. "UF_CRM_EMPLOYEE"
DEAL_CATEGORY_ID     = int(os.environ["DEAL_CATEGORY_ID"]) # ID воронки сделок
DEAL_EMPLOYEE_FIELD  = os.environ["DEAL_EMPLOYEE_FIELD"]   # поле в Сделке для записи сотрудника, напр. "UF_CRM_1_EMPLOYEE"
# ──────────────────────────────────────────────


def b24(method: str, params: dict) -> dict:
    """Вызов REST API Битрикс24."""
    url = f"{BITRIX_WEBHOOK_URL.rstrip('/')}/{method}.json"
    resp = requests.post(url, json=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"Bitrix24 API error [{method}]: {data}")
    return data.get("result", data)


def get_smart_item(item_id: int) -> dict | None:
    """Получить элемент смарт-процесса по ID."""
    try:
        result = b24("crm.item.get", {"entityTypeId": SMART_PROCESS_ID, "id": item_id})
        return result.get("item")
    except Exception as e:
        log.error("Ошибка получения смарт-процесса %s: %s", item_id, e)
        return None


def get_deals_by_stage(stage_id: str) -> list[dict]:
    """
    Найти все сделки в нужной воронке на указанной стадии.
    Обходит пагинацию (start=0, 50, 100, ...).
    """
    deals = []
    start = 0
    while True:
        try:
            result = b24("crm.deal.list", {
                "filter": {
                    "CATEGORY_ID": DEAL_CATEGORY_ID,
                    "STAGE_ID":    stage_id,
                },
                "select": ["ID", "TITLE", "STAGE_ID"],
                "start": start,
            })
        except Exception as e:
            log.error("Ошибка получения сделок (start=%s): %s", start, e)
            break

        batch = result if isinstance(result, list) else []
        deals.extend(batch)
        log.info("Получено сделок: %s (всего накоплено: %s)", len(batch), len(deals))

        if len(batch) < 50:
            break
        start += 50

    return deals


def update_deal_employee(deal_id: int, user_id: int) -> bool:
    """Записать сотрудника в пользовательское поле Сделки."""
    try:
        b24("crm.deal.update", {
            "id": deal_id,
            "fields": {DEAL_EMPLOYEE_FIELD: user_id},
        })
        log.info("Сделка %s: %s → user %s", deal_id, DEAL_EMPLOYEE_FIELD, user_id)
        return True
    except Exception as e:
        log.error("Ошибка обновления сделки %s: %s", deal_id, e)
        return False


def get_active_tasks_for_deal(deal_id: int) -> list[dict]:
    """Найти активные задачи, привязанные к Сделке."""
    try:
        result = b24("tasks.task.list", {
            "filter": {
                "CRM_ENTITY_TYPE": "DEAL",
                "CRM_ENTITY_ID":   deal_id,
                "STATUS": [2, 3],  # 2=выполняется, 3=ждёт контрольного срока
            },
            "select": ["ID", "TITLE", "CREATED_BY", "RESPONSIBLE_ID", "STATUS"],
        })
        return result.get("tasks", [])
    except Exception as e:
        log.error("Ошибка получения задач для сделки %s: %s", deal_id, e)
        return []


def update_task_members(task_id: int, user_id: int) -> bool:
    """Обновить постановщика и ответственного в задаче."""
    try:
        b24("tasks.task.update", {
            "taskId": task_id,
            "fields": {
                "CREATED_BY":     user_id,  # постановщик
                "RESPONSIBLE_ID": user_id,  # ответственный
            },
        })
        log.info("Задача %s: постановщик и ответственный → user %s", task_id, user_id)
        return True
    except Exception as e:
        log.error("Ошибка обновления задачи %s: %s", task_id, e)
        return False


# ──────────────────────────────────────────────
# Основной эндпоинт вебхука
# ──────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    payload = request.form.to_dict() or request.json or {}
    log.info("Входящий вебхук: %s", payload)

    # Битрикс24 передаёт ID в формате data[FIELDS][ID]
    item_id_raw = (
        payload.get("data[FIELDS][ID]")
        or payload.get("data[FIELDS][id]")
        or payload.get("ID")
    )

    if not item_id_raw:
        log.warning("ID смарт-процесса не найден в payload, пропускаем")
        return jsonify({"status": "skipped", "reason": "no entity id"}), 200

    item_id = int(item_id_raw)

    # 1. Получаем смарт-процесс
    item = get_smart_item(item_id)
    if not item:
        return jsonify({"status": "error", "reason": "smart item not found"}), 200

    # 2. Читаем из смарт-процесса: сотрудника и стадию сделок
    employee_id_raw = item.get(SMART_EMPLOYEE_FIELD)
    deal_stage_id   = item.get(SMART_STAGE_FIELD)

    log.info(
        "Смарт-процесс %s: сотрудник=%s, стадия сделок=%s",
        item_id, employee_id_raw, deal_stage_id,
    )

    if not employee_id_raw:
        log.warning("Поле сотрудника (%s) пустое — пропускаем", SMART_EMPLOYEE_FIELD)
        return jsonify({"status": "skipped", "reason": "employee field is empty"}), 200

    if not deal_stage_id:
        log.warning("Поле стадии (%s) пустое — пропускаем", SMART_STAGE_FIELD)
        return jsonify({"status": "skipped", "reason": "stage field is empty"}), 200

    # Поле типа «Сотрудник» возвращает одиночное значение или список
    if isinstance(employee_id_raw, list):
        employee_id = int(employee_id_raw[0])
    else:
        employee_id = int(employee_id_raw)

    # 3. Находим все сделки на этой стадии в нужной воронке
    deals = get_deals_by_stage(deal_stage_id)
    log.info("Найдено сделок для обработки: %s", len(deals))

    if not deals:
        return jsonify({"status": "ok", "deals_found": 0, "message": "no deals on this stage"}), 200

    results = {"deals_processed": [], "employee_id": employee_id, "stage_id": deal_stage_id}

    for deal in deals:
        deal_id = int(deal["id"])
        deal_result = {"deal_id": deal_id, "deal_updated": False, "tasks_updated": []}

        # 4. Меняем сотрудника в поле Сделки
        deal_result["deal_updated"] = update_deal_employee(deal_id, employee_id)

        # 5. Находим активные задачи сделки и меняем постановщика/ответственного
        tasks = get_active_tasks_for_deal(deal_id)
        log.info("Сделка %s: активных задач %s", deal_id, len(tasks))

        for task in tasks:
            task_id = int(task["id"])
            ok = update_task_members(task_id, employee_id)
            deal_result["tasks_updated"].append({"task_id": task_id, "success": ok})

        results["deals_processed"].append(deal_result)

    return jsonify({"status": "ok", "results": results}), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "alive"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
