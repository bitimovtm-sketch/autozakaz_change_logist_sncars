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

# Маппинг "Вид транзита": ID в смарт-процессе → допустимые ID в сделке
TRANSIT_MAP = {
    1062: [982],       # Уссурийск → Уссурийск
    1064: [990, 980],  # Уссурийск-МСК, Хоргос → Уссурийск-Москва, Хоргос
}


def b24(method, params):
    url = f"{BITRIX_WEBHOOK_URL.rstrip('/')}/{method}.json"
    resp = requests.post(url, json=params, timeout=30)
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


def get_all_deals(stage_id, allowed_transit_ids):
    """
    Получить ВСЕ сделки на стадии с нужным видом транзита.
    Битрикс отдаёт максимум 50 за раз — обходим все страницы.
    При 1000 сделках будет ~20 запросов.
    """
    deals = []
    start = 0
    while True:
        f = {"CATEGORY_ID": DEAL_CATEGORY_ID, "STAGE_ID": stage_id}
        if allowed_transit_ids:
            f[DEAL_TRANSIT_FIELD] = allowed_transit_ids
        try:
            result = b24("crm.deal.list", {
                "filter": f,
                "select": ["ID", "TITLE"],
                "start": start,
            })
        except Exception as e:
            log.error("Ошибка получения сделок (start=%s): %s", start, e)
            break

        batch = result if isinstance(result, list) else []
        deals.extend(batch)
        log.info("Страница start=%s: получено %s, всего накоплено %s", start, len(batch), len(deals))

        if len(batch) < 50:
            # Последняя страница — больше нет
            break
        start += 50

    return deals


def update_deal_employee(deal_id, user_id):
    try:
        b24("crm.deal.update", {"id": deal_id, "fields": {DEAL_EMPLOYEE_FIELD: user_id}})
        log.info("Сделка %s: сотрудник → user %s", deal_id, user_id)
        return True
    except Exception as e:
        log.error("Ошибка обновления сделки %s: %s", deal_id, e)
        return False


def get_active_tasks_for_deal(deal_id):
    try:
        result = b24("tasks.task.list", {
            "filter": {"CRM_ENTITY_TYPE": "DEAL", "CRM_ENTITY_ID": deal_id, "STATUS": [2, 3]},
            "select": ["ID", "STATUS"],
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
    """
    Извлечь ID элемента смарт-процесса из payload.

    Робот бизнес-процесса Битрикс24 присылает форму где ключи выглядят как:
      document_id[0] = crm
      document_id[1] = Bitrix\\Crm\\...\\Dynamic
      document_id[2] = DYNAMIC_1090_12   ← нам нужен этот, число 12

    Flask parses "document_id[2]" как ImmutableMultiDict с ключом "document_id[2]"
    или иногда как вложенный dict document_id → {2: ...}
    Проверяем оба варианта.
    """
    log.info("Все ключи payload: %s", list(payload.keys()))

    # Вариант А: ключ пришёл как строка "document_id[2]"
    doc = payload.get("document_id[2]")
    if doc:
        log.info("document_id[2] (вариант А): %s", doc)
        parts = str(doc).rsplit("_", 1)
        if len(parts) == 2 and parts[1].isdigit():
            return int(parts[1])

    # Вариант Б: Flask распарсил как вложенный — ищем любой ключ содержащий document_id
    for key, val in payload.items():
        if "document_id" in key and "2" in key:
            log.info("document_id ключ '%s' = '%s'", key, val)
            parts = str(val).rsplit("_", 1)
            if len(parts) == 2 and parts[1].isdigit():
                return int(parts[1])

    # Вариант В: стандартный исходящий вебхук
    raw = payload.get("data[FIELDS][ID]") or payload.get("data[FIELDS][id]") or payload.get("ID")
    if raw and str(raw).isdigit():
        return int(raw)

    # Вариант Г: ID в строке запроса (?ID=12)
    raw = request.args.get("ID") or request.args.get("id")
    if raw and str(raw).isdigit():
        return int(raw)

    return None


@app.route("/webhook", methods=["POST"])
def webhook():
    # Читаем и форму, и JSON
    payload = {}
    if request.form:
        payload = request.form.to_dict(flat=True)
    elif request.json:
        payload = request.json

    log.info("Входящий вебхук payload: %s", payload)

    item_id = extract_item_id(payload)
    if not item_id:
        log.warning("ID смарт-процесса не найден. Полный payload: %s", payload)
        return jsonify({"status": "skipped", "reason": "no entity id"}), 200

    log.info("ID смарт-процесса: %s", item_id)

    # 1. Получаем смарт-процесс
    item = get_smart_item(item_id)
    if not item:
        return jsonify({"status": "error", "reason": "smart item not found"}), 200

    log.info("Смарт-процесс данные: %s", item)

    # 2. Читаем поля
    employee_id_raw   = item.get(SMART_EMPLOYEE_FIELD)
    deal_stage_id     = item.get(SMART_STAGE_FIELD)
    smart_transit_raw = item.get("ufCrm34_1779184434")

    log.info("Сотрудник=%s, Стадия=%s, Транзит=%s", employee_id_raw, deal_stage_id, smart_transit_raw)

    if not employee_id_raw:
        return jsonify({"status": "skipped", "reason": "employee field is empty"}), 200
    if not deal_stage_id:
        return jsonify({"status": "skipped", "reason": "stage field is empty"}), 200

    employee_id = int(employee_id_raw[0] if isinstance(employee_id_raw, list) else employee_id_raw)

    # 3. Фильтр по виду транзита
    smart_transit_id    = int(smart_transit_raw) if smart_transit_raw else None
    allowed_transit_ids = TRANSIT_MAP.get(smart_transit_id, [])
    log.info("Фильтр транзит: smart=%s → deal=%s", smart_transit_id, allowed_transit_ids)

    # 4. Получаем ВСЕ сделки (с пагинацией, до 1000+)
    deals = get_all_deals(deal_stage_id, allowed_transit_ids)
    log.info("Итого сделок для обработки: %s", len(deals))

    if not deals:
        return jsonify({"status": "ok", "deals_found": 0}), 200

    # 5. Обрабатываем каждую сделку
    total_deals_ok = 0
    total_tasks_ok = 0

    for deal in deals:
        deal_id = int(deal["id"])

        if update_deal_employee(deal_id, employee_id):
            total_deals_ok += 1

        tasks = get_active_tasks_for_deal(deal_id)
        for task in tasks:
            if update_task_members(int(task["id"]), employee_id):
                total_tasks_ok += 1

    log.info("Готово. Сделок обновлено: %s/%s, задач обновлено: %s",
             total_deals_ok, len(deals), total_tasks_ok)

    return jsonify({
        "status": "ok",
        "deals_total": len(deals),
        "deals_updated": total_deals_ok,
        "tasks_updated": total_tasks_ok,
    }), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "alive"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
