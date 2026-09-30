# Inventory Interface

**NETPET → Addverb**

> **Interface:** Inventory / Stock Availability  
> **Direction:** NETPET → Addverb  
> **Format:** JSON  
> **Character encoding:** UTF-8  
> **Date format:** ISO 8601 (`YYYY-MM-DD`)

---

## 1. Overview

The **Inventory Interface** provides Addverb with the current inventory available in NETPET/Primavera.

Inventory is returned at **SKU and batch level**, allowing Addverb to identify the available quantity for each product and production batch.

Only inventory records with the following article types are relevant to this integration:

| ArticleTypeId | ArticleType | Description |
|---:|---|---|
| `3` | `Mercadoria` | Goods / merchandise |
| `4` | `Produto Acabado` | Finished product |
| `6` | `Matéria Prima` | Raw material |
| `9` | `Embal. de Consumo` | Packaging material |

The `ArticleTypeId` field identifies the **business classification of the SKU in Primavera**. It is not a stock status, warehouse location or product family. Addverb should use this field to distinguish the type of material represented by each inventory record.

---

## 2. Endpoint

### Request

```http
GET /fabrica/addverb/inventory/
```

### Response

```http
Content-Type: application/json
```

A successful request returns `200 OK`.

---

## 3. Response Structure

| Field | Type | Description |
|---|---|---|
| `meta` | Object | Information about the request and its result. |
| `data` | Array | List of inventory records. |

```json
{
  "meta": {
    "status": "Success",
    "requestId": "fad32008-02c5-4834-9c5f-74e809c34302",
    "messageCode": "200",
    "messageDescription": "Success",
    "messageTime": "2026-09-02T09:48:05.244425Z",
    "errorList": []
  },
  "data": []
}
```

---

## 4. Meta Object

| Field | Type | Nullable | Description |
|---|---|:---:|---|
| `status` | String | No | Overall status of the request. |
| `requestId` | String / UUID | No | Unique request identifier used for traceability and troubleshooting. |
| `messageCode` | String | No | Result code associated with the request. |
| `messageDescription` | String | No | Human-readable description of the result. |
| `messageTime` | DateTime | No | Response generation time in ISO 8601 format. |
| `errorList` | Array | No | Validation or processing errors. Empty when no errors occurred. |

---

## 5. Inventory Object

Each element of `data` represents the inventory of one **SKU + Batch** combination.

| Field | Type | Nullable | Description | Example |
|---|---|:---:|---|---|
| `Sku` | String | No | Unique SKU/article identifier used by NETPET/Primavera. | `"1000914"` |
| `Description` | String | No | Product description. | `"Kitty Gato Carne 4kg"` |
| `Family` | String | Yes | Product family. | `"06MARCAPRO"` |
| `SubFamily` | String | Yes | Product subfamily. | `"KITTY"` |
| `Batch` | String | No | Production/inventory batch identifier. | `"260825001"` |
| `StockQuantity` | Number | No | Available stock expressed in the SKU sales unit. | `1600` |
| `WeightTON` | Number | No | Available stock weight expressed in metric tonnes. | `6.4` |
| `ProductionDate` | String (Date) | Yes | Batch production date (`YYYY-MM-DD`). | `"2026-08-25"` |
| `ExpirationDate` | String (Date) | Yes | Batch expiration date (`YYYY-MM-DD`). | `"2027-08-25"` |
| `ShelfLifeDays` | Integer | Yes | Total shelf life between production and expiration dates, in days. | `365` |
| `RemainingShelfLifeDays` | Integer | Yes | Number of days remaining until expiration. | `357` |
| `ArticleTypeId` | String / Integer | No | Primavera article type identifier. Only `3`, `4`, `6` and `9` are relevant. | `4` |
| `ArticleType` | String | No | Human-readable Primavera article type. | `"Produto Acabado"` |
| `Model` | String | Yes | Product model/classification defined in Primavera. | `null` |

### Example

```json
{
  "Sku": "1000914",
  "Description": "Kitty Gato Carne 4kg",
  "Family": "06MARCAPRO",
  "SubFamily": "KITTY",
  "Batch": "260825001",
  "StockQuantity": 1600,
  "WeightTON": 6.4,
  "ProductionDate": "2026-08-25",
  "ExpirationDate": "2027-08-25",
  "ShelfLifeDays": 365,
  "RemainingShelfLifeDays": 357,
  "ArticleTypeId": 4,
  "ArticleType": "Produto Acabado",
  "Model": null
}
```

---

## 6. Article Types

`ArticleTypeId` represents the **article classification defined in Primavera ERP**. For this integration, only four article types are within scope.

### `3` — Mercadoria

Represents **goods / merchandise / Snacks / Silica** classified in Primavera as `Mercadoria`.

```json
{ "ArticleTypeId": 3, "ArticleType": "Mercadoria" }
```

### `4` — Produto Acabado

Represents a **finished product**, available for storage, order fulfilment and shipment.

```json
{ "ArticleTypeId": 4, "ArticleType": "Produto Acabado" }
```

### `6` — Matéria Prima

Represents a **raw material** used as an input in the production process.

```json
{ "ArticleTypeId": 6, "ArticleType": "Matéria Prima" }
```

### `9` — Embal. de Consumo

Represents **packaging material** used during the production and packing process.

```json
{ "ArticleTypeId": 9, "ArticleType": "Embal. de Consumo" }
```

### Summary

| ArticleTypeId | Primavera Description | Integration Meaning |
|---:|---|---|
| `3` | `Mercadoria` | Goods / merchandise |
| `4` | `Produto Acabado` | Finished product |
| `6` | `Matéria Prima` | Raw material |
| `9` | `Embal. de Consumo` | Packaging material |

Other Primavera article types are outside the scope of this interface and should not be included in the inventory response.

---

## 7. Business Rules

### 7.1 Inventory Identification

Inventory is provided at **SKU + Batch** level. The same SKU may appear multiple times when stock exists in different batches.

```json
[
  { "Sku": "1000914", "Batch": "260825001", "StockQuantity": 1600 },
  { "Sku": "1000914", "Batch": "260826001", "StockQuantity": 800 }
]
```

### 7.2 Stock Quantity

`StockQuantity` represents available stock converted to the SKU sales unit according to the unit conversion configured in Primavera. If no applicable conversion exists, the original stock quantity is used.

### 7.3 Weight

`WeightTON` represents available stock expressed in **metric tonnes**.

```text
6.4 metric tonnes = 6,400 kg
```

### 7.4 Positive Stock Only

Only inventory records with stock greater than zero are included. Zero-stock and negative-stock records are outside the scope of the response.

### 7.5 Production and Expiration Dates

`ProductionDate` identifies the production date associated with the batch. `ExpirationDate` identifies the expiration/best-before date associated with the batch. Both use `YYYY-MM-DD`.

### 7.6 Shelf Life

`ShelfLifeDays` is the total number of days between production and expiration:

```text
ShelfLifeDays = ExpirationDate - ProductionDate
```

`RemainingShelfLifeDays` is the number of days between the current date and expiration:

```text
RemainingShelfLifeDays = ExpirationDate - CurrentDate
```

A negative `RemainingShelfLifeDays` indicates that the expiration date has already passed.

### 7.7 Null and Zero Values

`null`, numeric `0`, boolean `false` and an empty string `""` are distinct values and must not be treated as equivalent.

---

## 8. Data Hierarchy

```text
Response
│
├── meta
│   ├── status
│   ├── requestId
│   ├── messageCode
│   ├── messageDescription
│   ├── messageTime
│   └── errorList
│
└── data[]
    ├── Sku
    ├── Description
    ├── Family
    ├── SubFamily
    ├── Batch
    ├── StockQuantity
    ├── WeightTON
    ├── ProductionDate
    ├── ExpirationDate
    ├── ShelfLifeDays
    ├── RemainingShelfLifeDays
    ├── ArticleTypeId
    ├── ArticleType
    └── Model
```

---

## 9. Complete Response Example

```json
{
  "meta": {
    "status": "Success",
    "requestId": "fad32008-02c5-4834-9c5f-74e809c34302",
    "messageCode": "200",
    "messageDescription": "Success",
    "messageTime": "2026-09-02T09:48:05.244425Z",
    "errorList": []
  },
  "data": [
    {
      "Sku": "1000914",
      "Description": "Kitty Gato Carne 4kg",
      "Family": "06MARCAPRO",
      "SubFamily": "KITTY",
      "Batch": "260825001",
      "StockQuantity": 1600,
      "WeightTON": 6.4,
      "ProductionDate": "2026-08-25",
      "ExpirationDate": "2027-08-25",
      "ShelfLifeDays": 365,
      "RemainingShelfLifeDays": 357,
      "ArticleTypeId": 4,
      "ArticleType": "Produto Acabado",
      "Model": null
    }
  ]
}
```

---

## 10. Error Handling

When inventory cannot be retrieved, the interface returns an error response. The `requestId` should be retained when reporting integration issues because it allows the request to be traced in NETPET logs.

```json
{
  "meta": {
    "status": "Error",
    "requestId": "fad32008-02c5-4834-9c5f-74e809c34302",
    "messageCode": "500",
    "messageDescription": "Internal server error",
    "messageTime": "2026-09-02T09:48:05.244425Z",
    "errorList": [
      {
        "fieldName": "server",
        "messageDescription": "Error description"
      }
    ]
  },
  "data": null
}
```

---

## 11. Integration Notes

1. Inventory is provided at **SKU + Batch** level.
2. The same `Sku` may appear multiple times when inventory exists in different batches.
3. `ArticleTypeId` identifies the Primavera business classification of the article.
4. Only `ArticleTypeId` values `3`, `4`, `6` and `9` are within the scope of this integration.
5. `StockQuantity` represents stock in the SKU sales unit.
6. `WeightTON` represents stock in metric tonnes.
7. Only positive stock quantities are included.
8. SKU and batch identifiers should be handled as strings.
9. Dates use the `YYYY-MM-DD` format.
10. `0`, `false`, `""` and `null` are distinct values and must not be treated as equivalent.

---

**Interface:** Inventory Interface  
**Source System:** NETPET / Primavera  
**Target System:** Addverb
