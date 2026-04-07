import os
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from api.main import app


class FakeAcquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakePool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return FakeAcquire(self.connection)


class LifeRadarApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.env_patcher = patch.dict(
            os.environ, {"LIFE_RADAR_API_KEY": "secret-key"}, clear=False
        )
        self.env_patcher.start()

    def tearDown(self):
        self.env_patcher.stop()

    def test_send_message_requires_api_key(self):
        response = self.client.post(
            "/messages/send",
            json={
                "conversation_id": "11111111-1111-1111-1111-111111111111",
                "content_text": "hi",
            },
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Missing or invalid API key")

    def test_send_message_uses_matrix_binary_for_matrix_conversations(self):
        with patch(
            "api.main.load_conversation_for_send",
            new=AsyncMock(return_value={"source": "matrix", "external_id": "!room:example.com"}),
        ), patch("api.main.run_matrix_send", new=AsyncMock(return_value="$event123")):
            response = self.client.post(
                "/messages/send",
                headers={"x-api-key": "secret-key"},
                json={
                    "conversation_id": "11111111-1111-1111-1111-111111111111",
                    "content_text": "hello from test",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(), {"status": "sent", "message_id": "$event123"}
        )

    def test_send_message_rejects_unsupported_sources(self):
        with patch(
            "api.main.load_conversation_for_send",
            new=AsyncMock(return_value={"source": "outlook", "external_id": "abc"}),
        ):
            response = self.client.post(
                "/messages/send",
                headers={"x-api-key": "secret-key"},
                json={
                    "conversation_id": "11111111-1111-1111-1111-111111111111",
                    "content_text": "hello from test",
                },
            )

        self.assertEqual(response.status_code, 501)
        self.assertIn("not implemented", response.json()["detail"])

    def test_get_tasks_without_status_uses_limit_only(self):
        connection = AsyncMock()
        connection.fetch = AsyncMock(return_value=[])
        pool = FakePool(connection)

        with patch("api.main.get_pool", new=AsyncMock(return_value=pool)):
            response = self.client.get("/tasks?limit=2")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])
        connection.fetch.assert_awaited_once()
        query, limit = connection.fetch.await_args.args
        self.assertIn("FROM life_radar.planned_actions", query)
        self.assertEqual(limit, 2)

    def test_search_uses_expected_placeholder_arguments(self):
        connection = AsyncMock()
        connection.fetchval = AsyncMock(return_value=False)
        connection.fetch = AsyncMock(side_effect=[[], [], []])
        pool = FakePool(connection)

        with patch("api.main.get_pool", new=AsyncMock(return_value=pool)):
            response = self.client.get("/search?q=matrix&limit=2")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])
        self.assertEqual(connection.fetch.await_count, 3)

        first_query, first_like, first_limit = connection.fetch.await_args_list[0].args
        second_query, second_like, second_limit = connection.fetch.await_args_list[1].args
        third_query, third_like, third_limit = connection.fetch.await_args_list[2].args

        self.assertIn("FROM life_radar.conversations", first_query)
        self.assertIn("LIMIT $2", second_query)
        self.assertIn("LIMIT $2", third_query)
        self.assertEqual(first_like, "%matrix%")
        self.assertEqual(second_like, "%matrix%")
        self.assertEqual(third_like, "%matrix%")
        self.assertEqual(first_limit, 2)
        self.assertEqual(second_limit, 2)
        self.assertEqual(third_limit, 2)


if __name__ == "__main__":
    unittest.main()
