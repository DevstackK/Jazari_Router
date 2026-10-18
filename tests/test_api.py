import importlib
import os
import sys
from unittest.mock import MagicMock, patch


def load_api():
    os.environ.update({
        "JWT_SECRET": "j" * 32,
        "GATEWAY_API_KEY": "gateway-test-key",
        "CRON_SECRET": "cron-test-key",
        "DATABASE_URL": "postgresql://test",
    })
    connection = MagicMock()
    cursor = connection.cursor.return_value
    cursor.fetchall.return_value = []
    cursor.fetchone.return_value = None
    with patch("psycopg2.connect", return_value=connection):
        sys.modules.pop("api.index", None)
        module = importlib.import_module("api.index")
    module.psycopg2.connect = MagicMock(return_value=connection)
    module._test_connection = connection
    return module


def test_openai_models_requires_auth():
    api = load_api()
    response = api.app.test_client().get("/v1/models")
    assert response.status_code == 401


def test_openai_models_accepts_gateway_key():
    api = load_api()
    response = api.app.test_client().get(
        "/v1/models", headers={"Authorization": "Bearer gateway-test-key"}
    )
    assert response.status_code == 200
    ids = {model["id"] for model in response.get_json()["data"]}
    assert {"auto", "auto/free", "gemini", "openrouter"} <= ids


def test_cron_fails_closed_without_valid_secret():
    api = load_api()
    assert api.app.test_client().get("/api/cron/refresh-models").status_code == 401


def test_openai_chat_validates_messages_before_upstream_call():
    api = load_api()
    response = api.app.test_client().post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer gateway-test-key"},
        json={"model": "auto", "messages": [{"role": "tool", "content": "no"}]},
    )
    assert response.status_code == 400

