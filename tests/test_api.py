import os
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from api.main import app


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


if __name__ == "__main__":
    unittest.main()
