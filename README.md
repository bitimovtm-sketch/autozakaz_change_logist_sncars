# Битрикс24 → Railway Webhook

Скрипт ловит вебхук от смарт-процесса, проверяет воронку и стадию,
затем меняет сотрудника в поле сделки и в активных задачах (постановщик + ответственный).

---

## Структура файлов

```
bitrix_webhook/
├── main.py            # основной код
├── requirements.txt   # зависимости Python
├── Procfile           # команда запуска для Railway
├── .env.example       # шаблон переменных окружения
└── .gitignore
```

---

## Шаг 1 — Найти нужные ID в Битрикс24

### BITRIX_WEBHOOK_URL
Настройки → Разработчикам → Другое → Входящий вебхук → Создать
Дать права: `crm`, `task`
Скопировать URL вида `https://портал.bitrix24.ru/rest/1/токен`

### SMART_PROCESS_ID (entityTypeId)
CRM → Смарт-процессы → открыть нужный → посмотреть URL
Ищите параметр `entityTypeId=XXX`

### TARGET_CATEGORY_ID
При просмотре воронки смарт-процесса — в URL параметр `categoryId=XX`

### TARGET_STAGE_ID
Вызвать через браузер или Postman:
```
GET https://портал.bitrix24.ru/rest/1/токен/crm.status.list?filter[ENTITY_ID]=DYNAMIC_{SMART_PROCESS_ID}_STAGE_{CATEGORY_ID}
```
Скопировать поле `STATUS_ID` нужной стадии.

### DEAL_EMPLOYEE_FIELD
CRM → Смарт-процессы → нужный → Поля → найти поле типа "Сотрудник" → скопировать код (UF_CRM_...)

### NEW_EMPLOYEE_ID
Компания → Сотрудники → открыть профиль → ID в URL

---

## Шаг 2 — Деплой на Railway

1. Зарегистрируйтесь на https://railway.app (бесплатно, GitHub-аккаунт)

2. Создайте репозиторий на GitHub и загрузите файлы:
```bash
git init
git add .
git commit -m "initial"
git remote add origin https://github.com/ВАШ_НИК/bitrix-webhook.git
git push -u origin main
```

3. На Railway: New Project → Deploy from GitHub repo → выберите репозиторий

4. В Railway откройте проект → Variables → добавьте все переменные из .env.example:
```
BITRIX_WEBHOOK_URL = https://...
SMART_PROCESS_ID   = 180
TARGET_CATEGORY_ID = 3
TARGET_STAGE_ID    = DT180_3:NEW
DEAL_EMPLOYEE_FIELD= UF_CRM_1_EMPLOYEE
NEW_EMPLOYEE_ID    = 42
```

5. Settings → Networking → Generate Domain
   Скопируйте URL вида `https://ваш-проект.up.railway.app`

---

## Шаг 3 — Настройка вебхука в Битрикс24

1. Настройки → Разработчикам → Другое → **Исходящий вебхук**
2. Тип события: **Изменение элемента смарт-процесса** (OnCrmDynamicItemUpdate)
3. URL обработчика: `https://ваш-проект.up.railway.app/webhook`
4. Сохранить

---

## Проверка работы

Откройте в браузере:
```
https://ваш-проект.up.railway.app/health
```
Должно вернуть: `{"status": "alive"}`

Логи Railway: в интерфейсе проекта → Deployments → View Logs

---

## Как это работает

```
Битрикс24 изменяет смарт-процесс
        ↓
Исходящий вебхук → POST /webhook
        ↓
Скрипт получает ID элемента
        ↓
Запрашивает элемент через crm.item.get
        ↓
Проверяет categoryId и stageId
        ↓ (совпало)
crm.item.update → меняет поле сотрудника
        ↓
tasks.task.list → ищет активные задачи элемента
        ↓
tasks.task.update → меняет постановщика и ответственного
```
