from flask import current_app

from buddy.db import connect


PROFILES = {
    "intern-catalog": {"name": "Стажёр Каталога", "team": "catalog", "team_name": "Каталог", "role": "intern", "resource_id": "catalog-dev"},
    "intern-pregranted": {"name": "Стажёр · доступ уже есть", "team": "catalog", "team_name": "Каталог", "role": "intern", "resource_id": "catalog-dev"},
    "intern-unknown": {"name": "Стажёр · стенд неизвестен", "team": "catalog", "team_name": "Каталог", "role": "intern", "resource_id": None},
    "intern-other": {"name": "Стажёр другой команды", "team": "other", "team_name": "Другая команда", "role": "intern", "resource_id": None},
}
REQUEST_FIELDS = {
    "resource_id": "catalog-dev", "resource_name": "Catalog Dev", "access_level": "read",
    "access_name": "Чтение", "reason": "Проверка исправления по первой задаче",
}


class ServiceUnavailable(Exception):
    pass


def permitted(actor):
    return actor["team"] == "catalog" and actor["role"] == "intern"


def task_for(actor):
    return {
        "title": "Проверить исправление на стенде",
        "resource_id": actor["resource_id"],
        "url": "https://tasks.example.test/CAT-101",
        "environment_url": "https://catalog-dev.example.test" if actor["resource_id"] else None,
        "version": "demo-fix-101",
        "expected_result": "После изменения фильтра список объявлений обновляется.",
    }


def get_access_state(actor):
    if not permitted(actor):
        return {"status": "restricted", "request": None}
    if current_app.config.get("READ_FAILURE"):
        raise ServiceUnavailable()
    with connect() as conn:
        record = conn.execute(
            """
            SELECT req.id, req.status, COALESCE(access.status = 'granted', FALSE) AS has_access
            FROM (VALUES (%s, 'catalog-dev', 'read')) AS target(user_id, resource_id, access_level)
            LEFT JOIN access_requests req USING (user_id, resource_id, access_level)
            LEFT JOIN user_access access USING (user_id, resource_id, access_level)
            """,
            (actor["id"],),
        ).fetchone()
    request = None
    if record["id"] is not None:
        request = {"number": f"ACC-{record['id']:03d}", "status": record["status"]}
    status = "missing"
    if record["has_access"]:
        status = "granted"
    elif record["status"] == "pending":
        status = "pending"
    elif record["status"] == "granted":
        status = "unknown"
    return {"status": status, "request": request}


def create_access_request(actor, resource, access_level):
    if not permitted(actor) or resource != "catalog-dev" or access_level != "read":
        raise PermissionError()
    if current_app.config.get("REQUEST_FAILURE_MODE") == "before":
        raise ServiceUnavailable()
    with connect() as conn:
        inserted = conn.execute(
            """
            INSERT INTO access_requests (user_id, resource_id, access_level)
            SELECT user_id, resource_id, access_level
            FROM (VALUES (%s, %s, %s)) AS target(user_id, resource_id, access_level)
            WHERE NOT EXISTS (
                SELECT 1 FROM user_access access
                WHERE access.status = 'granted'
                  AND (access.user_id, access.resource_id, access.access_level) =
                      (target.user_id, target.resource_id, target.access_level)
            )
            ON CONFLICT (user_id, resource_id, access_level) DO NOTHING RETURNING id
            """,
            (actor["id"], resource, access_level),
        ).fetchone()
    # The service may commit successfully while its response is lost.
    if current_app.config.get("REQUEST_FAILURE_MODE") == "after":
        raise ServiceUnavailable()
    return get_access_state(actor), inserted is not None


def card_for(state):
    descriptions = {
        "missing": ("Заблокировано", "Подготовь заявку на чтение и проверь её поля перед отправкой.", 0),
        "pending": ("Ожидает согласования", "Заявка у владельца стенда. После согласования можно продолжить задачу.", 1),
        "granted": ("Доступ выдан", "Сверь версию на стенде с задачей и проверь ожидаемый результат.", 2),
        "unknown": ("Не удалось проверить статус", "Повтори проверку, чтобы узнать, появилась ли заявка.", None),
        "restricted": ("Доступ ограничен", "Обратись к наставнику своей команды.", None),
    }
    title, next_step, step = descriptions[state["status"]]
    if state["status"] == "unknown" and state["request"]:
        next_step = "Заявка согласована, но право на стенд пока не подтверждено. Проверь статус ещё раз."
    return {**state, "title": title, "next_step": next_step, "step": step}
