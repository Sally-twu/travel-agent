# 海外就醫 `get-clinic-info` 2.0 開發交接說明

> 對象：開發 Bedrock Agent 的金控同仁、負責前後端 API 的 IT 同仁
> 更新日期：2026-11-14

## 1. 目前進度

| 項目 | 狀態 | 說明 |
|---|---|---|
| 症狀分流（一般門診／重症） | ✅ 完成 | 依關鍵字分流，結果可重現，不經 LLM |
| 一般門診：查附近合作院所 | ✅ 完成 | 地點 → 座標 → 篩選科別／營業／語言 → 依距離排序 |
| 重症：急難救助資訊 | ✅ 完成 | 一律回傳國泰產險 24h 海外急難救助專線與當地緊急電話 |
| AgentCore Gateway Target 串接 | ✅ 完成 | Agent 可正常對話並呼叫工具 |
| JWT 身分傳遞 | ⏳ 未做 | 因此代墊資格一律回 `UNDETERMINED` |
| 重症代墊資格判斷 | ⏳ 未上線 | 程式已寫好，需要 JWT 身分＋保單服務才會生效 |
| DynamoDB 院所資料 | ⚠️ 範例資料 | 需依真實合作院所資料調整 |

架構：

```text
前端 ──POST /v1/chat/messages──▶ API Gateway ──▶ Chat Lambda ──▶ AgentCore Runtime (Bedrock Agent)
                                                                   │
                                                                   ▼ MCP
                                                   AgentCore Gateway (target: clinic)
                                                                   │
                                                                   ▼
                                       Lambda travel-overseas-medical-tool:live
                                        ├─ Amazon Location（地點轉座標）
                                        ├─ DynamoDB travel-partner-facilities
                                        └─ 保單服務 Lambda（下一步）
```

程式：[`lambda/overseas_medical_tool.py`](../lambda/overseas_medical_tool.py)（工具本體）、[`lambda/overseas_medical.py`](../lambda/overseas_medical.py)（共用：地點轉座標、DynamoDB 查詢、營業時間）。

---

## 2. Tool Input Schema（給 Agent 開發）

Agent 看到的工具名稱帶 Target 前綴：**`clinic___get-clinic-info`**。

| 欄位 | 型別 | 必填 | 說明 |
|---|---|---|---|
| `location` | string | ✅ | 使用者**目前所在地**（≤ 200 字），例如 `Tokyo Shinjuku`。不可用行程上的旅遊目的地代替 |
| `symptoms` | string | ✅ | 使用者自述症狀（≤ 500 字），照原意填，不加重也不淡化 |
| `language` | string | | `zh`／`en`／`ja`／`ko`／`any`；使用者沒表達偏好就**不要傳** |

```json
{"location": "Tokyo Shinjuku", "symptoms": "發燒、喉嚨痛"}
```

- 只接受以上三個欄位，多傳任何欄位（例如 `userId`、保額）會回 `INVALID_ARGUMENT`。
- 不要傳科別：工具會依症狀自行判斷。
- 使用者的身分與保單**不由 Agent 傳入**，由工具從可信身分自行查詢（見第 5 節）。

### Agent 呼叫前與回覆時的規則（請放入 system prompt）

1. 缺少「目前所在地」或「症狀」時先追問，不要呼叫工具。
2. `triage.route = HOSPITALIZATION_OR_SEVERE` 時，**第一句**先給當地緊急電話（`emergencyAssistance.localEmergencyNumber`）和國泰產險 24 小時海外急難救助電話 `+886-2-2755-1258`，再列醫院。
3. 只有 `advancePayment.status = PRELIMINARILY_ELIGIBLE` **且**該院所 `advancePaymentCoordinable = true` 時，才能說「可協調代墊」。其他情況一律不提代墊，也不要推測或說出保額。
4. `displayNotice` 不為 `null` 時，要在院所清單正下方原文顯示，前面加「※ 」。
5. `status = error` 或 `clinics` 為空時，不可自行編造院所。

---

## 3. Tool Output Schema

所有回應都是同一個外層結構（Lambda 直接回傳物件，沒有包 `statusCode`／`body`）：

| 欄位 | 型別 | 說明 |
|---|---|---|
| `schemaVersion` | string | 固定 `"2.0"` |
| `status` | string | `success`／`error` |
| `source` | object | `system`（固定 `clinic_service`）、`isMock`（bool）、`retrievedAt`（UTC 時間） |
| `data` | object \| null | 成功時有值 |
| `error` | object \| null | 失敗時有值：`code`、`message`、`retryable` |

### 3.1 `data` 欄位

| 欄位 | 一般門診 | 重症 | 說明 |
|---|---|---|---|
| `queriedLocation` | ✔ | ✔ | 傳入的 `location` |
| `countryCode` | ✔ | ✔／`null` | 解析出的國家代碼，例如 `JP`；重症解析失敗時為 `null` |
| `triage` | ✔ | ✔ | `route`、`matchedKeywords`（命中的症狀關鍵字）、`specialty` |
| `emergencyAssistance` | `null` | ✔ | `provider`、`phone`、`localEmergencyNumber`（JP/KR `119`、US/CA `911`、GB `999`，其他國家 `null`） |
| `advancePayment` | `null` | ✔ | 代墊初步資格，見 3.3 |
| `displayNotice` | `null` | ✔ | 固定為「由急難救助合作夥伴與當地醫院最終確認」 |
| `clinics` | ✔ | ✔ | 院所清單，最多 `MAX_RESULTS` 筆（預設 3）；重症時可能是空陣列 |

`triage.route`：

| 值 | 觸發條件（節錄） | `specialty` |
|---|---|---|
| `HOSPITALIZATION_OR_SEVERE` | 住院、呼吸困難、意識不清、大量出血、胸痛、中風、嚴重過敏、chest pain… | `emergency` |
| `GENERAL_OUTPATIENT` | 發燒、感冒、咳嗽、喉嚨痛、頭痛、腹瀉、fever… | `internal_medicine` |
| `GENERAL_OUTPATIENT` | 未命中任何關鍵字 | `general` |

只要命中任一重症關鍵字就走重症路線，即使同時有輕症關鍵字。

### 3.2 `clinics[]` 欄位

| 欄位 | 型別 | 說明 |
|---|---|---|
| `clinicId` | string | 院所 ID |
| `name`、`address` | string | 院所名稱、地址 |
| `phone` | string \| null | 院所電話 |
| `specialties` | string[] | 只輸出 `general`、`internal_medicine`、`emergency`、`other` |
| `languages` | string[] | 只輸出 `zh`、`en`、`ja`、`ko` |
| `isPartner` | bool | 固定 `true`（未通過合作檢查的院所不會出現） |
| `openingStatus` | string | `OPEN`／`CLOSED`／`UNKNOWN`（資料不足時為 `UNKNOWN`，不可說營業中） |
| `distanceKm` | number | 直線距離（公里，小數一位） |
| `advancePaymentCoordinable` | bool \| null | 是否可協調代墊；只有代墊初步符合時才是 `true`／`false`，其他情況 `null` |

### 3.3 `advancePayment`（只在重症路線出現）

| 欄位 | 說明 |
|---|---|
| `status` | `PRELIMINARILY_ELIGIBLE`（初步符合）／`NOT_ELIGIBLE`（不符合）／`UNDETERMINED`（無法判斷） |
| `reasons` | 不符合或無法判斷的原因代碼，例如 `IDENTITY_UNAVAILABLE`、`COUNTRY_NOT_COVERED` |
| `ruleVersion` | 代墊規則版本，目前 `2026-11-14` |
| `partner` | `聯合國際救援公司` |
| `requiresHospitalConsent` | 固定 `true`：仍需當地醫院同意 |
| `finalConfirmationBy` | `聯合國際救援公司與當地醫院` |

> **目前因為還沒有 JWT，`status` 一律是 `UNDETERMINED`，`reasons = ["IDENTITY_UNAVAILABLE"]`。**

### 3.4 回應範例

一般門診：

```json
{
  "schemaVersion": "2.0",
  "status": "success",
  "source": {"system": "clinic_service", "isMock": false, "retrievedAt": "2026-11-14T05:00:00Z"},
  "data": {
    "queriedLocation": "Tokyo Shinjuku",
    "countryCode": "JP",
    "triage": {"route": "GENERAL_OUTPATIENT", "matchedKeywords": ["發燒", "喉嚨痛"], "specialty": "internal_medicine"},
    "emergencyAssistance": null,
    "advancePayment": null,
    "displayNotice": null,
    "clinics": [
      {
        "clinicId": "jp-tokyo-shinjuku-001",
        "name": "新宿合作診所",
        "address": "東京都新宿區西新宿1-5-1",
        "phone": "+81-3-0000-0000",
        "specialties": ["general"],
        "languages": ["ja", "en"],
        "isPartner": true,
        "openingStatus": "OPEN",
        "distanceKm": 0.4,
        "advancePaymentCoordinable": null
      }
    ]
  },
  "error": null
}
```

重症（目前未接 JWT 的實際結果）：

```json
{
  "schemaVersion": "2.0",
  "status": "success",
  "source": {"system": "clinic_service", "isMock": false, "retrievedAt": "2026-11-14T05:00:00Z"},
  "data": {
    "queriedLocation": "Tokyo Shinjuku",
    "countryCode": "JP",
    "triage": {"route": "HOSPITALIZATION_OR_SEVERE", "matchedKeywords": ["住院", "胸痛"], "specialty": "emergency"},
    "emergencyAssistance": {
      "provider": "國泰產險 24 小時海外急難救助",
      "phone": "+886-2-2755-1258",
      "localEmergencyNumber": "119"
    },
    "advancePayment": {
      "ruleVersion": "2026-11-14",
      "partner": "聯合國際救援公司",
      "requiresHospitalConsent": true,
      "finalConfirmationBy": "聯合國際救援公司與當地醫院",
      "status": "UNDETERMINED",
      "reasons": ["IDENTITY_UNAVAILABLE"]
    },
    "displayNotice": "由急難救助合作夥伴與當地醫院最終確認",
    "clinics": [
      {
        "clinicId": "jp-tokyo-shinjuku-hospital-009",
        "name": "新宿合作綜合醫院",
        "address": "東京都新宿區西新宿6-7-1",
        "phone": "+81-3-0000-0009",
        "specialties": ["emergency", "internal_medicine", "general"],
        "languages": ["ja", "en", "zh"],
        "isPartner": true,
        "openingStatus": "OPEN",
        "distanceKm": 0.9,
        "advancePaymentCoordinable": null
      }
    ]
  },
  "error": null
}
```

### 3.5 錯誤碼

| `error.code` | 情境 | `retryable` | Agent 建議做法 |
|---|---|---|---|
| `INVALID_ARGUMENT` | 欄位缺少、空白、過長，或傳了未定義欄位 | `false` | 修正參數；缺資訊就追問使用者 |
| `NOT_FOUND` | 一般門診：地點無法解析，或附近沒有營業中的合作院所 | `false` | 請使用者提供更明確的地址，或說明附近沒有符合的院所 |
| `SERVICE_UNAVAILABLE` | 一般門診：AWS 下游服務暫時失敗 | `true` | 告知暫時無法查詢，稍後再試 |

重症路線**不會**回錯誤：地點、院所或保單查詢失敗時仍回 `success`，確保急難救助電話一定送得到使用者。

---

## 4. DynamoDB 欄位（`travel-partner-facilities`）

- Partition key：`countryCode`（String）；Sort key：`facilityId`（String）
- 範例資料：[`SOP/sample-facility-item-001.json`](../SOP/sample-facility-item-001.json) ～ `010.json`（001–008 診所，009 東京醫院可代墊，010 首爾醫院不可代墊）
- 欄位註解版：[`SOP/sample-facility-item.annotated.jsonc`](../SOP/sample-facility-item.annotated.jsonc)

| 欄位 | 型別 | 必填 | 用途 | 對外輸出 |
|---|---|---|---|---|
| `countryCode` | S | ✅ | ISO 兩碼國家代碼（PK），例如 `JP` | — |
| `facilityId` | S | ✅ | 院所 ID（SK），格式 `<國家>-<城市>-<地區>-<流水號>` | `clinicId` |
| `name` | S | ✅ | 院所名稱 | ✔ |
| `address` | S | ✅ | 地址 | ✔ |
| `latitude`、`longitude` | N | ✅ | WGS84 座標，用於計算距離 | `distanceKm` |
| `timezone` | S | ✅ | IANA 時區，例如 `Asia/Tokyo`（不可用 `EST` 等縮寫） | — |
| `phone` | S | | 電話，建議國際格式 | ✔ |
| `services` | L\<S\> | ✅ | `general`、`internal_medicine`、`emergency`、`dental`、`pediatric`、`other` | `specialties`（部分） |
| `languages` | L\<S\> | | 看診語言 `zh`／`en`／`ja`／`ko` | ✔ |
| `hours` | M | ✅（診所） | `mon`～`sun`：時段清單，例如 `["09:00-13:00","14:30-19:00"]`；`holidays`：`YYYY-MM-DD` 清單 | `openingStatus` |
| `emergency24h` | BOOL | | `true` = 24 小時急診，一律視為營業中，可進入重症推薦 | — |
| `facilityType` | S | | `hospital` 才會被重症路線推薦 | — |
| `thirdPartyDirectBilling` | BOOL | | 醫院是否接受第三方代墊；須經救援公司確認才可設 `true` | `advancePaymentCoordinable` |
| `active` | BOOL | ✅ | `true` 才會被查到 | — |
| `partnershipStatus` | S | ✅ | 必須是 `ACTIVE` | — |
| `validUntil` | S | | 合作到期日 `YYYY-MM-DD`，過期就排除 | — |
| `priority` | N | | 次要排序權重 | — |
| `insuranceNetworks` | L\<S\> | | 可受理的保險網絡（目前工具未使用） | — |
| `internalContact`、`contractRate` | S | | 內部資料，程式不讀取、不輸出 | — |

營業時間寫法：`00:00-00:00` 代表全天營業；跨午夜寫成 `20:00-02:00`；當天休診填空清單 `[]`。

---

## 5. 下一步：重症代墊資格判斷

代墊判斷的程式已完成（`evaluate_advance_payment`），上線還差三件事。

### 5.1 JWT 身分傳遞（IT 同仁＋Agent 開發同仁）

目標：把**已驗證的使用者 ID** 一路傳到 Tool Lambda，且不能經過 LLM。

```text
前端帶 JWT ─▶ API Gateway JWT Authorizer (Cognito)
             ─▶ Chat Lambda：從 requestContext.authorizer.jwt.claims.sub 取得使用者 ID
             ─▶ AgentCore Runtime（actorId = sub）
             ─▶ AgentCore Gateway（JWT inbound auth／AgentCore Identity 傳遞身分）
             ─▶ Tool Lambda：讀 context.client_context.custom[<key>]
```

| 負責 | 工作 |
|---|---|
| IT（前後端 API） | `POST /v1/chat/messages` 掛 JWT Authorizer；Chat Lambda 從 claims 取 `sub`，**不可**從 request body 或前端參數取 |
| Agent 開發 | Runtime 呼叫 Gateway 時帶上使用者身分；確認身分出現在 Tool Lambda `client_context.custom` 的哪個 key |
| 雙方共同 | 確認 key 後，在 Tool Lambda 設定環境變數 `TRUSTED_IDENTITY_CONTEXT_KEY=<key>`，發布新版本並更新 `live` alias |

驗收：用 Lambda 測試事件傳 `{"location":"Tokyo","symptoms":"胸痛","userId":"someone-else"}`，必須回 `INVALID_ARGUMENT`（工具不接受由輸入帶入的身分）。

### 5.2 保單服務（IT 同仁）

提供一支 Lambda（預設名稱 `travel-policy-service`），輸入 `{"userId": "<sub>"}`，回傳：

```json
{
  "policies": [
    {
      "insurer": "CATHAY_CENTURY",
      "productCode": "OVERSEAS_TRAVEL",
      "status": "ACTIVE",
      "coverageStart": "2026-11-10",
      "coverageEnd": "2026-11-20",
      "currency": "TWD",
      "coverages": {"accidentMedical": 1200000, "overseasSuddenIllnessMedical": 1200000},
      "riders": ["TRAVEL_INCONVENIENCE"]
    }
  ]
}
```

也接受包成 `{"statusCode": 200, "body": "<上面的 JSON 字串>"}` 的格式。設定 Tool Lambda 環境變數 `POLICY_SERVICE_FUNCTION`，並在 Tool Lambda 執行角色加上該函式的 `lambda:InvokeFunction`。

代墊規則（全部符合才算初步符合；多張保單只要任一張符合即可）：

| 條件 | 不符合時的 `reasons` |
|---|---|
| 有效的國泰產險海外旅遊險 | `NO_ACTIVE_CATHAY_TRAVEL_POLICY` |
| 傷害醫療 ≥ 新台幣 120 萬 | `ACCIDENT_MEDICAL_BELOW_THRESHOLD` |
| 海外突發疾病醫療 ≥ 新台幣 120 萬 | `SUDDEN_ILLNESS_MEDICAL_BELOW_THRESHOLD` |
| 幣別為 TWD | `UNSUPPORTED_CURRENCY` |
| 附加旅遊不便險 | `TRAVEL_INCONVENIENCE_RIDER_MISSING` |
| 目前所在地在日本或韓國 | `COUNTRY_NOT_COVERED` |

保單服務暫時失敗時回 `UNDETERMINED`（`POLICY_SERVICE_UNAVAILABLE`），不影響急難救助資訊。

### 5.3 DynamoDB 依真實資料調整

- 以合作院所主檔取代範例資料，並刪除範例項目。`facilityId` 是 Sort Key，用新 ID 寫入不會覆蓋舊資料。
- 醫院要正確填寫 `facilityType=hospital`、`emergency24h`、`services` 含 `emergency`。
- `thirdPartyDirectBilling` 只能依救援公司確認的名單設為 `true`。
- 填入實際營業時間、休診日、看診語言與合作到期日（`validUntil`）。
- 建立定期更新流程：合作終止或到期時，把 `active` 改成 `false` 或更新 `validUntil`。

---

## 6. 參考文件

- Console 建置步驟：[`SOP/海外就醫AgentCore遷移-AWS-Console操作SOP.md`](../SOP/海外就醫AgentCore遷移-AWS-Console操作SOP.md)
- 工具完整規格：[`SOP/海外就醫AgentCore工具-AWS操作SOP.md`](../SOP/海外就醫AgentCore工具-AWS操作SOP.md)
- 前端對話 API：[`api/07-chat.md`](07-chat.md)
