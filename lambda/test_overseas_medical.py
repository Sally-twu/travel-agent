import json
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import overseas_medical as medical


class OverseasMedicalTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(
            os.environ,
            {
                "ENABLE_BEDROCK_EXTRACTION": "false",
                "OPEN_ONLY": "true",
                "SEARCH_RADIUS_KM": "50",
                "MAX_RESULTS": "3",
                "INSURANCE_MATCH_REQUIRED": "false",
            },
            clear=False,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    @staticmethod
    def facility(**overrides):
        value = {
            "facilityId": "internal-001",
            "name": "QA Partner Clinic",
            "address": "1501 Broadway, New York, NY",
            "latitude": 40.7589,
            "longitude": -73.9851,
            "timezone": "America/New_York",
            "phone": "+1-212-555-0100",
            "services": ["general"],
            "insuranceNetworks": ["Demo Insurance"],
            "hours": {day: ["00:00-00:00"] for day in medical.DAY_KEYS},
            "emergency24h": False,
            "active": True,
            "partnershipStatus": "ACTIVE",
            "validUntil": "2099-12-31",
            "priority": 10,
            "internalContact": "must-not-leak",
            "contractRate": "must-not-leak",
        }
        value.update(overrides)
        return value

    def test_times_square_query_returns_fixed_contract_without_internal_fields(self):
        event = {
            "body": json.dumps(
                {
                    "conversationId": None,
                    "text": "我住在紐約時代廣場附近，最近的合作診所在哪裡？",
                },
                ensure_ascii=False,
            )
        }
        with (
            patch.object(
                medical,
                "_geocode",
                return_value={
                    "latitude": 40.7580,
                    "longitude": -73.9855,
                    "countryCode": "US",
                    "label": "Times Square, New York, NY",
                },
            ),
            patch.object(medical, "_query_facilities", return_value=[self.facility()]),
        ):
            result = medical.lambda_handler(event, None)

        self.assertEqual(result["statusCode"], 200)
        payload = json.loads(result["body"])
        self.assertEqual(payload["data"]["assistantMessage"]["role"], "ASSISTANT")
        content = payload["data"]["assistantMessage"]["content"]
        self.assertEqual(content["tasks"], [])
        self.assertIsInstance(content["question"], dict)
        self.assertIn("QA Partner Clinic", content["text"])
        self.assertNotIn("internal-001", result["body"])
        self.assertNotIn("must-not-leak", result["body"])

    def test_invalid_conversation_id_is_rejected(self):
        result = medical.lambda_handler(
            {"conversationId": "not-a-uuid", "text": "我在紐約"},
            None,
        )
        self.assertEqual(result["statusCode"], 400)
        self.assertEqual(json.loads(result["body"])["error"]["code"], "INVALID_CONVERSATION_ID")

    def test_shinjuku_chinese_location_fallback(self):
        text = "我人在東京新宿，附近有國泰合作的診所嗎？"
        self.assertEqual(medical._heuristic_location(text), "東京新宿")

    def test_japan_emergency_number(self):
        self.assertEqual(medical._emergency_number("我人在東京新宿，現在胸痛"), "119")

    def test_places_v2_geocode_uses_geocode_api(self):
        client = MagicMock()
        client.geocode.return_value = {
            "ResultItems": [
                {
                    "Position": [139.7034, 35.6938],
                    "Title": "東京新宿",
                    "Address": {"Country": {"Code2": "JP"}},
                }
            ]
        }
        with patch.object(medical, "_aws_client", return_value=client):
            result = medical._geocode("東京新宿")

        client.geocode.assert_called_once_with(QueryText="東京新宿", MaxResults=1)
        client.search_text.assert_not_called()
        self.assertEqual(result["countryCode"], "JP")

    def test_emergency_rule_skips_external_lookups(self):
        event = {"conversationId": None, "text": "我人在紐約，現在胸痛而且呼吸困難"}
        with (
            patch.object(medical, "_geocode") as geocode,
            patch.object(medical, "_query_facilities") as query,
        ):
            result = medical.lambda_handler(event, None)
        self.assertEqual(result["statusCode"], 200)
        self.assertIn("911", json.loads(result["body"])["data"]["assistantMessage"]["content"]["text"])
        geocode.assert_not_called()
        query.assert_not_called()

    def test_previous_day_overnight_period_is_open(self):
        # 2026-11-02 06:30 UTC is Monday 01:30 in New York. Sunday's
        # 20:00-02:00 interval should still be open; Monday's same interval
        # should not independently make 01:30 open.
        now_utc = datetime(2026, 11, 2, 6, 30, tzinfo=timezone.utc)
        sunday_open = self.facility(hours={"sun": ["20:00-02:00"]})
        monday_only = self.facility(hours={"mon": ["20:00-02:00"]})
        self.assertTrue(medical._is_open(sunday_open, now_utc))
        self.assertFalse(medical._is_open(monday_only, now_utc))

    def test_inactive_or_expired_facilities_are_filtered(self):
        now_utc = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
        values = [
            self.facility(active=False),
            self.facility(facilityId="expired", validUntil="2020-01-01"),
        ]
        result = medical._eligible_facilities(values, 40.7580, -73.9855, None, "general", now_utc)
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
