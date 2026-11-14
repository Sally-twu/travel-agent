import io
import json
import os
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import overseas_medical as medical
import overseas_medical_tool as tool


TOKYO = {"latitude": 35.6938, "longitude": 139.7034, "countryCode": "JP", "label": "新宿"}
NEW_YORK = {"latitude": 40.7580, "longitude": -73.9855, "countryCode": "US", "label": "Times Square"}


def facility(**overrides):
    value = {
        "facilityId": "qa-jp-clinic-001",
        "name": "QA Shinjuku Clinic",
        "address": "Shinjuku, Tokyo",
        "latitude": 35.6940,
        "longitude": 139.7030,
        "timezone": "Asia/Tokyo",
        "phone": "+81-3-0000-0000",
        "services": ["general"],
        "languages": ["ja", "en"],
        "hours": {day: ["00:00-00:00"] for day in medical.DAY_KEYS},
        "emergency24h": False,
        "active": True,
        "partnershipStatus": "ACTIVE",
        "validUntil": "2099-12-31",
        "internalContact": "must-not-leak",
        "contractRate": "must-not-leak",
    }
    value.update(overrides)
    return value


def hospital(**overrides):
    value = facility(
        facilityId="qa-jp-hospital-001",
        name="QA Tokyo Hospital",
        facilityType="hospital",
        services=["emergency", "internal_medicine"],
        emergency24h=True,
        thirdPartyDirectBilling=False,
    )
    value.update(overrides)
    return value


def eligible_policy(**overrides):
    value = {
        "insurer": "CATHAY_CENTURY",
        "productCode": "OVERSEAS_TRAVEL",
        "status": "ACTIVE",
        "coverageStart": "2026-01-01",
        "coverageEnd": "2099-12-31",
        "currency": "TWD",
        "coverages": {"accidentMedical": 1_200_000, "overseasSuddenIllnessMedical": 1_200_000},
        "riders": ["TRAVEL_INCONVENIENCE"],
    }
    value.update(overrides)
    return value


def trusted_context(user_id="user-123"):
    return SimpleNamespace(
        aws_request_id="req-1",
        client_context=SimpleNamespace(custom={"trustedUserId": user_id}),
    )


class GetClinicInfoTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(
            os.environ,
            {
                "SEARCH_RADIUS_KM": "50",
                "MAX_RESULTS": "3",
                "DATA_IS_MOCK": "true",
                "TRUSTED_IDENTITY_CONTEXT_KEY": "trustedUserId",
                "POLICY_SERVICE_FUNCTION": "travel-policy-service",
            },
            clear=False,
        )
        env.start()
        self.addCleanup(env.stop)

    def invoke(self, event, context=None, geocode=TOKYO, facilities=None, policies=None):
        with (
            patch.object(medical, "_geocode", return_value=geocode),
            patch.object(medical, "_query_facilities", return_value=facilities or []),
            patch.object(tool, "fetch_policies", return_value=policies or []) as fetch,
        ):
            result = tool.lambda_handler(event, context)
        json.dumps(result, ensure_ascii=False)  # must be valid JSON
        return result, fetch

    # ---- triage ---------------------------------------------------------
    def test_triage_general_keywords(self):
        result = tool.triage("發燒、喉嚨痛")
        self.assertEqual(result["route"], tool.ROUTE_GENERAL)
        self.assertEqual(result["specialty"], "internal_medicine")

    def test_triage_severe_wins_over_general(self):
        result = tool.triage("發燒而且呼吸困難")
        self.assertEqual(result["route"], tool.ROUTE_SEVERE)
        self.assertIn("呼吸困難", result["matchedKeywords"])

    def test_unknown_symptoms_default_to_general(self):
        result = tool.triage("腳踝扭到")
        self.assertEqual(result["route"], tool.ROUTE_GENERAL)
        self.assertEqual(result["specialty"], "general")

    # ---- validation -----------------------------------------------------
    def test_invalid_arguments(self):
        cases = [
            {"symptoms": "發燒"},
            {"location": " ", "symptoms": "發燒"},
            {"location": "Tokyo"},
            {"location": "Tokyo", "symptoms": "發燒", "language": "fr"},
            {"location": "Tokyo", "symptoms": "發燒", "accidentMedical": 2_000_000},
        ]
        for event in cases:
            with self.subTest(event=event):
                result = tool.lambda_handler(event, None)
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["error"]["code"], "INVALID_ARGUMENT")
                self.assertFalse(result["error"]["retryable"])
                self.assertIsNone(result["data"])

    # ---- general outpatient route ----------------------------------------
    def test_general_route_returns_nearest_open_clinics(self):
        far = facility(facilityId="far", name="Far", latitude=35.75, longitude=139.75)
        closed = facility(facilityId="closed", name="Closed", hours={})
        near = facility()
        result, fetch = self.invoke(
            {"location": "Tokyo Shinjuku", "symptoms": "發燒、感冒"},
            facilities=[far, closed, near],
        )
        self.assertEqual(result["schemaVersion"], "2.0")
        self.assertEqual(result["status"], "success")
        self.assertTrue(result["source"]["isMock"])
        data = result["data"]
        self.assertEqual(data["triage"]["route"], tool.ROUTE_GENERAL)
        self.assertIsNone(data["emergencyAssistance"])
        self.assertIsNone(data["advancePayment"])
        self.assertIsNone(data["displayNotice"])
        self.assertEqual([c["clinicId"] for c in data["clinics"]], ["qa-jp-clinic-001", "far"])
        self.assertTrue(all(c["advancePaymentCoordinable"] is None for c in data["clinics"]))
        self.assertNotIn("must-not-leak", json.dumps(result))
        fetch.assert_not_called()

    def test_general_route_filters_language(self):
        result, _ = self.invoke(
            {"location": "Tokyo", "symptoms": "拉肚子", "language": "zh"},
            facilities=[facility()],
        )
        self.assertEqual(result["error"]["code"], "NOT_FOUND")

    def test_general_route_unknown_hours_kept_as_unknown(self):
        result, _ = self.invoke(
            {"location": "Tokyo", "symptoms": "咳嗽"},
            facilities=[facility(timezone=None)],
        )
        self.assertEqual(result["data"]["clinics"][0]["openingStatus"], "UNKNOWN")

    def test_general_route_geocode_failure_is_not_found(self):
        result, _ = self.invoke({"location": "???", "symptoms": "發燒"}, geocode=None)
        self.assertEqual(result["error"]["code"], "NOT_FOUND")

    def test_downstream_failure_is_retryable(self):
        with patch.object(medical, "_geocode", side_effect=RuntimeError("boom")):
            result = tool.lambda_handler({"location": "Tokyo", "symptoms": "發燒"}, None)
        self.assertEqual(result["error"]["code"], "SERVICE_UNAVAILABLE")
        self.assertTrue(result["error"]["retryable"])

    # ---- severe route ------------------------------------------------------
    def test_severe_eligible_prioritises_direct_billing_hospital(self):
        near = hospital()
        billing = hospital(
            facilityId="qa-jp-hospital-002",
            name="QA Billing Hospital",
            latitude=35.70,
            longitude=139.71,
            thirdPartyDirectBilling=True,
        )
        clinic = facility(facilityId="clinic-only")
        result, fetch = self.invoke(
            {"location": "Tokyo Shinjuku", "symptoms": "胸痛需要住院"},
            context=trusted_context(),
            facilities=[near, billing, clinic],
            policies=[eligible_policy()],
        )
        data = result["data"]
        fetch.assert_called_once_with("user-123")
        self.assertEqual(data["triage"]["route"], tool.ROUTE_SEVERE)
        self.assertEqual(data["emergencyAssistance"]["phone"], "+886-2-2755-1258")
        self.assertEqual(data["emergencyAssistance"]["localEmergencyNumber"], "119")
        self.assertEqual(data["advancePayment"]["status"], tool.STATUS_ELIGIBLE)
        self.assertEqual(data["advancePayment"]["partner"], "聯合國際救援公司")
        self.assertEqual(
            [c["clinicId"] for c in data["clinics"]],
            ["qa-jp-hospital-002", "qa-jp-hospital-001"],
        )
        self.assertEqual([c["advancePaymentCoordinable"] for c in data["clinics"]], [True, False])
        self.assertEqual(data["displayNotice"], "由急難救助合作夥伴與當地醫院最終確認")

    def test_severe_not_eligible_does_not_claim_advance_payment(self):
        result, _ = self.invoke(
            {"location": "Tokyo", "symptoms": "意識不清"},
            context=trusted_context(),
            facilities=[hospital(thirdPartyDirectBilling=True)],
            policies=[eligible_policy(riders=[])],
        )
        data = result["data"]
        self.assertEqual(data["advancePayment"]["status"], tool.STATUS_NOT_ELIGIBLE)
        self.assertEqual(data["advancePayment"]["reasons"], ["TRAVEL_INCONVENIENCE_RIDER_MISSING"])
        self.assertIsNone(data["clinics"][0]["advancePaymentCoordinable"])
        self.assertEqual(data["displayNotice"], tool.FINAL_CONFIRMATION_NOTICE)

    def test_severe_without_identity_is_undetermined_and_skips_policy_service(self):
        result, fetch = self.invoke(
            {"location": "Tokyo", "symptoms": "嚴重過敏"},
            context=None,
            facilities=[hospital()],
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["data"]["advancePayment"]["status"], tool.STATUS_UNDETERMINED)
        self.assertEqual(result["data"]["advancePayment"]["reasons"], ["IDENTITY_UNAVAILABLE"])
        fetch.assert_not_called()

    def test_severe_policy_service_failure_still_returns_guidance(self):
        with (
            patch.object(medical, "_geocode", return_value=TOKYO),
            patch.object(medical, "_query_facilities", return_value=[hospital()]),
            patch.object(tool, "fetch_policies", side_effect=RuntimeError("down")),
        ):
            result = tool.lambda_handler({"location": "Tokyo", "symptoms": "大量出血"}, trusted_context())
        data = result["data"]
        self.assertEqual(data["advancePayment"]["reasons"], ["POLICY_SERVICE_UNAVAILABLE"])
        self.assertEqual(len(data["clinics"]), 1)

    def test_severe_geocode_failure_still_returns_hotline(self):
        with patch.object(medical, "_geocode", side_effect=RuntimeError("down")):
            result = tool.lambda_handler({"location": "Tokyo", "symptoms": "中風"}, None)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["data"]["emergencyAssistance"]["phone"], "+886-2-2755-1258")
        self.assertEqual(result["data"]["clinics"], [])

    # ---- versioned rule ------------------------------------------------------
    def test_rule_thresholds_and_country(self):
        today = date(2026, 11, 14)
        self.assertEqual(
            tool.evaluate_advance_payment([eligible_policy()], "KR", today)["status"],
            tool.STATUS_ELIGIBLE,
        )
        self.assertEqual(
            tool.evaluate_advance_payment([eligible_policy()], "US", today)["reasons"],
            ["COUNTRY_NOT_COVERED"],
        )
        low = eligible_policy(
            coverages={"accidentMedical": 1_000_000, "overseasSuddenIllnessMedical": 1_200_000}
        )
        self.assertEqual(
            tool.evaluate_advance_payment([low], "JP", today)["reasons"],
            ["ACCIDENT_MEDICAL_BELOW_THRESHOLD"],
        )
        expired = eligible_policy(coverageEnd="2026-01-31")
        self.assertEqual(
            tool.evaluate_advance_payment([expired], "JP", today)["reasons"],
            ["NO_ACTIVE_CATHAY_TRAVEL_POLICY"],
        )
        other_insurer = eligible_policy(insurer="OTHER")
        self.assertEqual(
            tool.evaluate_advance_payment([other_insurer], "JP", today)["status"],
            tool.STATUS_NOT_ELIGIBLE,
        )

    def test_identity_never_read_from_input_or_without_config(self):
        with patch.dict(os.environ, {"TRUSTED_IDENTITY_CONTEXT_KEY": ""}):
            self.assertIsNone(tool.trusted_user_id(trusted_context()))
        self.assertEqual(tool.trusted_user_id(trusted_context("abc")), "abc")

    def test_fetch_policies_invokes_policy_lambda(self):
        client = MagicMock()
        client.invoke.return_value = {
            "Payload": io.BytesIO(json.dumps({"policies": [eligible_policy()]}).encode())
        }
        with patch.object(medical, "_aws_client", return_value=client):
            policies = tool.fetch_policies("user-123")
        self.assertEqual(len(policies), 1)
        kwargs = client.invoke.call_args.kwargs
        self.assertEqual(kwargs["FunctionName"], "travel-policy-service")
        self.assertEqual(json.loads(kwargs["Payload"]), {"userId": "user-123"})


if __name__ == "__main__":
    unittest.main()
