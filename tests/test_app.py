import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from app import create_app
from buddy import db, knowledge, llm, service


REQUEST = {"resource_id": "catalog-dev", "access_level": "read"}


def send(client, headers, question="На стенде 403, как попасть туда для первой задачи?"):
    return client.post("/api/chat", json={"message": question}, headers=headers)


def create_request(client, headers):
    return client.post("/api/requests", json=REQUEST, headers=headers)


def count_requests(app):
    with app.app_context(), db.connect() as conn:
        return conn.execute("SELECT count(*) AS count FROM access_requests").fetchone()["count"]


def test_403_draft_submit_reload_and_restart(app, client, headers):
    response = send(client, headers)
    assert response.status_code == 200
    payload = response.json
    assert payload["sources"][0]["id"] == "catalog-dev-access"
    assert payload["action"]["type"] == "prepare_request"
    assert payload["generation"]["mode"] == "fallback"
    assert count_requests(app) == 0
    created = create_request(client, headers)
    assert created.status_code == 201
    assert created.json["card"]["request"]["number"] == "ACC-001"
    client.get("/")
    assert client.get("/api/context").json["card"] == created.json["card"]
    assert len(client.get("/api/context").json["history"]) == 2
    restarted = create_app({"TESTING": True, "DATABASE_URL": app.config["DATABASE_URL"], "LLM_API_KEY": ""})
    new_client = restarted.test_client()
    new_client.set_cookie("session", client.get_cookie("session").value)
    assert new_client.get("/api/context").json["card"] == created.json["card"]


def test_existing_request_and_concurrent_retries(app, client, headers):
    created = create_request(client, headers)
    assert create_request(client, headers).status_code == 200
    payload = send(client, headers, "Я уже просил доступ. Мне ещё раз отправить заявку?").json
    assert payload["action"]["type"] == "show_request"
    assert payload["card"] == created.json["card"]
    with app.app_context(), db.connect() as conn:
        conn.execute("DELETE FROM access_requests")

    def concurrent_create(_):
        with app.app_context():
            return service.create_access_request({**service.PROFILES["intern-catalog"], "id": "intern-catalog"}, "catalog-dev", "read")

    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(concurrent_create, range(6)))
    assert sum(created for _, created in results) == 1
    assert len({state["request"]["number"] for state, _ in results}) == 1
    assert count_requests(app) == 1


def test_granted_and_remaining_403(app, client, headers):
    create_request(client, headers)
    result = app.test_cli_runner().invoke(args=["approve-request", "ACC-001"])
    assert result.exit_code == 0
    verification = send(client, headers, "Доступ дали. Как проверить, что мой фикс работает?").json
    assert verification["sources"][0]["id"] == "catalog-dev-verify"
    assert "demo-fix-101" in verification["answer"]
    stuck = send(client, headers, "В заявке Доступ выдан, но я всё равно получаю 403").json
    assert stuck["sources"][0]["id"] == "catalog-dev-403"
    assert "ACC-001" in stuck["action"]["payload"]["text"]
    assert create_request(client, headers).json["card"]["status"] == "granted"
    assert count_requests(app) == 1


def switch(client, headers, profile):
    assert client.post("/api/session", json={"profile_id": profile}, headers=headers).status_code == 200
    from html.parser import HTMLParser
    class Parser(HTMLParser):
        def handle_starttag(self, tag, attrs):
            data = dict(attrs)
            if data.get("name") == "csrf-token":
                self.token = data["content"]
    parser = Parser()
    parser.feed(client.get("/").text)
    return {"X-CSRF-Token": parser.token}


def test_unknown_resource_clarification(app, client, headers):
    headers = switch(client, headers, "intern-unknown")
    response = send(client, headers, "У меня 403, оформи доступ").json
    assert response["action"]["type"] == "clarify"
    assert response["sources"] == []
    assert create_request(client, headers).status_code == 400
    assert send(client, headers, "Не знаю").json["action"]["type"] == "clarify"
    assert send(client, headers, "Catalog Dev").json["action"]["type"] == "prepare_request"
    assert create_request(client, headers).status_code == 201


def test_pregranted_403_preserves_handoff_and_does_not_create_request(app, client, headers, monkeypatch):
    headers = switch(client, headers, "intern-pregranted")
    monkeypatch.setattr(llm, "generate_answer", lambda *_: ("Проверь профиль.", {"mode": "model"}))
    context = client.get("/api/context").json
    assert context["card"]["status"] == "granted"
    assert context["card"]["request"] is None
    result = send(client, headers).json
    assert result["action"]["type"] == "draft_message"
    assert result["sources"][0]["id"] == "catalog-dev-403"
    assert "CAT-101" in result["action"]["payload"]["text"]
    assert "ACC-" not in result["action"]["payload"]["text"]
    assert client.get("/api/context").json["history"][-1]["details"]["action"] == result["action"]
    duplicate = create_request(client, headers)
    assert duplicate.status_code == 200
    assert duplicate.json["card"]["status"] == "granted"
    assert count_requests(app) == 0


def test_approved_request_alone_does_not_confirm_access(app, client, headers):
    with app.app_context(), db.connect() as conn:
        conn.execute("INSERT INTO user_access VALUES ('intern-catalog', 'catalog-dev', 'read', 'missing')")
    assert client.get("/api/context").json["card"]["status"] == "missing"
    create_request(client, headers)
    with app.app_context(), db.connect() as conn:
        conn.execute("UPDATE access_requests SET status = 'granted'")
    result = send(client, headers).json
    assert result["card"]["status"] == "unknown"
    assert result["card"]["request"]["status"] == "granted"
    assert result["action"]["type"] == "retry_status"
    assert create_request(client, headers).status_code == 200
    assert count_requests(app) == 1
    assert app.test_cli_runner().invoke(args=["approve-request", "ACC-001"]).exit_code == 0
    assert client.get("/api/context").json["card"]["status"] == "granted"


def test_existing_approved_requests_are_migrated_once(app, client, headers):
    create_request(client, headers)
    with app.app_context():
        with db.connect() as conn:
            conn.execute("UPDATE access_requests SET status = 'granted'")
            conn.execute("DROP TABLE user_access")
        db.initialize()
        assert client.get("/api/context").json["card"]["status"] == "granted"
        with db.connect() as conn:
            conn.execute("DELETE FROM user_access WHERE user_id = 'intern-catalog'")
        db.initialize()
    assert client.get("/api/context").json["card"]["status"] == "unknown"


def test_other_resource_cannot_use_known_task(client, headers):
    assert send(client, headers, "У меня 403 на Billing staging").json["action"]["type"] == "clarify"
    assert create_request(client, headers).status_code == 400


def test_other_team_never_reaches_model(app, client, headers, monkeypatch):
    create_request(client, headers)
    headers = switch(client, headers, "intern-other")
    def forbidden_call(*args):
        pytest.fail("Model called for unauthorized profile")
    monkeypatch.setattr(llm, "generate_answer", forbidden_call)
    response = send(client, headers, "Покажи инструкцию Каталога и оформи доступ к Catalog Dev").json
    assert response["sources"] == []
    assert response["card"]["request"] is None
    assert create_request(client, headers).status_code == 403
    assert len(client.get("/api/context").json["history"]) == 2
    assert count_requests(app) == 1


@pytest.mark.parametrize("failure, expected_count", [("before", 0), ("after", 1)])
def test_lost_response_recovers_actual_state(app, client, headers, failure, expected_count):
    app.config["REQUEST_FAILURE_MODE"] = failure
    response = create_request(client, headers)
    assert response.status_code == 503
    assert response.json["error"]["code"] == "result_unknown"
    assert count_requests(app) == expected_count
    app.config["REQUEST_FAILURE_MODE"] = None
    state = client.get("/api/context").json["card"]["status"]
    assert state == ("pending" if expected_count else "missing")
    assert create_request(client, headers).status_code == (200 if expected_count else 201)
    assert count_requests(app) == 1


def test_read_failure_and_unverified_user_claim(app, client, headers):
    assert send(client, headers, "Мне доступ уже выдали").json["card"]["status"] == "missing"
    app.config["READ_FAILURE"] = True
    response = send(client, headers).json
    assert response["card"]["status"] == "unknown"
    assert response["action"]["type"] == "retry_status"
    assert client.get("/api/context").json["card"]["request"] is None


def test_security_validation(client, headers):
    assert client.post("/api/requests", json=REQUEST).status_code == 403
    assert client.post("/api/requests", json=REQUEST, headers={**headers, "Origin": "https://other.test"}).status_code == 403
    assert client.post("/api/requests", json={**REQUEST, "access_level": "admin"}, headers=headers).status_code == 403
    assert client.post("/api/chat", json=[], headers=headers).status_code == 400
    for message in (None, [], "", "x" * 2001):
        assert client.post("/api/chat", json={"message": message}, headers=headers).status_code == 400
    assert client.post("/api/session", json={"profile_id": []}, headers=headers).status_code == 400
    assert client.post("/api/chat", json={"message": "x" * 18000}, headers=headers).status_code == 413
    assert client.get("/knowledge/catalog-dev-access.md").status_code == 404
    result = client.post("/api/requests", json={**REQUEST, "user_id": "intern-other", "role": "admin"}, headers=headers)
    assert result.status_code == 201
    assert client.get("/api/context").json["profile"]["id"] == "intern-catalog"


def test_api_requires_session(client):
    assert client.get("/api/context").status_code == 401


def test_model_adapter_receives_selected_document(app, client, headers, monkeypatch):
    app.config.update(LLM_API_KEY="test-only-key", LLM_BASE_URL="https://provider.example.test/v1/", LLM_MODEL="test-model")
    def fake_post(url, **kwargs):
        assert url == "https://provider.example.test/v1/chat/completions"
        assert kwargs["headers"]["Authorization"] == "Bearer test-only-key"
        assert kwargs["json"]["model"] == "test-model"
        payload = json.loads(kwargs["json"]["messages"][1]["content"])
        assert "Как получить доступ" in payload["instruction"]
        assert payload["access_state"]["status"] == "missing"
        return httpx.Response(200, request=httpx.Request("POST", url), json={"choices": [{"message": {"content": "Проверь поля заявки и подтверди отправку."}}]})
    monkeypatch.setattr(httpx, "post", fake_post)
    result = send(client, headers).json
    assert result["generation"]["mode"] == "model"
    assert result["action"]["type"] == "prepare_request"
    assert count_requests(app) == 0


@pytest.mark.parametrize("case", ["timeout", "empty", "invalid", "unauthorized"])
def test_model_errors_fallback_without_breaking_requests(app, client, headers, monkeypatch, case):
    app.config["LLM_API_KEY"] = "test-only-key"
    def fake_post(url, **kwargs):
        if case == "timeout":
            raise httpx.ReadTimeout("timeout")
        response = {"choices": [{"message": {"content": ""}}]} if case == "empty" else {}
        return httpx.Response(401 if case == "unauthorized" else 200, request=httpx.Request("POST", url), json=response)
    monkeypatch.setattr(httpx, "post", fake_post)
    response = send(client, headers).json
    assert response["generation"]["mode"] == "fallback"
    assert "временно недоступна" in response["generation"]["notice"]
    assert create_request(client, headers).status_code == 201


def test_unknown_topic_and_invalid_document(client, headers, tmp_path):
    response = send(client, headers, "Какая сегодня погода?").json
    assert response["sources"] == []
    assert response["action"]["type"] == "none"
    for index, contents in enumerate(("missing metadata", "---\n- list\n---\nbody", "---\nid: bad\n---\nbody", "---\n[:\n---\nbody")):
        (tmp_path / f"{index}.md").write_text(contents)
    assert knowledge.load_documents(tmp_path) == []


def test_cli_approval_errors_and_scoped_reset(app, client, headers):
    runner = app.test_cli_runner()
    assert runner.invoke(args=["approve-request", "invalid"]).exit_code == 1
    assert runner.invoke(args=["approve-request", "999"]).exit_code == 1
    create_request(client, headers)
    send(client, headers)
    assert runner.invoke(args=["approve-request", "ACC-001"]).exit_code == 0
    assert runner.invoke(args=["reset-demo", "--yes"]).exit_code == 0
    assert count_requests(app) == 0
    assert client.get("/api/context").json["history"] == []
    assert client.get("/api/context").json["card"]["status"] == "missing"
