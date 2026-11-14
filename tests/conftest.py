import os
from urllib.parse import urlsplit

import pytest

from app import create_app
from buddy.db import connect


@pytest.fixture
def app():
    url = os.environ.get("TEST_DATABASE_URL", "")
    if not url or not urlsplit(url).path.endswith("_test"):
        pytest.fail("Set TEST_DATABASE_URL to a dedicated PostgreSQL database ending in _test")
    application = create_app({"TESTING": True, "DATABASE_URL": url, "LLM_API_KEY": "", "LLM_ENABLED": True})
    with application.app_context(), connect() as conn:
        conn.execute("TRUNCATE messages, access_requests, browser_sessions RESTART IDENTITY")
        conn.execute("DELETE FROM user_access WHERE user_id <> 'intern-pregranted'")
    yield application


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def headers(client):
    from html.parser import HTMLParser

    class TokenParser(HTMLParser):
        token = ""

        def handle_starttag(self, tag, attrs):
            data = dict(attrs)
            if tag == "meta" and data.get("name") == "csrf-token":
                self.token = data["content"]

    parser = TokenParser()
    parser.feed(client.get("/").text)
    return {"X-CSRF-Token": parser.token}
