import hmac
import os
import secrets
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

import click
import psycopg
from dotenv import load_dotenv
from flask import Flask, g, jsonify, render_template, request, session

from buddy import db, knowledge, llm, service


ROOT = Path(__file__).parent


def create_app(test_config=None):
    load_dotenv(ROOT / ".env")
    app = Flask(__name__)
    Path(app.instance_path).mkdir(exist_ok=True)
    secret_path = Path(app.instance_path) / "session.key"
    try:
        descriptor = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(descriptor, "w") as key_file:
            key_file.write(secrets.token_hex(32))
    app.config.update(
        SECRET_KEY=os.getenv("SECRET_KEY") or secret_path.read_text(),
        DATABASE_URL=os.getenv("DATABASE_URL", "postgresql://buddy:buddy-local-demo@localhost:55432/buddy"),
        LLM_BASE_URL=os.getenv("LLM_BASE_URL", "https://api.openai.com/v1"),
        LLM_MODEL=os.getenv("LLM_MODEL", "gpt-6-luna"),
        LLM_API_KEY=os.getenv("LLM_API_KEY", ""),
        LLM_ENABLED=os.getenv("LLM_ENABLED", "true").lower() == "true",
        LLM_TIMEOUT_SECONDS=float(os.getenv("LLM_TIMEOUT_SECONDS", "15")),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "false").lower() == "true",
        PERMANENT_SESSION_LIFETIME=timedelta(days=7),
        MAX_CONTENT_LENGTH=16384,
    )
    if test_config:
        app.config.update(test_config)
    app.extensions["knowledge"] = knowledge.load_documents(ROOT / "knowledge")
    with app.app_context():
        db.initialize()

    def error(code, message, status):
        return jsonify(error={"code": code, "message": message}), status

    @app.before_request
    def authenticate():
        if request.path != "/" and not request.path.startswith("/api/"):
            return None
        g.browser_session = db.get_session(session.get("sid", ""))
        if not g.browser_session:
            if request.path != "/":
                return error("session_expired", "Сессия закончилась. Перезагрузи страницу.", 401)
            g.browser_session = db.new_session("intern-catalog")
            session.clear()
            session["sid"] = g.browser_session["id"]
            session.permanent = True
        user_id = g.browser_session["user_id"]
        g.actor = {
            **service.PROFILES[user_id],
            "id": user_id,
            "confirmed_resource": g.browser_session.get("confirmed_resource"),
        }
        if request.method == "POST":
            origin = request.headers.get("Origin")
            expected = urlsplit(request.host_url)
            if origin and urlsplit(origin)[:2] != expected[:2]:
                return error("invalid_origin", "Обнови страницу и повтори действие.", 403)
            token = request.headers.get("X-CSRF-Token", "")
            if not hmac.compare_digest(token, g.browser_session["csrf_token"]):
                return error("invalid_csrf", "Обнови страницу и повтори действие.", 403)
            if not isinstance(request.get_json(silent=True), dict):
                return error("invalid_json", "Ожидается JSON-объект.", 400)

    @app.after_request
    def response_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        return response

    @app.errorhandler(psycopg.Error)
    def database_error(exc):
        # Do not log DSNs, submitted secrets or provider response bodies.
        app.logger.error("Database operation failed (%s)", type(exc).__name__)
        if request.path == "/api/requests" and request.method == "POST":
            return error("result_unknown", "Результат отправки пока неизвестен. Проверь заявку.", 503)
        return error("status_unavailable", "Не удалось получить данные. Повтори проверку.", 503)

    @app.errorhandler(413)
    def too_large(exc):
        return error("too_large", "Сообщение слишком длинное.", 413)

    def current_state():
        try:
            return service.get_access_state(g.actor)
        except (service.ServiceUnavailable, psycopg.Error):
            return {"status": "unknown", "request": None}

    def resource_known():
        return g.browser_session["confirmed_resource"] or g.actor["resource_id"]

    @app.get("/")
    def index():
        return render_template("index.html", csrf_token=g.browser_session["csrf_token"])

    @app.get("/api/context")
    def context():
        state = current_state()
        return jsonify(
            profile=g.actor, task=service.task_for(g.actor),
            card=service.card_for(state), history=db.history(g.actor["id"]),
            resource_known=bool(resource_known()),
            profiles=[{"id": key, "name": value["name"]} for key, value in service.PROFILES.items()],
            model={"configured": bool(app.config["LLM_ENABLED"] and app.config["LLM_API_KEY"]), "name": app.config["LLM_MODEL"]},
        )

    @app.post("/api/session")
    def switch_profile():
        profile_id = request.json.get("profile_id")
        if not isinstance(profile_id, str) or profile_id not in service.PROFILES:
            return error("invalid_profile", "Выбери тестовый профиль из списка.", 400)
        with db.connect() as conn:
            conn.execute("DELETE FROM browser_sessions WHERE id = %s", (g.browser_session["id"],))
        browser_session = db.new_session(profile_id)
        session.clear()
        session["sid"] = browser_session["id"]
        session.permanent = True
        return jsonify(ok=True)

    @app.post("/api/chat")
    def chat():
        message = request.json.get("message")
        if not isinstance(message, str) or not 1 <= len(message.strip()) <= 2000:
            return error("invalid_message", "Введи сообщение от 1 до 2000 символов.", 400)
        message = message.strip()
        state = current_state()
        action = {"type": "none", "payload": {}}
        sources = []
        generation = {"mode": "server", "notice": ""}
        pending = g.browser_session["pending_question"]
        effective_message = f"{pending}\n{message}" if pending else message
        document = None

        if not service.permitted(g.actor):
            answer = "Инструкции и доступы этой команды ограничены. Обратись к наставнику своей команды."
        elif knowledge.another_resource(message) or (pending and "catalog dev" not in message.lower()):
            db.set_clarification(g.browser_session["id"], message, None, action="prepare_request")
            answer = "Уточни название стенда для этой задачи. В демонстрации доступен Catalog Dev."
            action["type"] = "clarify"
        elif not resource_known() and "catalog dev" not in message.lower():
            db.set_clarification(g.browser_session["id"], message, None, action="prepare_request")
            answer = "На каком стенде возникает ошибка? Уточни название в своей задаче."
            action["type"] = "clarify"
        else:
            db.set_clarification(g.browser_session["id"], None, "catalog-dev", action=None)
            g.actor["confirmed_resource"] = "catalog-dev"
            document = knowledge.select_document(app.extensions["knowledge"], g.actor, effective_message, state["status"])
            if not document:
                answer = "В доступных инструкциях нет ответа на этот вопрос. Уточни его у наставника. Я могу помочь с доступом к Catalog Dev и проверкой исправления."
            else:
                sources = [{"id": document["id"], "title": document["title"], "excerpt": document["content"]}]
                answer, action = answer_plan(document, state, g.actor)
                answer, generation = llm.generate_answer({
                    "question": effective_message, "task": service.task_for(g.actor),
                    "access_state": state, "instruction": document["content"],
                    "server_goal": answer,
                }, answer)
        response = {"answer": answer, "sources": sources, "card": service.card_for(state), "action": action, "generation": generation}
        db.save_exchange(g.actor["id"], message, response)
        return jsonify(response)

    @app.post("/api/requests")
    def create_request():
        data = request.json
        if data.get("resource_id") != "catalog-dev" or data.get("access_level") != "read" or not service.permitted(g.actor):
            return error("forbidden", "Этот доступ недоступен для текущего профиля.", 403)
        if g.browser_session["pending_question"] or not resource_known():
            return error("resource_unknown", "Сначала уточни стенд в диалоге.", 400)
        try:
            state, created = service.create_access_request(g.actor, data["resource_id"], data["access_level"])
        except PermissionError:
            return error("forbidden", "Этот доступ недоступен для текущего профиля.", 403)
        except service.ServiceUnavailable:
            return error("result_unknown", "Результат отправки пока неизвестен. Проверь заявку.", 503)
        return jsonify(card=service.card_for(state)), 201 if created else 200

    @app.cli.command("approve-request")
    @click.argument("number")
    def approve_request(number):
        """Imitate the owner's approval, e.g. approve-request ACC-001."""
        try:
            request_id = int(number.removeprefix("ACC-"))
        except ValueError:
            raise click.ClickException("Укажи номер заявки, например ACC-001.")
        with db.connect() as conn:
            record = conn.execute("""
                WITH approved AS (
                    UPDATE access_requests SET status = 'granted', updated_at = now() WHERE id = %s
                    RETURNING user_id, resource_id, access_level
                ), grant_access AS (
                    INSERT INTO user_access (user_id, resource_id, access_level, status)
                    SELECT user_id, resource_id, access_level, 'granted' FROM approved
                    ON CONFLICT (user_id, resource_id, access_level) DO UPDATE SET status = EXCLUDED.status
                )
                SELECT user_id FROM approved
            """, (request_id,)).fetchone()
        if not record:
            raise click.ClickException("Заявка не найдена.")
        click.echo(f"ACC-{request_id:03d}: доступ выдан. Обнови статус в браузере.")

    @app.cli.command("reset-demo")
    @click.option("--profile", type=click.Choice(list(service.PROFILES)), default="intern-catalog")
    @click.confirmation_option(prompt="Удалить заявки и историю выбранного тестового профиля?")
    def reset_demo(profile):
        """Reset one synthetic profile for a new demo run."""
        with db.connect() as conn:
            conn.execute("DELETE FROM messages WHERE user_id = %s", (profile,))
            conn.execute("DELETE FROM access_requests WHERE user_id = %s", (profile,))
            if profile not in ("intern-pregranted", "intern-catalog-granted"):
                conn.execute("DELETE FROM user_access WHERE user_id = %s", (profile,))
            conn.execute("UPDATE browser_sessions SET pending_question = NULL, confirmed_resource = NULL, pending_action = NULL WHERE user_id = %s", (profile,))
            if profile in ("intern-pending", "intern-catalog-pending"):
                conn.execute("INSERT INTO access_requests (user_id, resource_id, access_level, status) VALUES (%s, 'catalog-dev', 'read', 'pending') ON CONFLICT DO NOTHING", (profile,))
        click.echo("Тестовый профиль сброшен.")

    return app


def answer_plan(document, state, actor):
    action = {"type": "none", "payload": {}}
    if state["status"] == "unknown":
        answer = (
            "Заявка согласована, но право на чтение Catalog Dev пока не подтверждено. Проверь статус ещё раз."
            if state["request"] else
            "Для проверки исправления нужен доступ на чтение к Catalog Dev. Сейчас состояние доступа и заявки неизвестно. Повтори проверку."
        )
        action["type"] = "retry_status"
    elif state["status"] == "pending":
        answer = "Заявка уже ожидает согласования у владельца стенда. Её номер и актуальный статус указаны в карточке."
        action["type"] = "show_request"
    elif state["status"] == "missing":
        answer = "Для первой задачи нужен доступ на чтение к Catalog Dev. Сервис показывает, что доступа пока нет. Подготовь заявку владельцу стенда, проверь поля и подтверди отправку."
        action = {"type": "prepare_request", "payload": service.REQUEST_FIELDS}
    elif document["id"] == "catalog-dev-403":
        answer = "Право на чтение подтверждено. Сверь адрес стенда со ссылкой в задаче и рабочий профиль. Если 403 повторится, отправь черновик владельцу стенда или наставнику."
        task_url = service.task_for(actor)["url"]
        request_info = f" Заявка {state['request']['number']}." if state["request"] else ""
        draft = (
            f"Привет! По задаче {task_url} на Catalog Dev получаю 403 при [действие] в [время]. "
            f"Право на чтение подтверждено.{request_info} Помоги проверить доступ для моего рабочего профиля."
        )
        action = {"type": "draft_message", "payload": {"text": draft}}
    else:
        task = service.task_for(actor)
        answer = f"Открой ссылку на стенд из задачи и сверь сборку {task['version']}. Ожидаемый результат — {task['expected_result']} Если ссылка или версия отличаются, уточни их у наставника."
        action["type"] = "show_verification"
    return answer, action
