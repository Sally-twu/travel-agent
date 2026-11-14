"""AgentCore Gateway Target for the ``get-clinic-info`` tool (schema 2.0).

Flow (see 海外就醫lambda.png):

1. The Agent passes the user's current ``location`` and ``symptoms``.
2. Symptoms are triaged by deterministic keywords:
   * GENERAL_OUTPATIENT  - fever, cold, sore throat, mild GI upset ...
     geocode -> map symptoms to specialty -> filter opening status, language
     and data validity -> sort by distance -> return nearest clinics.
   * HOSPITALIZATION_OR_SEVERE - hospitalization, breathing difficulty,
     altered consciousness, heavy bleeding, chest pain, stroke signs, severe
     allergy ... -> provide Cathay 24h overseas emergency assistance ->
     read the user identity from the trusted invocation context -> call the
     policy service for coverage (never from Agent input) -> evaluate the
     versioned advance-payment rule -> if preliminarily eligible, prioritise
     hospitals that accept third-party direct billing; otherwise recommend
     suitable facilities without claiming advance payment. Final confirmation
     is always made by the assistance partner and the local hospital.

The tool never calls Bedrock, never writes conversation records and never logs
symptoms, locations or policy data.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import date, datetime, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# 重用 overseas_medical 的地理編碼、DynamoDB 查詢、營業時間與距離計算等函式。
import overseas_medical as medical


LOGGER = logging.getLogger()
LOGGER.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())

# 工具回應 envelope 的版本、資料來源標示，以及輸入長度上限。
SCHEMA_VERSION = "2.0"
SOURCE_SYSTEM = "clinic_service"
MAX_LOCATION_LENGTH = 200
MAX_SYMPTOMS_LENGTH = 500

# 允許的輸入欄位、支援的語言代碼、對外輸出的科別代碼。
ALLOWED_INPUT_KEYS = {"location", "symptoms", "language"}
LANGUAGE_CODES = {"zh", "en", "ja", "ko"}
SPECIALTY_CODES = ("general", "internal_medicine", "emergency", "other")

# 分流結果：一般門診／住院或重症。
ROUTE_GENERAL = "GENERAL_OUTPATIENT"
ROUTE_SEVERE = "HOSPITALIZATION_OR_SEVERE"

# Severe keywords always win over general keywords.
# 重症關鍵字：命中任一個就走 HOSPITALIZATION_OR_SEVERE 路線（優先於一般關鍵字）。
SEVERE_KEYWORDS = (
    "住院",
    "重大",
    "呼吸困難",
    "無法呼吸",
    "喘不過氣",
    "意識不清",
    "失去意識",
    "昏迷",
    "昏倒",
    "大量出血",
    "嚴重出血",
    "血流不止",
    "胸痛",
    "胸悶",
    "中風",
    "半身無力",
    "口齒不清",
    "嘴歪",
    "嚴重過敏",
    "過敏性休克",
    "喉嚨腫脹",
    "hospitalization",
    "hospitalized",
    "can't breathe",
    "cannot breathe",
    "difficulty breathing",
    "unconscious",
    "severe bleeding",
    "heavy bleeding",
    "chest pain",
    "stroke",
    "anaphylaxis",
    "severe allergy",
)

# Keyword -> specialty for the general outpatient route.
# 一般門診關鍵字 → 對應科別（目前都對應內科）。
GENERAL_KEYWORDS: dict[str, str] = {
    "發燒": "internal_medicine",
    "發熱": "internal_medicine",
    "感冒": "internal_medicine",
    "咳嗽": "internal_medicine",
    "流鼻水": "internal_medicine",
    "喉嚨痛": "internal_medicine",
    "頭痛": "internal_medicine",
    "腸胃": "internal_medicine",
    "腹瀉": "internal_medicine",
    "拉肚子": "internal_medicine",
    "胃痛": "internal_medicine",
    "噁心": "internal_medicine",
    "嘔吐": "internal_medicine",
    "fever": "internal_medicine",
    "cold": "internal_medicine",
    "cough": "internal_medicine",
    "sore throat": "internal_medicine",
    "headache": "internal_medicine",
    "diarrhea": "internal_medicine",
    "stomach": "internal_medicine",
    "nausea": "internal_medicine",
    "vomit": "internal_medicine",
}

# 重症時提供的國泰產險 24 小時海外急難救助專線。
EMERGENCY_ASSISTANCE = {
    "provider": "國泰產險 24 小時海外急難救助",
    "phone": "+886-2-2755-1258",
}

# 各國當地緊急電話（依 countryCode）；未列出的國家回傳 None。
LOCAL_EMERGENCY_NUMBERS = {"JP": "119", "KR": "119", "US": "911", "CA": "911", "GB": "999"}

# Versioned advance-payment (代墊) rule. Any change must bump ``version`` and
# be reviewed by the product, claims and compliance owners.
# 代墊（醫療費用由救援公司先行墊付）資格規則：
# 國泰產險海外旅平險、意外醫療與海外突發疾病醫療保額皆 ≥ 120 萬 TWD、
# 需附加旅遊不便險，且僅限日本／韓國。
ADVANCE_PAYMENT_RULE: dict[str, Any] = {
    "version": "2026-11-14",
    "insurer": "CATHAY_CENTURY",
    "productCode": "OVERSEAS_TRAVEL",
    "currency": "TWD",
    "minimumCoverages": {
        "accidentMedical": 1_200_000,
        "overseasSuddenIllnessMedical": 1_200_000,
    },
    "requiredRiders": ("TRAVEL_INCONVENIENCE",),
    "countries": ("JP", "KR"),
    "partner": "聯合國際救援公司",
}

# 重症回應中 Agent 必須原文顯示的最終確認聲明。
FINAL_CONFIRMATION_NOTICE = "由急難救助合作夥伴與當地醫院最終確認"

# 代墊資格判斷結果：初步符合／不符合／無法判斷（缺身分或保單服務失敗）。
STATUS_ELIGIBLE = "PRELIMINARILY_ELIGIBLE"
STATUS_NOT_ELIGIBLE = "NOT_ELIGIBLE"
STATUS_UNDETERMINED = "UNDETERMINED"

# 不可輸出的內部欄位（第二道防線；第一道是 overseas_medical 的 ProjectionExpression）。
NON_PUBLIC_FIELDS = {"internalContact", "contractRate", "internalNote", "notes"}


# 工具錯誤：code、可顯示的訊息、retryable（呼叫端是否可重試）。
class ToolError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


# --------------------------------------------------------------------------- #
# Envelope
# --------------------------------------------------------------------------- #
# 目前 UTC 時間（獨立成函式，方便測試時替換）。
def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


# 轉成 RFC 3339 UTC 字串，例如 2026-11-14T08:00:00Z。
def _rfc3339(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# envelope 的 source 區塊：來源系統、是否為模擬資料（DATA_IS_MOCK）、取得時間。
def _source(retrieved_at: datetime) -> dict[str, Any]:
    return {
        "system": SOURCE_SYSTEM,
        "isMock": medical._env_bool("DATA_IS_MOCK", False),
        "retrievedAt": _rfc3339(retrieved_at),
    }


# 成功 envelope：status=success、data 有值、error=None。
def _success(data: dict[str, Any], retrieved_at: datetime) -> dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "status": "success",
        "source": _source(retrieved_at),
        "data": data,
        "error": None,
    }


# 失敗 envelope：status=error、data=None、error 帶 code / message / retryable。
def _error(exc: ToolError, retrieved_at: datetime) -> dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "status": "error",
        "source": _source(retrieved_at),
        "data": None,
        "error": {"code": exc.code, "message": exc.message, "retryable": exc.retryable},
    }


# --------------------------------------------------------------------------- #
# Input & triage
# --------------------------------------------------------------------------- #
# 驗證工具輸入：只允許 location、symptoms、language。
# location ≤ 200 字、symptoms ≤ 500 字，且都不可空白；language 為 zh / en / ja / ko / any（預設 any）。
def _parse_input(event: Any) -> dict[str, Any]:
    if not isinstance(event, dict):
        raise ToolError("INVALID_ARGUMENT", "工具輸入必須是 JSON 物件")
    unknown = sorted(set(event) - ALLOWED_INPUT_KEYS)
    if unknown:
        raise ToolError("INVALID_ARGUMENT", f"不支援的欄位：{', '.join(unknown)}")

    location = event.get("location")
    if not isinstance(location, str) or not location.strip():
        raise ToolError("INVALID_ARGUMENT", "location 必須是非空字串")
    location = location.strip()
    if len(location) > MAX_LOCATION_LENGTH:
        raise ToolError("INVALID_ARGUMENT", f"location 不可超過 {MAX_LOCATION_LENGTH} 字")

    symptoms = event.get("symptoms")
    if not isinstance(symptoms, str) or not symptoms.strip():
        raise ToolError("INVALID_ARGUMENT", "symptoms 必須是非空字串")
    symptoms = symptoms.strip()
    if len(symptoms) > MAX_SYMPTOMS_LENGTH:
        raise ToolError("INVALID_ARGUMENT", f"symptoms 不可超過 {MAX_SYMPTOMS_LENGTH} 字")

    language = event.get("language", "any")
    if language is None:
        language = "any"
    if language != "any" and language not in LANGUAGE_CODES:
        raise ToolError("INVALID_ARGUMENT", "language 必須是 zh、en、ja、ko 或 any")

    return {"location": location, "symptoms": symptoms, "language": language}


# 症狀分流（純關鍵字，不呼叫 LLM）：
# 命中重症字 → SEVERE，specialty=emergency；
# 否則 → GENERAL，specialty 取第一個命中的一般關鍵字對應科別，都沒命中則為 general。
def triage(symptoms: str) -> dict[str, Any]:
    """Classify symptoms by keyword. Severe keywords take precedence."""
    lowered = symptoms.casefold()
    severe = [term for term in SEVERE_KEYWORDS if term in lowered]
    if severe:
        return {"route": ROUTE_SEVERE, "matchedKeywords": severe, "specialty": "emergency"}
    general = [term for term in GENERAL_KEYWORDS if term in lowered]
    specialty = GENERAL_KEYWORDS[general[0]] if general else "general"
    return {"route": ROUTE_GENERAL, "matchedKeywords": general, "specialty": specialty}


# --------------------------------------------------------------------------- #
# Facility helpers
# --------------------------------------------------------------------------- #
# 營業狀態三態：OPEN / CLOSED / UNKNOWN（時區或營業時間資料缺漏時為 UNKNOWN，而不是直接當作關閉）。
def _opening_status(facility: dict[str, Any], now_utc: datetime) -> str:
    if facility.get("emergency24h") is True:
        return "OPEN"
    timezone_name = facility.get("timezone")
    if not isinstance(timezone_name, str) or not isinstance(facility.get("hours"), dict):
        return "UNKNOWN"
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        return "UNKNOWN"
    return "OPEN" if medical._is_open(facility, now_utc) else "CLOSED"


# 合作是否有效：active=True 且 partnershipStatus=ACTIVE；validUntil 未設定視為無期限，過期或格式錯誤則無效。
def _is_valid_partner(facility: dict[str, Any], today: date) -> bool:
    if facility.get("active") is not True or facility.get("partnershipStatus") != "ACTIVE":
        return False
    valid_until = facility.get("validUntil")
    if valid_until is None:
        return True
    try:
        return date.fromisoformat(str(valid_until)) >= today
    except ValueError:
        return False  # Fail closed on malformed validity.


# 取出並驗證院所經緯度；無效時回傳 None。
def _coordinates(facility: dict[str, Any]) -> tuple[float, float] | None:
    try:
        lat, lon = float(facility["latitude"]), float(facility["longitude"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


# 安全取出字串清單：不是 list 就回傳空清單，並過濾掉非字串元素。
def _list_of_str(value: Any) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


# 是否能處理重症：facilityType=hospital、24 小時急診，或 services 包含 emergency。
def _is_hospital(facility: dict[str, Any]) -> bool:
    services = _list_of_str(facility.get("services"))
    return (
        facility.get("facilityType") == "hospital"
        or facility.get("emergency24h") is True
        or "emergency" in services
    )


# 科別比對：general 需求接受一般科或內科院所；特定科別接受該科或一般科院所。
def _matches_specialty(facility: dict[str, Any], specialty: str) -> bool:
    services = _list_of_str(facility.get("services"))
    if specialty == "general":
        return "general" in services or "internal_medicine" in services
    # Symptoms such as fever/cold can be treated at a general clinic.
    return specialty in services or "general" in services


# 產生候選院所：合作有效 + 座標有效 + 在搜尋半徑內。
# 移除非公開欄位，並附上內部用的 _distanceKm、_openingStatus（底線開頭，輸出前會轉換）。
def _candidates(
    facilities: Iterable[dict[str, Any]],
    latitude: float,
    longitude: float,
    now_utc: datetime,
) -> list[dict[str, Any]]:
    radius_km = medical._env_float("SEARCH_RADIUS_KM", medical.DEFAULT_RADIUS_KM, 1.0, 500.0)
    today = now_utc.date()
    results = []
    for facility in facilities:
        if not _is_valid_partner(facility, today):
            continue
        coordinates = _coordinates(facility)
        if coordinates is None:
            continue
        distance = medical._haversine_km(latitude, longitude, *coordinates)
        if distance > radius_km:
            continue
        item = {key: value for key, value in facility.items() if key not in NON_PUBLIC_FIELDS}
        item["_distanceKm"] = distance
        item["_openingStatus"] = _opening_status(facility, now_utc)
        results.append(item)
    return results


# 轉成對外輸出的 clinic 物件（schema 2.0）；語言與科別只保留白名單代碼。
# advance_flag：該院所是否可協調代墊（None 表示不適用）。
def _public_clinic(item: dict[str, Any], advance_flag: bool | None) -> dict[str, Any]:
    languages = [code for code in _list_of_str(item.get("languages")) if code in LANGUAGE_CODES]
    specialties = [code for code in _list_of_str(item.get("services")) if code in SPECIALTY_CODES]
    clinic = {
        "clinicId": str(item.get("facilityId") or ""),
        "name": str(item.get("name") or ""),
        "address": str(item.get("address") or ""),
        "phone": str(item["phone"]) if item.get("phone") else None,
        "specialties": specialties,
        "languages": languages,
        "isPartner": True,  # Only ACTIVE, unexpired partner records reach this point.
        "openingStatus": item["_openingStatus"],
        "distanceKm": round(float(item["_distanceKm"]), 1),
        "advancePaymentCoordinable": advance_flag,
    }
    return clinic


# 回傳筆數上限（MAX_RESULTS，範圍 1~10）。
def _max_results() -> int:
    return medical._env_int("MAX_RESULTS", medical.DEFAULT_MAX_RESULTS, 1, 10)


# 一般門診：科別符合、不是確定關閉（UNKNOWN 仍保留）、語言符合，依距離排序取前 N 筆。
def general_clinics(
    candidates: list[dict[str, Any]], specialty: str, language: str
) -> list[dict[str, Any]]:
    selected = [
        item
        for item in candidates
        if _matches_specialty(item, specialty)
        and item["_openingStatus"] != "CLOSED"
        and (language == "any" or language in _list_of_str(item.get("languages")))
    ]
    selected.sort(key=lambda item: item["_distanceKm"])
    return [_public_clinic(item, None) for item in selected[: _max_results()]]


# 重症：只挑醫院／急診院所，並比對語言。
# 代墊初步符合 → 支援第三方直接帳單（thirdPartyDirectBilling）的醫院優先，再依距離；
# 否則只依距離排序，且不標示代墊。
def severe_hospitals(
    candidates: list[dict[str, Any]], language: str, eligible: bool
) -> list[dict[str, Any]]:
    selected = [
        item
        for item in candidates
        if _is_hospital(item)
        and (language == "any" or language in _list_of_str(item.get("languages")))
    ]

    def coordinable(item: dict[str, Any]) -> bool:
        return item.get("thirdPartyDirectBilling") is True

    if eligible:
        selected.sort(key=lambda item: (not coordinable(item), item["_distanceKm"]))
        return [_public_clinic(item, coordinable(item)) for item in selected[: _max_results()]]
    selected.sort(key=lambda item: item["_distanceKm"])
    return [_public_clinic(item, None) for item in selected[: _max_results()]]


# --------------------------------------------------------------------------- #
# Identity, policy service and versioned rule
# --------------------------------------------------------------------------- #
# 只從可信的呼叫 context（client_context.custom[TRUSTED_IDENTITY_CONTEXT_KEY]）取得使用者 ID；
# 絕不採用工具輸入；未設定 key 時回傳 None。
def trusted_user_id(context: Any) -> str | None:
    """Read the user id only from the trusted invocation context.

    The key must be configured with ``TRUSTED_IDENTITY_CONTEXT_KEY`` after the
    Gateway identity propagation has been verified end to end. Tool input is
    never consulted and there is no ``default_actor`` fallback.
    """
    key = os.getenv("TRUSTED_IDENTITY_CONTEXT_KEY", "").strip()
    if not key:
        return None
    client_context = getattr(context, "client_context", None)
    custom = getattr(client_context, "custom", None)
    if not isinstance(custom, dict):
        return None
    value = custom.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


# 同步呼叫保單服務 Lambda（POLICY_SERVICE_FUNCTION）取得使用者保單；
# 相容 API Gateway 格式（body 為字串）；任何錯誤或格式不符都會拋出例外。
def fetch_policies(user_id: str) -> list[dict[str, Any]]:
    """Call the policy service Lambda. Raises on any downstream failure."""
    function_name = os.getenv("POLICY_SERVICE_FUNCTION", "").strip()
    if not function_name:
        raise RuntimeError("POLICY_SERVICE_FUNCTION is not configured")
    client = medical._aws_client("lambda")
    response = client.invoke(
        FunctionName=function_name,
        InvocationType="RequestResponse",
        Payload=json.dumps({"userId": user_id}).encode("utf-8"),
    )
    if response.get("FunctionError"):
        raise RuntimeError("policy service function error")
    payload = json.loads(response["Payload"].read())
    if isinstance(payload, dict) and isinstance(payload.get("body"), str):
        payload = json.loads(payload["body"])
    policies = payload.get("policies") if isinstance(payload, dict) else None
    if not isinstance(policies, list):
        raise RuntimeError("policy service returned an unexpected shape")
    return [policy for policy in policies if isinstance(policy, dict)]


# 保額轉成 float；無法解析視為 0（低於門檻，fail-closed）。
def _amount(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


# 保單是否生效：status=ACTIVE，且今天介於 coverageStart 與 coverageEnd 之間。
def _policy_in_force(policy: dict[str, Any], today: date) -> bool:
    if policy.get("status") != "ACTIVE":
        return False
    try:
        start = date.fromisoformat(str(policy.get("coverageStart")))
        end = date.fromisoformat(str(policy.get("coverageEnd")))
    except ValueError:
        return False
    return start <= today <= end


# 依 ADVANCE_PAYMENT_RULE 評估代墊資格：
# - 國家不在 JP / KR → 不符合
# - 沒有生效中的國泰旅平險 → 不符合
# - 任一張保單同時符合幣別、兩項保額門檻與必要附約 → 初步符合
# - 否則彙整所有保單的不符原因
def evaluate_advance_payment(
    policies: list[dict[str, Any]], country_code: str | None, today: date
) -> dict[str, Any]:
    rule = ADVANCE_PAYMENT_RULE
    if country_code not in rule["countries"]:
        return {"status": STATUS_NOT_ELIGIBLE, "reasons": ["COUNTRY_NOT_COVERED"]}

    reasons: set[str] = set()
    travel_policies = [
        policy
        for policy in policies
        if policy.get("insurer") == rule["insurer"]
        and policy.get("productCode") == rule["productCode"]
        and _policy_in_force(policy, today)
    ]
    if not travel_policies:
        return {"status": STATUS_NOT_ELIGIBLE, "reasons": ["NO_ACTIVE_CATHAY_TRAVEL_POLICY"]}

    for policy in travel_policies:
        policy_reasons = []
        coverages = policy.get("coverages") if isinstance(policy.get("coverages"), dict) else {}
        if policy.get("currency", rule["currency"]) != rule["currency"]:
            policy_reasons.append("UNSUPPORTED_CURRENCY")
        if _amount(coverages.get("accidentMedical")) < rule["minimumCoverages"]["accidentMedical"]:
            policy_reasons.append("ACCIDENT_MEDICAL_BELOW_THRESHOLD")
        if (
            _amount(coverages.get("overseasSuddenIllnessMedical"))
            < rule["minimumCoverages"]["overseasSuddenIllnessMedical"]
        ):
            policy_reasons.append("SUDDEN_ILLNESS_MEDICAL_BELOW_THRESHOLD")
        riders = _list_of_str(policy.get("riders"))
        if any(rider not in riders for rider in rule["requiredRiders"]):
            policy_reasons.append("TRAVEL_INCONVENIENCE_RIDER_MISSING")
        if not policy_reasons:
            return {"status": STATUS_ELIGIBLE, "reasons": []}
        reasons.update(policy_reasons)
    return {"status": STATUS_NOT_ELIGIBLE, "reasons": sorted(reasons)}


# 組合代墊資訊：規則版本、合作救援公司、需醫院同意等固定欄位 + 評估結果。
# 沒有可信身分或保單服務失敗 → UNDETERMINED，絕不因此阻擋急難指引。
def _advance_payment(context: Any, country_code: str | None, today: date) -> dict[str, Any]:
    rule = ADVANCE_PAYMENT_RULE
    base = {
        "ruleVersion": rule["version"],
        "partner": rule["partner"],
        "requiresHospitalConsent": True,
        "finalConfirmationBy": f"{rule['partner']}與當地醫院",
    }
    user_id = trusted_user_id(context)
    if user_id is None:
        return {**base, "status": STATUS_UNDETERMINED, "reasons": ["IDENTITY_UNAVAILABLE"]}
    try:
        policies = fetch_policies(user_id)
    except Exception as exc:  # Never block emergency guidance on policy lookup.
        LOGGER.warning("policy_lookup_failed error_type=%s", type(exc).__name__)
        return {**base, "status": STATUS_UNDETERMINED, "reasons": ["POLICY_SERVICE_UNAVAILABLE"]}
    return {**base, **evaluate_advance_payment(policies, country_code, today)}


# --------------------------------------------------------------------------- #
# Handler
# --------------------------------------------------------------------------- #
# 追蹤用 request ID：Lambda aws_request_id，否則隨機 UUID。
def _request_id(context: Any) -> str:
    return str(getattr(context, "aws_request_id", None) or uuid.uuid4())


# 一般門診流程：地理編碼 → 查詢該國院所 → 篩選候選 → 依科別與語言挑選最近的院所。
# 地點無法解析或查無院所時回傳 NOT_FOUND 錯誤。
def _handle_general(request: dict[str, Any], triaged: dict[str, Any], now_utc: datetime) -> dict[str, Any]:
    geocoded = medical._geocode(request["location"])
    if not geocoded or not geocoded.get("countryCode"):
        raise ToolError("NOT_FOUND", "無法解析目前所在地，請提供城市、地標或完整地址")
    candidates = _candidates(
        medical._query_facilities(geocoded["countryCode"]),
        float(geocoded["latitude"]),
        float(geocoded["longitude"]),
        now_utc,
    )
    clinics = general_clinics(candidates, triaged["specialty"], request["language"])
    if not clinics:
        raise ToolError("NOT_FOUND", "查無符合條件的醫療院所")
    return {
        "queriedLocation": request["location"],
        "countryCode": geocoded["countryCode"],
        "triage": triaged,
        "emergencyAssistance": None,
        "advancePayment": None,
        "displayNotice": None,
        "clinics": clinics,
    }


# 重症流程：一定回傳急難救助專線與代墊資訊。
# 地理編碼或院所查詢失敗只記 warning，不影響急難指引（此時 clinics 可能是空陣列）。
def _handle_severe(
    request: dict[str, Any], triaged: dict[str, Any], context: Any, now_utc: datetime
) -> dict[str, Any]:
    # Emergency guidance must survive any downstream failure.
    geocoded: dict[str, Any] | None = None
    try:
        geocoded = medical._geocode(request["location"])
    except Exception as exc:
        LOGGER.warning("severe_geocode_failed error_type=%s", type(exc).__name__)
    country_code = (geocoded or {}).get("countryCode")

    advance = _advance_payment(context, country_code, now_utc.date())
    clinics: list[dict[str, Any]] = []
    if geocoded and country_code:
        try:
            candidates = _candidates(
                medical._query_facilities(country_code),
                float(geocoded["latitude"]),
                float(geocoded["longitude"]),
                now_utc,
            )
            clinics = severe_hospitals(
                candidates, request["language"], advance["status"] == STATUS_ELIGIBLE
            )
        except Exception as exc:
            LOGGER.warning("severe_facility_lookup_failed error_type=%s", type(exc).__name__)

    return {
        "queriedLocation": request["location"],
        "countryCode": country_code,
        "triage": triaged,
        "emergencyAssistance": {
            **EMERGENCY_ASSISTANCE,
            "localEmergencyNumber": LOCAL_EMERGENCY_NUMBERS.get(country_code or ""),
        },
        "advancePayment": advance,
        # The Agent must show this text verbatim below the recommended facilities.
        "displayNotice": FINAL_CONFIRMATION_NOTICE,
        "clinics": clinics,
    }


# Lambda 入口（AgentCore Gateway Target）：驗證輸入 → 症狀分流 → 一般／重症流程 → 回傳 schema 2.0 envelope。
# log 只記錄路線、國家、筆數與代墊狀態，不記錄症狀、地點或保單內容。
def lambda_handler(event: Any, context: Any) -> dict[str, Any]:
    """Return the get-clinic-info v2.0 envelope as a JSON-serialisable dict."""
    now_utc = _now_utc()
    request_id = _request_id(context)
    try:
        request = _parse_input(event)
        triaged = triage(request["symptoms"])
        if triaged["route"] == ROUTE_SEVERE:
            data = _handle_severe(request, triaged, context, now_utc)
        else:
            data = _handle_general(request, triaged, now_utc)
        LOGGER.info(
            "clinic_tool request_id=%s outcome=success route=%s country=%s returned=%d advance=%s",
            request_id,
            triaged["route"],
            data.get("countryCode"),
            len(data["clinics"]),
            (data.get("advancePayment") or {}).get("status"),
        )
        return _success(data, now_utc)
    # 預期錯誤（輸入錯誤、查無結果）：回傳不可重試的錯誤 envelope。
    except ToolError as exc:
        LOGGER.info("clinic_tool request_id=%s outcome=error code=%s", request_id, exc.code)
        return _error(exc, now_utc)
    # 非預期錯誤：只記錄錯誤代碼，回傳可重試的 SERVICE_UNAVAILABLE。
    except Exception as exc:
        error_response = getattr(exc, "response", None)
        error_object = error_response.get("Error", {}) if isinstance(error_response, dict) else {}
        # operation 為 botocore 的 API 名稱（如 Geocode、Query），方便判斷是哪個 IAM 權限不足。
        LOGGER.error(
            "clinic_tool request_id=%s outcome=failed operation=%s error_code=%s",
            request_id,
            str(getattr(exc, "operation_name", None) or "local"),
            str(error_object.get("Code") or type(exc).__name__),
        )
        return _error(
            ToolError("SERVICE_UNAVAILABLE", "醫療院所服務暫時無法使用，請稍後再試", True),
            now_utc,
        )
