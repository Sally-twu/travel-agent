import json
import os
import unittest
from unittest.mock import MagicMock, patch

import conversation_api as api


CONVERSATION_ID = "13f5bb94-a35c-48b1-a0b4-54d6188d7d94"


class ConversationApiTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(
            os.environ,
            {
                "CONVERSATIONS_TABLE": "test-conversations",
                "CONVERSATIONS_USER_INDEX": "GSI1",
                "REQUIRE_AUTH_CONTEXT": "false",
                "DEV_USER_ID": "qa-user",
            },
            clear=False,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    @staticmethod
    def event(method, path, *, query=None, path_parameters=None, body=None):
        value = {
            "version": "2.0",
            "rawPath": path,
            "requestContext": {
                "requestId": "qa-request",
                "http": {"method": method, "path": path},
            },
            "queryStringParameters": query,
            "pathParameters": path_parameters,
        }
        if body is not None:
            value["body"] = json.dumps(body, ensure_ascii=False)
            value["isBase64Encoded"] = False
        return value

    def test_post_new_conversation_persists_exchange(self):
        event = self.event(
            "POST",
            "/v1/chat/messages",
            body={"conversationId": None, "text": "我人在東京新宿"},
        )
        medical_payload = {
            "data": {
                "conversationId": CONVERSATION_ID,
                "title": "海外就醫・2026/11/04",
                "userTimestamp": "2026-11-04T01:00:00.000Z",
                "assistantMessage": {
                    "role": "ASSISTANT",
                    "timestamp": "2026-11-04T01:00:01.000Z",
                    "content": {"text": "找到合作院所。", "tasks": [], "question": None},
                },
            }
        }
        medical_response = {
            "statusCode": 200,
            "headers": {},
            "body": json.dumps(medical_payload, ensure_ascii=False),
        }
        with (
            patch.object(api.overseas_medical, "lambda_handler", return_value=medical_response),
            patch.object(api, "_persist_exchange") as persist,
        ):
            result = api.lambda_handler(event, None)

        self.assertEqual(result["statusCode"], 200)
        persist.assert_called_once()
        kwargs = persist.call_args.kwargs
        self.assertEqual(kwargs["principal_id"], "qa-user")
        self.assertEqual(kwargs["conversation_id"], CONVERSATION_ID)
        self.assertEqual(kwargs["user_text"], "我人在東京新宿")
        self.assertTrue(kwargs["is_new"])

    def test_list_returns_only_public_conversation_fields(self):
        table = MagicMock()
        table.query.return_value = {
            "Items": [
                {
                    "PK": f"CONV#{CONVERSATION_ID}",
                    "SK": "META",
                    "ownerId": "qa-user",
                    "conversationId": CONVERSATION_ID,
                    "title": "海外就醫・2026/11/04",
                    "preview": "找到合作院所。",
                    "updatedAt": "2026-11-04T01:00:01.000Z",
                    "internalNote": "must-not-leak",
                }
            ]
        }
        with patch.object(api, "_table", return_value=table):
            result = api.lambda_handler(self.event("GET", "/v1/conversations"), None)

        self.assertEqual(result["statusCode"], 200)
        payload = json.loads(result["body"])
        self.assertEqual(payload["data"]["items"][0]["conversationId"], CONVERSATION_ID)
        self.assertNotIn("ownerId", result["body"])
        self.assertNotIn("internalNote", result["body"])
        self.assertIsNone(payload["data"]["nextCursor"])

    def test_detail_returns_old_to_new_and_removes_old_suggestions(self):
        table = MagicMock()
        table.get_item.return_value = {
            "Item": {
                "PK": f"CONV#{CONVERSATION_ID}",
                "SK": "META",
                "ownerId": "qa-user",
                "title": "海外就醫・2026/11/04",
            }
        }
        table.query.return_value = {
            # Query is descending: assistant is returned before user.
            "Items": [
                {
                    "PK": f"CONV#{CONVERSATION_ID}",
                    "SK": "MSG#2026-11-04T01:00:01.000Z#1#a",
                    "role": "ASSISTANT",
                    "timestamp": "2026-11-04T01:00:01.000Z",
                    "content": {
                        "text": "找到合作院所。",
                        "tasks": [],
                        "question": {
                            "text": "還需要協助嗎？",
                            "suggestedReplies": [{"label": "需要", "value": "需要"}],
                        },
                    },
                },
                {
                    "PK": f"CONV#{CONVERSATION_ID}",
                    "SK": "MSG#2026-11-04T01:00:00.000Z#0#u",
                    "role": "USER",
                    "timestamp": "2026-11-04T01:00:00.000Z",
                    "text": "我人在東京新宿",
                },
            ]
        }
        event = self.event(
            "GET",
            f"/v1/conversations/{CONVERSATION_ID}",
            path_parameters={"conversationId": CONVERSATION_ID},
        )
        with patch.object(api, "_table", return_value=table):
            result = api.lambda_handler(event, None)

        self.assertEqual(result["statusCode"], 200)
        messages = json.loads(result["body"])["data"]["messages"]
        self.assertEqual([message["role"] for message in messages], ["USER", "ASSISTANT"])
        self.assertEqual(
            messages[1]["content"]["question"]["suggestedReplies"],
            [],
        )

    def test_detail_hides_foreign_conversation(self):
        table = MagicMock()
        table.get_item.return_value = {
            "Item": {"ownerId": "another-user", "title": "private"}
        }
        event = self.event(
            "GET",
            f"/v1/conversations/{CONVERSATION_ID}",
            path_parameters={"conversationId": CONVERSATION_ID},
        )
        with patch.object(api, "_table", return_value=table):
            result = api.lambda_handler(event, None)

        self.assertEqual(result["statusCode"], 404)
        self.assertEqual(
            json.loads(result["body"])["error"]["code"],
            "CONVERSATION_NOT_FOUND",
        )

    def test_list_cursor_cannot_cross_users(self):
        cursor = api._encode_cursor(
            {
                "PK": f"CONV#{CONVERSATION_ID}",
                "SK": "META",
                "GSI1PK": "USER#another-user",
                "GSI1SK": f"2026-11-04T01:00:01.000Z#{CONVERSATION_ID}",
            }
        )
        event = self.event("GET", "/v1/conversations", query={"cursor": cursor})
        result = api.lambda_handler(event, None)
        self.assertEqual(result["statusCode"], 400)
        self.assertEqual(json.loads(result["body"])["error"]["code"], "INVALID_CURSOR")

    def test_persist_uses_one_transaction_with_meta_and_two_messages(self):
        client = MagicMock()
        assistant = {
            "role": "ASSISTANT",
            "timestamp": "2026-11-04T01:00:01.000Z",
            "content": {"text": "找到合作院所。", "tasks": [], "question": None},
        }

        def fake_item(value):
            return value

        def fake_values(value):
            return value

        with (
            patch.object(api, "_ddb_client", return_value=client),
            patch.object(api, "_serialize_item", side_effect=fake_item),
            patch.object(api, "_serialize_values", side_effect=fake_values),
        ):
            api._persist_exchange(
                principal_id="qa-user",
                conversation_id=CONVERSATION_ID,
                title="海外就醫・2026/11/04",
                user_text="我人在東京新宿",
                user_timestamp="2026-11-04T01:00:00.000Z",
                assistant_message=assistant,
                is_new=True,
            )

        request = client.transact_write_items.call_args.kwargs
        self.assertEqual(len(request["TransactItems"]), 3)
        self.assertEqual(
            request["TransactItems"][0]["Put"]["Item"]["GSI1PK"],
            "USER#qa-user",
        )


if __name__ == "__main__":
    unittest.main()
