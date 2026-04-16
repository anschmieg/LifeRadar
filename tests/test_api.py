import os
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

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
            os.environ,
            {
                "LIFE_RADAR_API_KEY": "secret-key",
                "LIFE_RADAR_BEEPER_ENABLED": "false",
            },
            clear=False,
        )
        self.env_patcher.start()

    def tearDown(self):
        self.env_patcher.stop()

    def auth_headers(self):
        return {"x-api-key": "secret-key"}

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

    def test_private_read_endpoints_require_api_key(self):
        endpoints = [
            "/alerts",
            "/conversations",
            "/messages",
            "/commitments",
            "/reminders",
            "/tasks",
            "/calendar/events",
            "/memories",
            "/probe-status",
            "/probe-status/candidates",
            "/search?q=matrix",
            "/docs",
            "/openapi.json",
        ]

        for endpoint in endpoints:
            with self.subTest(endpoint=endpoint):
                response = self.client.get(endpoint)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()["detail"], "Missing or invalid API key")

    def test_docs_and_openapi_work_with_api_key(self):
        docs_response = self.client.get("/docs", headers=self.auth_headers())
        schema_response = self.client.get("/openapi.json", headers=self.auth_headers())

        self.assertEqual(docs_response.status_code, 200)
        self.assertIn("Swagger UI", docs_response.text)
        self.assertEqual(schema_response.status_code, 200)
        self.assertEqual(schema_response.json()["info"]["title"], "LifeRadar API")

    def test_send_message_uses_matrix_binary_for_matrix_conversations_when_enabled(self):
        with patch.dict(os.environ, {"LIFE_RADAR_BEEPER_ENABLED": "true"}, clear=False), patch(
            "api.main.BEEPER_ENABLED", True
        ), patch(
            "api.main.load_conversation_for_send",
            new=AsyncMock(return_value={"source": "matrix", "external_id": "!room:example.com"}),
        ), patch("api.main.run_matrix_send", new=AsyncMock(return_value="$event123")):
            response = self.client.post(
                "/messages/send",
                headers=self.auth_headers(),
                json={
                    "conversation_id": "11111111-1111-1111-1111-111111111111",
                    "content_text": "hello from test",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(), {"status": "sent", "message_id": "$event123"}
        )

    def test_send_message_rejects_matrix_when_beeper_disabled(self):
        with patch(
            "api.main.load_conversation_for_send",
            new=AsyncMock(return_value={"source": "matrix", "external_id": "abc"}),
        ):
            response = self.client.post(
                "/messages/send",
                headers=self.auth_headers(),
                json={
                    "conversation_id": "11111111-1111-1111-1111-111111111111",
                    "content_text": "hello from test",
                },
            )

        self.assertEqual(response.status_code, 501)
        self.assertIn("disabled", response.json()["detail"])

    def test_send_message_uses_chat_gateway_for_direct_connectors(self):
        with patch(
            "api.main.load_conversation_for_send",
            new=AsyncMock(return_value={"source": "telegram", "external_id": "12345"}),
        ), patch(
            "api.main.run_direct_connector_send",
            new=AsyncMock(return_value="12345:99"),
        ) as send_mock:
            response = self.client.post(
                "/messages/send",
                headers=self.auth_headers(),
                json={
                    "conversation_id": "11111111-1111-1111-1111-111111111111",
                    "content_text": "hello from test",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "sent", "message_id": "12345:99"})
        send_mock.assert_awaited_once()

    def test_send_message_rejects_unsupported_sources(self):
        with patch(
            "api.main.load_conversation_for_send",
            new=AsyncMock(return_value={"source": "outlook", "external_id": "abc"}),
        ):
            response = self.client.post(
                "/messages/send",
                headers=self.auth_headers(),
                json={
                    "conversation_id": "11111111-1111-1111-1111-111111111111",
                    "content_text": "hello from test",
                },
            )

        self.assertEqual(response.status_code, 501)
        self.assertIn("not implemented", response.json()["detail"])

    def test_connector_routes_proxy_to_chat_gateway(self):
        gateway_payload = [{"provider": "telegram", "enabled": True, "accounts": []}]
        with patch("api.main.call_chat_gateway", new=AsyncMock(return_value=gateway_payload)):
            response = self.client.get("/connectors", headers=self.auth_headers())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), gateway_payload)

    def test_auth_pages_support_api_key_query_param(self):
        response = self.client.get("/auth/telegram?api_key=secret-key")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Telegram Login", response.text)
        self.assertIn('/connectors/${provider}/login', response.text)

    def test_get_tasks_without_status_uses_limit_only(self):
        connection = AsyncMock()
        connection.fetch = AsyncMock(return_value=[])
        pool = FakePool(connection)

        with patch("api.main.get_pool", new=AsyncMock(return_value=pool)):
            response = self.client.get("/tasks?limit=2", headers=self.auth_headers())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])
        connection.fetch.assert_awaited_once()
        query, limit = connection.fetch.await_args.args
        self.assertIn("FROM life_radar.planned_actions", query)
        self.assertEqual(limit, 2)

    def test_calendar_events_support_days_window_for_ongoing_events(self):
        connection = AsyncMock()
        connection.fetch = AsyncMock(return_value=[])
        pool = FakePool(connection)

        with patch("api.main.get_pool", new=AsyncMock(return_value=pool)):
            response = self.client.get(
                "/calendar/events?days=14&limit=5",
                headers=self.auth_headers(),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])
        connection.fetch.assert_awaited_once()
        query, from_date, to_date, limit = connection.fetch.await_args.args
        self.assertIn("COALESCE(scheduled_end, scheduled_start) >= $1", query)
        self.assertIn("scheduled_start <= $2", query)
        self.assertEqual(limit, 5)
        self.assertLess(from_date, to_date)

    def test_search_uses_expected_placeholder_arguments(self):
        connection = AsyncMock()
        connection.fetchval = AsyncMock(return_value=False)
        connection.fetch = AsyncMock(side_effect=[[], [], []])
        pool = FakePool(connection)

        with patch("api.main.get_pool", new=AsyncMock(return_value=pool)):
            response = self.client.get(
                "/search?q=matrix&limit=2",
                headers=self.auth_headers(),
            )

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

    def test_get_conversations_coerces_null_json_fields(self):
        connection = AsyncMock()
        connection.fetch = AsyncMock(return_value=[{
            "id": uuid4(),
            "source": "outlook",
            "external_id": "conv-1",
            "account_id": None,
            "title": "Inbox thread",
            "participants": None,
            "state": None,
            "needs_read": False,
            "needs_reply": True,
            "important_now": False,
            "waiting_on_other": False,
            "follow_up_later": False,
            "ready_to_act": False,
            "blocked_needs_context": False,
            "last_event_at": None,
            "last_triaged_at": None,
            "priority_score": None,
            "urgency_score": None,
            "social_weight": None,
            "reward_value": None,
            "energy_fit": None,
            "effort_estimate_minutes": None,
            "due_at": None,
            "metadata": None,
            "created_at": None,
            "updated_at": None,
        }])
        pool = FakePool(connection)

        with patch("api.main.get_pool", new=AsyncMock(return_value=pool)):
            response = self.client.get(
                "/conversations?limit=1",
                headers=self.auth_headers(),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 1)
        self.assertEqual(response.json()[0]["participants"], [])
        self.assertEqual(response.json()[0]["metadata"], {})
        self.assertEqual(response.json()[0]["state"], "active")
        self.assertIsNone(response.json()[0]["created_at"])
        self.assertIsNone(response.json()[0]["updated_at"])

    def test_get_messages_coerces_null_json_fields(self):
        connection = AsyncMock()
        connection.fetch = AsyncMock(return_value=[{
            "id": uuid4(),
            "conversation_id": None,
            "source": "matrix",
            "external_id": "evt-1",
            "sender_id": "@julia:example.com",
            "sender_label": "Julia",
            "occurred_at": "2026-04-08T10:00:00Z",
            "content_text": "Nachdem was Sarah gesagt hat, bin ich eher wieder gegen Sau",
            "content_json": None,
            "is_inbound": True,
            "reply_needed": None,
            "needs_read": None,
            "needs_reply": None,
            "importance_score": None,
            "triage_summary": None,
            "provenance": None,
            "created_at": None,
            "updated_at": None,
        }])
        pool = FakePool(connection)

        with patch("api.main.get_pool", new=AsyncMock(return_value=pool)):
            response = self.client.get(
                "/messages?limit=1&source=matrix",
                headers=self.auth_headers(),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 1)
        self.assertEqual(response.json()[0]["content_json"], {})
        self.assertEqual(response.json()[0]["provenance"], {})
        self.assertIsNone(response.json()[0]["created_at"])
        self.assertIsNone(response.json()[0]["updated_at"])

    def test_get_probe_status_coerces_null_metadata(self):
        connection = AsyncMock()
        connection.fetch = AsyncMock(return_value=[{
            "id": uuid4(),
            "candidate_id": "matrix-rust-sdk",
            "candidate_type": "matrix-native",
            "status": "ok",
            "observed_at": "2026-04-08T10:00:00Z",
            "latency_ms": 10,
            "freshness_seconds": 5,
            "total_events": 123,
            "decrypt_failures": 0,
            "encrypted_non_text": 0,
            "running_processes": 1,
            "metadata": None,
            "notes": None,
        }])
        pool = FakePool(connection)

        with patch("api.main.get_pool", new=AsyncMock(return_value=pool)):
            response = self.client.get("/probe-status", headers=self.auth_headers())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 1)
        self.assertEqual(response.json()[0]["metadata"], {})

    def test_get_probe_status_coerces_stringified_metadata(self):
        connection = AsyncMock()
        connection.fetch = AsyncMock(return_value=[{
            "id": uuid4(),
            "candidate_id": "matrix-rust-sdk",
            "candidate_type": "matrix-native",
            "status": "ok",
            "observed_at": "2026-04-08T10:00:00Z",
            "latency_ms": 10,
            "freshness_seconds": 5,
            "total_events": 123,
            "decrypt_failures": 0,
            "encrypted_non_text": 0,
            "running_processes": 1,
            "metadata": '{"store_path":"/tmp/store"}',
            "notes": None,
        }])
        pool = FakePool(connection)

        with patch("api.main.get_pool", new=AsyncMock(return_value=pool)):
            response = self.client.get("/probe-status", headers=self.auth_headers())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["metadata"], {"store_path": "/tmp/store"})

    def test_get_messages_coerces_stringified_json_fields(self):
        connection = AsyncMock()
        connection.fetch = AsyncMock(return_value=[{
            "id": uuid4(),
            "conversation_id": None,
            "source": "matrix",
            "external_id": "evt-2",
            "sender_id": "@julia:example.com",
            "sender_label": "Julia",
            "occurred_at": "2026-04-08T10:00:00Z",
            "content_text": "hi",
            "content_json": '{"kind":"text"}',
            "is_inbound": True,
            "reply_needed": None,
            "needs_read": None,
            "needs_reply": None,
            "importance_score": None,
            "triage_summary": None,
            "provenance": '{"source":"matrix-sdk"}',
            "created_at": None,
            "updated_at": None,
        }])
        pool = FakePool(connection)

        with patch("api.main.get_pool", new=AsyncMock(return_value=pool)):
            response = self.client.get("/messages?limit=1", headers=self.auth_headers())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["content_json"], {"kind": "text"})
        self.assertEqual(response.json()[0]["provenance"], {"source": "matrix-sdk"})

    def test_get_conversations_uses_coalesced_state_filter(self):
        connection = AsyncMock()
        connection.fetch = AsyncMock(return_value=[])
        pool = FakePool(connection)

        with patch("api.main.get_pool", new=AsyncMock(return_value=pool)):
            response = self.client.get(
                "/conversations?limit=2",
                headers=self.auth_headers(),
            )

        self.assertEqual(response.status_code, 200)
        query = connection.fetch.await_args.args[0]
        self.assertIn("COALESCE(state, 'active')", query)


if __name__ == "__main__":
    unittest.main()
