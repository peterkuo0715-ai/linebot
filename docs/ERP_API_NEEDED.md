# LINE Bot 所需的 ERP API 新增需求

## 背景
LINE Bot 已串接現有的 customer、products、quote、create_quote、verify_staff、pdf API。
為了讓客戶能自助查詢訂單、報價、物流，需要新增以下 endpoint。

## 現有認證方式（維持不變）
```
Base URL: https://unifi-erp.vercel.app/api/external/linebot
Header: Authorization: Bearer {token}
```

---

## 需要新增的 API

### 1. 查詢客戶訂單清單
```
GET ?action=orders&code=K01&status=all&limit=10
```

**參數：**
- `code` — 客戶代號
- `status` — 可選：`all`（全部）/ `pending`（處理中）/ `confirmed`（已確認）/ `shipped`（已出貨）/ `completed`（已完成）
- `limit` — 最多幾筆（預設 10）

**回傳：**
```json
[
  {
    "orderNumber": "PO-2026-0042",
    "createdAt": "2026-04-15",
    "status": "shipped",
    "statusText": "已出貨",
    "totalAmount": 277360,
    "itemCount": 1,
    "items": [
      {"sku": "U7-Pro", "name": "U7-Pro", "quantity": 40, "unitPrice": 6934}
    ],
    "expectedDelivery": "2026-04-18"
  }
]
```

---

### 2. 查詢客戶報價單清單
```
GET ?action=quotes_list&code=K01&status=pending&limit=10
```

**參數：**
- `code` — 客戶代號
- `status` — `all` / `pending`（待成交）/ `approved`（已核准）/ `expired`（過期）
- `limit` — 最多幾筆

**回傳：**
```json
[
  {
    "quoteNumber": "QT-2026-0045",
    "createdAt": "2026-04-15",
    "expiryDate": "2026-05-15",
    "status": "pending",
    "statusText": "待成交",
    "totalAmount": 277360,
    "itemCount": 1,
    "pdfDownload": "https://.../pdf?quoteId=xxx"
  }
]
```

---

### 3. 查詢物流/配送狀態
```
GET ?action=shipping&code=K01
```

**參數：**
- `code` — 客戶代號
- 回傳該客戶所有**進行中**的出貨（status != delivered）

**回傳：**
```json
[
  {
    "orderNumber": "PO-2026-0042",
    "shippingMethod": "黑貓宅急便",
    "trackingNumber": "820123456789",
    "trackingUrl": "https://www.t-cat.com.tw/Inquire/Trace.aspx?no=820123456789",
    "status": "in_transit",
    "statusText": "配送中",
    "shippedAt": "2026-04-16",
    "expectedDelivery": "2026-04-18",
    "items": [
      {"sku": "U7-Pro", "quantity": 40}
    ]
  }
]
```

**status 可能值：**
- `preparing` — 備貨中
- `shipped` — 已出貨
- `in_transit` — 配送中
- `delivered` — 已送達
- `returned` — 退回

---

### 4. 查詢客戶帳款
```
GET ?action=accounts&code=K01
```

**回傳：**
```json
{
  "customer": {
    "code": "K01",
    "name": "Kenny公司"
  },
  "creditLimit": 500000,
  "usedCredit": 277360,
  "availableCredit": 222640,
  "unpaidAmount": 277360,
  "overdueAmount": 0,
  "invoices": [
    {
      "invoiceNumber": "INV-2026-0042",
      "orderNumber": "PO-2026-0042",
      "amount": 277360,
      "dueDate": "2026-05-15",
      "status": "unpaid",
      "daysOverdue": 0
    }
  ]
}
```

---

## 使用情境

### 情境 1：客戶查訂單
```
客戶：訂單
Bot → API ?action=orders&code=K01
Bot：K01 最近訂單：
  • PO-2026-0042  U7-Pro x40  $277,360  已出貨（預計 4/18）
  • PO-2026-0041  U6+ x20     $91,980   處理中
```

### 情境 2：客戶查物流
```
客戶：物流
Bot → API ?action=shipping&code=K01
Bot：K01 進行中的配送：
  • PO-2026-0042 黑貓 820123456789 配送中 → 預計 4/18
```

### 情境 3：員工查客戶帳款（管理群組）
```
員工：$K01 帳款
Bot → API ?action=accounts&code=K01
Bot：K01 帳款狀況：
  • 信用額度：$500,000
  • 已用額度：$277,360
  • 未付款：$277,360
  • 逾期：$0
```

---

## 權限設計（Bot 端會處理）

| API | 客戶可看 | 員工可看 |
|---|---|---|
| orders | ✓（自己的） | ✓ |
| quotes_list | ✓（自己的） | ✓ |
| shipping | ✓（自己的） | ✓ |
| accounts | ✗ | ✓ |

客戶只能看自己群組綁定的客戶代號的資料，員工在管理群組可以查任何客戶。

---

## 錯誤處理

| HTTP Status | 意義 |
|---|---|
| 200 | 成功 |
| 400 | 缺少參數 |
| 401 | Token 錯誤 |
| 404 | 客戶代號不存在 |

**找不到資料時回傳空陣列 `[]`，不是 404**

---

## 優先順序建議

1. **高**：orders（客戶最常問）
2. **高**：shipping（減少「我的貨呢」客服電話）
3. **中**：quotes_list（方便客戶追蹤未成交報價）
4. **低**：accounts（內部使用為主）

可以分批開放，先做 1 和 2 就能大幅減少客服負擔。
