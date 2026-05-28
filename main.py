import os
import logging
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

BITRIX_WEBHOOK_URL   = os.environ["BITRIX_WEBHOOK_URL"]
SMART_PROCESS_ID     = int(os.environ["SMART_PROCESS_ID"])
SMART_EMPLOYEE_FIELD = os.environ["SMART_EMPLOYEE_FIELD"]
SMART_STAGE_FIELD    = os.environ["SMART_STAGE_FIELD"]
DEAL_CATEGORY_ID     = int(os.environ["DEAL_CATEGORY_ID"])
DEAL_EMPLOYEE_FIELD  = os.environ["DEAL_EMPLOYEE_FIELD"]
DEAL_TRANSIT_FIELD   = os.environ["DEAL_TRANSIT_FIELD"]

# Маппинг "Вид транзита": смарт-процесс → допустимые значения в сделке
TRANSIT_MAP = {
    1062: [982],       # Уссурийск → Уссурийск
    1064: [990, 980],  # Уссурийск-МСК, Хоргос → Уссурийск-Москва, Хоргос
}


def b24(method, params):
    url = f"{BITRIX_WEBHOOK_URL.rstrip('/')}/{method}.json"
    resp = requests.post(url, json=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"Bitrix24 API error [{method}]: {data}")
    return data.get("result", data)


def get_smart_item(item_id):
    try:
        result = b24("crm.item.get", {"entityTypeId": SMART_PROCESS_ID, "id": item_id})
        return result.get("item")
    except Exception as e:
        log.error("Ошибка получения смарт-процесса %s: %s", item_id, e)
        return None


def get_deals_by_stage_and_transit(stage_id, allowed_transit_ids):
    deals = []
    start = 0
    while True:
        try:
            f = {"CATEGORY_ID": DEAL_CATEGORY_ID, "STAGE_ID": stage_id}
            if allowed_transit_ids:
                f[DEAL_TRANSIT_FIELD] = allowed_transit_ids
            result = b24("crm.deal.list", {
                "filter": f,
                "select": ["ID", "TITLE", "STAGE_ID", DEAL_TRANSIT_FIELD],
                "start": start,
            })
        except Exception as e:
            log.error("Ошибка получения сделок (start=%s): %s", start, e)
            break
        batch = result if isinstance(result, list) else []
        deals.extend(batch)
        log.info("Получено сделок: %s (всего: %s)", len(batch), len(deals))
        if len(batch) < 50:
            break
        start += 50
    return deals


def update_deal_employee(deal_id, user_id):
    try:
        b24("crm.deal.update", {"id": deal_id, "fields": {DEAL_EMPLOYEE_FIELD: user_id}})
        log.info("Сделка %s: %s → user %s", deal_id, DEAL_EMPLOYEE_FIELD, user_id)
        return True
    except Exception as e:
        log.error("Ошибка обновления сделки %s: %s", deal_id, e)
        return False


def get_active_tasks_for_deal(deal_id):
    try:
        result = b24("tasks.task.list", {
            "filter": {"CRM_ENTITY_TYPE": "DEAL", "CRM_ENTITY_ID": deal_id, "STATUS": [2, 3]},
            "select": ["ID", "TITLE", "CREATED_BY", "RESPONSIBLE_ID", "STATUS"],
        })
        return result.get("tasks", [])
    except Exception as e:
        log.error("Ошибка получения задач для сделки %s: %s", deal_id, e)
        return []


def update_task_members(task_id, user_id):
    try:
        b24("tasks.task.update", {
            "taskId": task_id,
            "fields": {"CREATED_BY": user_id, "RESPONSIBLE_ID": user_id},
        })
        log.info("Задача %s: постановщик и ответственный → user %s", task_id, user_id)
        return True
    except Exception as e:
        log.error("Ошибка обновления задачи %s: %s", task_id, e)
        return False


def extract_item_id(payload):
    """Извлечь ID элемента смарт-процесса из payload любого формата."""

    # Формат 1: стандартный исходящий вебхук
    raw = payload.get("data[FIELDS][ID]") or payload.get("data[FIELDS][id]") or payload.get("ID")
    if raw:
        return int(raw)

    # Формат 2: робот бизнес-процесса → document_id[2] = 'DYNAMIC_1090_12'
    doc = payload.get("document_id[2]") or payload.get("document_id%5B2%5D")
    if doc:
        parts = str(doc).rsplit("_", 1)
        if len(parts) == 2 and parts[1].isdigit():
            log.info("ID извлечён из document_id[2]: %s → %s", doc, parts[1])
            return int(parts[1])

    # Формат 3: ID в строке запроса (?ID=12)
    raw = request.args.get("ID") or request.args.get("id")
    if raw and str(raw).isdigit():
        return int(raw)

    return None


@app.route("/webhook", methods=["POST"])
def webhook():
    payload = request.form.to_dict() or request.json or {}
    log.info("Входящий вебхук: %s", payload)

    item_id = extract_item_id(payload)
    if not item_id:
        log.warning("ID смарт-процесса не найден в payload")
        return jsonify({"status": "skipped", "reason": "no entity id"}), 200

    log.info("ID смарт-процесса: %s", item_id)

    # 1. Получаем смарт-процесс
    item = get_smart_item(item_id)
    if not item:
        return jsonify({"status": "error", "reason": "smart item not found"}), 200

    # 2. Читаем поля из смарт-процесса
    employee_id_raw   = item.get(SMART_EMPLOYEE_FIELD)
    deal_stage_id     = item.get(SMART_STAGE_FIELD)
    smart_transit_raw = item.get("ufCrm34_1779184434")

    log.info("Смарт-процесс %s: сотрудник=%s, стадия=%s, транзит=%s",
             item_id, employee_id_raw, deal_stage_id, smart_transit_raw)

    if not employee_id_raw:
        return jsonify({"status": "skipped", "reason": "employee field is empty"}), 200
    if not deal_stage_id:
        return jsonify({"status": "skipped", "reason": "stage field is empty"}), 200

    employee_id = int(employee_id_raw[0] if isinstance(employee_id_raw, list) else employee_id_raw)

    # 3. Определяем фильтр по виду транзита
    smart_transit_id = int(smart_transit_raw) if smart_transit_raw else None
    allowed_transit_ids = TRANSIT_MAP.get(smart_transit_id, [])
    log.info("Фильтр по виду транзита: %s", allowed_transit_ids)

    # 4. Ищем сделки
    deals = get_deals_by_stage_and_transit(deal_stage_id, allowed_transit_ids)
    log.info("Найдено сделок для обработки: %s", len(deals))

    if not deals:
        return jsonify({"status": "ok", "deals_found": 0}), 200

    results = {
        "employee_id": employee_id,
        "stage_id": deal_stage_id,
        "transit_filter": allowed_transit_ids,
        "deals_processed": [],
    }

    for deal in deals:
        deal_id = int(deal["id"])
        deal_result = {"deal_id": deal_id, "deal_updated": False, "tasks_updated": []}

        # 5. Меняем сотрудника в сделке
        deal_result["deal_updated"] = update_deal_employee(deal_id, employee_id)

        # 6. Меняем постановщика/ответственного в активных задачах
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
