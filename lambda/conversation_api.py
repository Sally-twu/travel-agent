"""Chat API router and DynamoDB conversation persistence.

Routes:
* POST /v1/chat/messages
* GET  /v1/conversations
* GET  /v1/conversations/{conversationId}

POST delegates response generation to overseas_medical.lambda_handler, then
atomically stores the USER and ASSISTANT messages in a separate DynamoDB table.
The two GET routes expose only conversations owned by the authenticated user.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import uuid
from typing import Any

import overseas_medical


LOGGER = logging.getLogger()
LOGGER.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())

DEFAULT_LIST_LIMIT = 20
MAX_LIST_LIMIT = 50
DEFAULT_MESSAGE_LIMIT = 50
MAX_MESSAGE_LIMIT = 100
CURSOR_MAX_LENGTH = 4_096


class ApiError(Exception):
    """A client-safe API error."""

    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _request_id(event: dict[str, Any], context: Any) -> str:
    request_context = event.get("requestContext")
    if isinstance(request_context, dict) and request_context.get("requestId"):
        return str(request_context["requestId"])
    return str(getattr(context, "aws_request_id", None) or uuid.uuid4())


def _response(status_code: int, payload: dict[str, Any]) -> dict[str, Any]:
    headers = {
        "content-type": "application/json; charset=utf-8",
        "cache-control": "no-store",
        "x-content-type-options": "nosniff",
    }
    allowed_origin = os.getenv("ALLOWED_ORIGIN", "").strip()
    if allowed_origin:
        headers["access-control-allow-origin"] = allowed_origin
    return {
        "statusCode": status_code,
        "headers": headers,
        "body": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    }


def _principal_id(event: dict[str, Any]) -> str:
    """Read identity only from the trusted API Gateway authorizer context."""

    request_context = event.get("requestContext") or {}
    authorizer = request_context.get("authorizer") or {}
    jwt_claims = (authorizer.get("jwt") or {}).get("claims") or {}
    legacy_claims = authorizer.get("claims") or {}
    principal = (
        jwt_claims.get("sub")
        or legacy_claims.get("sub")
        or authorizer.get("principalId")
    )
    if principal:
        return str(principal)
    if _env_bool("REQUIRE_AUTH_CONTEXT", True):
        raise ApiError("UNAUTHORIZED", "尚未通過身分驗證。", 401)
    # This fallback is only for a Lambda Console/local test before JWT is wired.
    return os.getenv("DEV_USER_ID", "local-dev-user").strip() or "local-dev-user"


def _method_and_path(event: dict[str, Any]) -> tuple[str, str]:
    request_context = event.get("requestContext") or {}
    http_context = request_context.get("http") or {}
    method = str(http_context.get("method") or event.get("httpMethod") or "").upper()
    path = str(event.get("rawPath") or http_context.get("path") or event.get("path") or "")
    if not method and ("body" in event or "text" in event):
        method = "POST"
        path = "/v1/chat/messages"
    return method, path.rstrip("/") or "/"


def _table_name() -> str:
    table_name = os.getenv("CONVERSATIONS_TABLE", "").strip()
    if not table_name:
        raise RuntimeError("CONVERSATIONS_TABLE is required")
    return table_name


def _table():
    import boto3  # type: ignore

    return boto3.resource("dynamodb", region_name=os.getenv("AWS_REGION")).Table(_table_name())


def _ddb_client():
    import boto3  # type: ignore

    return boto3.client("dynamodb", region_name=os.getenv("AWS_REGION"))


def _serialize_item(item: dict[str, Any]) -> dict[str, Any]:
    from boto3.dynamodb.types import TypeSerializer  # type: ignore

    serializer = TypeSerializer()
    return {key: serializer.serialize(value) for key, value in item.items()}


def _serialize_values(values: dict[str, Any]) -> dict[str, Any]:
    from boto3.dynamodb.types import TypeSerializer  # type: ignore

    serializer = TypeSerializer()
    return {key: serializer.serialize(value) for key, value in values.items()}


def _conversation_pk(conversation_id: str) -> str:
    return f"CONV#{conversation_id}"


def _user_gsi_pk(principal_id: str) -> str:
    return f"USER#{principal_id}"


def _validate_uuid(value: Any) -> str:
    if not isinstance(value, str):
        raise ApiError("INVALID_CONVERSATION_ID", "conversationId 必須是有效的 UUID。")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        raise ApiError("INVALID_CONVERSATION_ID", "conversationId 必須是有效的 UUID。") from None
    if str(parsed) != value.lower():
        raise ApiError("INVALID_CONVERSATION_ID", "conversationId 必須使用標準 UUID 格式。")
    return value


def _query_parameters(event: dict[str, Any]) -> dict[str, str]:
    values = event.get("queryStringParameters") or {}
    return values if isinstance(values, dict) else {}


def _limit(event: dict[str, Any], default: int, maximum: int) -> int:
    raw = _query_parameters(event).get("limit")
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ApiError("INVALID_LIMIT", f"limit 必須是 1–{maximum} 的整數。") from None
    if not 1 <= value <= maximum:
        raise ApiError("INVALID_LIMIT", f"limit 必須是 1–{maximum} 的整數。")
    return value


def _encode_cursor(last_key: dict[str, Any] | None) -> str | None:
    if not last_key:
        return None
    raw = json.dumps(last_key, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(raw_cursor: str | None, required_keys: set[str]) -> dict[str, str] | None:
    if not raw_cursor:
        return None
    if not isinstance(raw_cursor, str) or len(raw_cursor) > CURSOR_MAX_LENGTH:
        raise ApiError("INVALID_CURSOR", "cursor 無效或已過期。")
    try:
        padding = "=" * (-len(raw_cursor) % 4)
        decoded = base64.urlsafe_b64decode((raw_cursor + padding).encode("ascii"))
        value = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeError, json.JSONDecodeError):
        raise ApiError("INVALID_CURSOR", "cursor 無效或已過期。") from None
    if (
        not isinstance(value, dict)
        or set(value) != required_keys
        or not all(isinstance(item, str) for item in value.values())
    ):
        raise ApiError("INVALID_CURSOR", "cursor 無效或已過期。")
    return value


def _get_owned_meta(conversation_id: str, principal_id: str) -> dict[str, Any]:
    response = _table().get_item(
        Key={"PK": _conversation_pk(conversation_id), "SK": "META"},
        ConsistentRead=True,
    )
    item = response.get("Item")
    if not isinstance(item, dict) or item.get("ownerId") != principal_id:
        # Use the same response for missing and foreign conversations.
        raise ApiError("CONVERSATION_NOT_FOUND", "找不到指定的對話。", 404)
    return item


def _plain_preview(value: Any, max_length: int = 160) -> str:
    text = str(value or "")
    text = re.sub(r"[`*_#>\[\]|]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_length]


def _message_item(
    conversation_id: str,
    timestamp: str,
    order: int,
    role: str,
    *,
    text: str | None = None,
    content: dict[str, Any] | None = None,
) -> dict[str, Any]:
    message_id = str(uuid.uuid4())
    item: dict[str, Any] = {
        "PK": _conversation_pk(conversation_id),
        "SK": f"MSG#{timestamp}#{order}#{message_id}",
        "entityType": "MESSAGE",
        "messageId": message_id,
        "conversationId": conversation_id,
        "role": role,
        "timestamp": timestamp,
    }
    if role == "USER":
        item["text"] = str(text or "")
    else:
        item["content"] = content or {"text": "", "tasks": [], "question": None}
    return item


def _persist_exchange(
    principal_id: str,
    conversation_id: str,
    title: str,
    user_text: str,
    user_timestamp: str,
    assistant_message: dict[str, Any],
    is_new: bool,
) -> None:
    assistant_timestamp = str(assistant_message["timestamp"])
    assistant_content = assistant_message.get("content") or {}
    preview = _plain_preview(assistant_content.get("text"))
    user_item = _message_item(
        conversation_id,
        user_timestamp,
        0,
        "USER",
        text=user_text,
    )
    assistant_item = _message_item(
        conversation_id,
        assistant_timestamp,
        1,
        "ASSISTANT",
        content=assistant_content,
    )
    table_name = _table_name()
    operations: list[dict[str, Any]] = []

    if is_new:
        meta_item = {
            "PK": _conversation_pk(conversation_id),
            "SK": "META",
            "entityType": "CONVERSATION",
            "conversationId": conversation_id,
            "ownerId": principal_id,
            "title": title,
            "preview": preview,
            "createdAt": user_timestamp,
            "updatedAt": assistant_timestamp,
            "GSI1PK": _user_gsi_pk(principal_id),
            "GSI1SK": f"{assistant_timestamp}#{conversation_id}",
        }
        operations.append(
            {
                "Put": {
                    "TableName": table_name,
                    "Item": _serialize_item(meta_item),
                    "ConditionExpression": "attribute_not_exists(PK)",
                }
            }
        )
    else:
        operations.append(
            {
                "Update": {
                    "TableName": table_name,
                    "Key": _serialize_item(
                        {"PK": _conversation_pk(conversation_id), "SK": "META"}
                    ),
                    "UpdateExpression": (
                        "SET #preview = :preview, updatedAt = :updated, GSI1SK = :gsi"
                    ),
                    "ConditionExpression": "ownerId = :owner",
                    "ExpressionAttributeNames": {"#preview": "preview"},
                    "ExpressionAttributeValues": _serialize_values(
                        {
                            ":preview": preview,
                            ":updated": assistant_timestamp,
                            ":gsi": f"{assistant_timestamp}#{conversation_id}",
                            ":owner": principal_id,
                        }
                    ),
                }
            }
        )

    for item in (user_item, assistant_item):
        operations.append(
            {
                "Put": {
                    "TableName": table_name,
                    "Item": _serialize_item(item),
                    "ConditionExpression": "attribute_not_exists(PK)",
                }
            }
        )

    token_source = f"{conversation_id}|{user_timestamp}|{assistant_timestamp}"
    request_token = hashlib.sha256(token_source.encode("utf-8")).hexdigest()[:36]
    _ddb_client().transact_write_items(
        TransactItems=operations,
        ClientRequestToken=request_token,
    )


def _parse_proxy_body(response: dict[str, Any]) -> dict[str, Any]:
    raw_body = response.get("body")
    if not isinstance(raw_body, str):
        raise RuntimeError("Medical handler returned an invalid body")
    value = json.loads(raw_body)
    if not isinstance(value, dict):
        raise RuntimeError("Medical handler returned a non-object body")
    return value


def _handle_post(
    event: dict[str, Any],
    context: Any,
    principal_id: str,
) -> dict[str, Any]:
    # Reuse the medical handler's strict public request validation.
    request = overseas_medical._parse_body(event)
    existing_meta: dict[str, Any] | None = None
    if request["conversationId"] is not None:
        existing_meta = _get_owned_meta(request["conversationId"], principal_id)

    medical_response = overseas_medical.lambda_handler(event, context)
    if int(medical_response.get("statusCode", 500)) != 200:
        return medical_response

    payload = _parse_proxy_body(medical_response)
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("assistantMessage"), dict):
        raise RuntimeError("Medical handler returned an invalid success contract")

    is_new = request["conversationId"] is None
    conversation_id = (
        str(data.get("conversationId")) if is_new else str(request["conversationId"])
    )
    _validate_uuid(conversation_id)
    title = str(data.get("title")) if is_new else str(existing_meta.get("title", "海外就醫"))
    user_timestamp = str(data["userTimestamp"])
    _persist_exchange(
        principal_id=principal_id,
        conversation_id=conversation_id,
        title=title,
        user_text=request["text"],
        user_timestamp=user_timestamp,
        assistant_message=data["assistantMessage"],
        is_new=is_new,
    )
    return medical_response


def _handle_list(event: dict[str, Any], principal_id: str) -> dict[str, Any]:
    limit = _limit(event, DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT)
    query = _query_parameters(event)
    cursor = _decode_cursor(
        query.get("cursor"),
        {"PK", "SK", "GSI1PK", "GSI1SK"},
    )
    expected_gsi_pk = _user_gsi_pk(principal_id)
    if cursor and cursor.get("GSI1PK") != expected_gsi_pk:
        raise ApiError("INVALID_CURSOR", "cursor 無效或已過期。")

    kwargs: dict[str, Any] = {
        "IndexName": os.getenv("CONVERSATIONS_USER_INDEX", "GSI1"),
        "KeyConditionExpression": "GSI1PK = :user",
        "ExpressionAttributeValues": {":user": expected_gsi_pk},
        "ScanIndexForward": False,
        "Limit": limit,
    }
    if cursor:
        kwargs["ExclusiveStartKey"] = cursor
    result = _table().query(**kwargs)
    items = []
    for item in result.get("Items", []):
        items.append(
            {
                "conversationId": str(item.get("conversationId", "")),
                "title": str(item.get("title", "")),
                "preview": str(item.get("preview", "")),
                "updatedAt": str(item.get("updatedAt", "")),
            }
        )
    return _response(
        200,
        {
            "data": {
                "items": items,
                "nextCursor": _encode_cursor(result.get("LastEvaluatedKey")),
            }
        },
    )


def _history_assistant_content(value: Any) -> dict[str, Any]:
    content = value if isinstance(value, dict) else {}
    question_value = content.get("question")
    question = None
    if isinstance(question_value, dict):
        question = {
            "text": str(question_value.get("text", "")),
            # api/07-chat.md: history never returns old suggestion buttons.
            "suggestedReplies": [],
        }
    return {
        "text": str(content.get("text", "")),
        "tasks": content.get("tasks") if isinstance(content.get("tasks"), list) else [],
        "question": question,
    }


def _handle_detail(
    event: dict[str, Any],
    principal_id: str,
    conversation_id: str,
) -> dict[str, Any]:
    conversation_id = _validate_uuid(conversation_id)
    meta = _get_owned_meta(conversation_id, principal_id)
    limit = _limit(event, DEFAULT_MESSAGE_LIMIT, MAX_MESSAGE_LIMIT)
    cursor = _decode_cursor(_query_parameters(event).get("cursor"), {"PK", "SK"})
    expected_pk = _conversation_pk(conversation_id)
    if cursor and cursor.get("PK") != expected_pk:
        raise ApiError("INVALID_CURSOR", "cursor 無效或已過期。")

    kwargs: dict[str, Any] = {
        "KeyConditionExpression": "PK = :pk AND begins_with(SK, :prefix)",
        "ExpressionAttributeValues": {":pk": expected_pk, ":prefix": "MSG#"},
        "ScanIndexForward": False,
        "Limit": limit,
    }
    if cursor:
        kwargs["ExclusiveStartKey"] = cursor
    result = _table().query(**kwargs)

    messages: list[dict[str, Any]] = []
    # DynamoDB returns newest first; the frontend contract requires old to new.
    for item in reversed(result.get("Items", [])):
        role = item.get("role")
        if role == "USER":
            messages.append(
                {
                    "role": "USER",
                    "text": str(item.get("text", "")),
                    "timestamp": str(item.get("timestamp", "")),
                }
            )
        elif role == "ASSISTANT":
            messages.append(
                {
                    "role": "ASSISTANT",
                    "timestamp": str(item.get("timestamp", "")),
                    "content": _history_assistant_content(item.get("content")),
                }
            )

    return _response(
        200,
        {
            "data": {
                "title": str(meta.get("title", "")),
                "messages": messages,
                "nextCursor": _encode_cursor(result.get("LastEvaluatedKey")),
            }
        },
    )


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    request_id = _request_id(event if isinstance(event, dict) else {}, context)
    stage = "route"
    try:
        if not isinstance(event, dict):
            raise ApiError("INVALID_REQUEST", "請提供有效的請求。")
        method, path = _method_and_path(event)
        if method == "OPTIONS":
            return _response(204, {})

        stage = "authenticate"
        principal_id = _principal_id(event)
        if method == "POST" and path == "/v1/chat/messages":
            stage = "post_and_persist"
            return _handle_post(event, context, principal_id)
        if method == "GET" and path == "/v1/conversations":
            stage = "list_conversations"
            return _handle_list(event, principal_id)
        if method == "GET":
            path_parameters = event.get("pathParameters") or {}
            conversation_id = path_parameters.get("conversationId")
            if not conversation_id:
                match = re.fullmatch(r"/v1/conversations/([^/]+)", path)
                conversation_id = match.group(1) if match else None
            if conversation_id:
                stage = "conversation_detail"
                return _handle_detail(event, principal_id, str(conversation_id))
        raise ApiError("ROUTE_NOT_FOUND", "找不到指定的 API。", 404)

    except (ApiError, overseas_medical.RequestError) as exc:
        status_code = int(getattr(exc, "status_code", 400))
        code = str(getattr(exc, "code", "INVALID_REQUEST"))
        message = str(getattr(exc, "message", "請求格式不正確。"))
        LOGGER.info(
            "conversation_api request_id=%s outcome=rejected stage=%s code=%s",
            request_id,
            stage,
            code,
        )
        return _response(
            status_code,
            {"error": {"code": code, "message": message, "requestId": request_id}},
        )
    except Exception as exc:
        error_response = getattr(exc, "response", None)
        error_object = error_response.get("Error", {}) if isinstance(error_response, dict) else {}
        error_code = str(error_object.get("Code") or type(exc).__name__)
        operation = str(getattr(exc, "operation_name", None) or "local")
        LOGGER.error(
            "conversation_api request_id=%s outcome=failed stage=%s operation=%s error_code=%s",
            request_id,
            stage,
            operation,
            error_code,
        )
        return _response(
            500,
            {
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "服務暫時無法使用，請稍後再試。",
                    "requestId": request_id,
                }
            },
        )
