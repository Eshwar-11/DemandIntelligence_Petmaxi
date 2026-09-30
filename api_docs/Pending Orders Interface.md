# Pending Orders Interface

**NETPET → Addverb**

> **Interface:** Pending customer orders  
> **Direction:** NETPET → Addverb  
> **Format:** JSON  
> **Character encoding:** UTF-8  
> **Date format:** ISO 8601 (`YYYY-MM-DD`)

---

## 1. Overview

The **Pending Orders Interface** provides Addverb with the current list of customer orders pending processing in NETPET/Primavera.

Each order contains general order information and an `Items` array containing the SKUs associated with the order, including ordered, reserved, pending and transformed quantities, as well as weight and pallet information.

---

## 2. Endpoint

### Request

```http
GET /fabrica/addverb/list-orders/
```

### Response Content Type

```http
Content-Type: application/json
```

A successful request returns:

```http
200 OK
```

---

## 3. Response Structure

The response contains two main properties:

| Field | Type | Description |
|---|---|---|
| `meta` | Object | Information about the request and its result. |
| `data` | Array | List of pending customer orders. |

### Example

```json
{
  "meta": {
    "status": "Success",
    "requestId": "fad32008-02c5-4834-9c5f-74e809c34302",
    "messageCode": "200",
    "messageDescription": "Success",
    "messageTime": "2026-09-02T08:48:05.244425Z",
    "errorList": []
  },
  "data": []
}
```

---

## 4. Meta Object

The `meta` object contains information about the processing of the request.

| Field | Type | Nullable | Description |
|---|---|:---:|---|
| `status` | String | No | Overall status of the request. |
| `requestId` | String / UUID | No | Unique identifier assigned to the request for traceability and troubleshooting. |
| `messageCode` | String | No | Result code associated with the request. |
| `messageDescription` | String | No | Human-readable description of the request result. |
| `messageTime` | DateTime | No | Date and time when the response was generated, in ISO 8601 format. |
| `errorList` | Array | No | List containing validation or processing errors. Empty when no errors occurred. |

### Successful Response Example

```json
{
  "status": "Success",
  "requestId": "fad32008-02c5-4834-9c5f-74e809c34302",
  "messageCode": "200",
  "messageDescription": "Success",
  "messageTime": "2026-09-02T08:48:05.244425Z",
  "errorList": []
}
```

---

## 5. Order Object

Each element of the `data` array represents one customer order. An order can contain one or multiple SKUs, represented by the `Items` array.

### Order Fields

| Field | Type | Nullable | Description | Example |
|---|---|:---:|---|---|
| `OrderId` | String | No | Unique order identifier generated as `Series-DocumentNumber`. | `"2026-4041"` |
| `Series` | String | No | ERP document series. | `"2026"` |
| `DocumentNumber` | String | No | ERP document/order number. | `"4041"` |
| `CustomerName` | String | No | Customer name. | `"PINGO DOCE - DIST. ALIMENTAR, SA"` |
| `Country` | String | Yes | Customer country or country code. | `"PT"` |
| `Reference` | String | Yes | Customer purchase order or external order reference. | `"8094897754"` |
| `DeliveryDate` | String (Date) | Yes | Planned delivery date in `YYYY-MM-DD` format. | `"2026-09-09"` |
| `Status` | String | Yes | Current status of the order in NETPET. | `"Planned"` |
| `Notes` | String | Yes | Additional information or remarks associated with the order. | `"Valongo"` |
| `IsExport` | Boolean | Yes | Indicates whether the order is classified as an export order. | `false` |
| `Items` | Array | No | List of SKUs/order lines associated with the order. | `[...]` |

### Example

```json
{
  "OrderId": "2026-4041",
  "Series": "2026",
  "DocumentNumber": "4041",
  "CustomerName": "PINGO DOCE - DIST. ALIMENTAR, SA",
  "Country": null,
  "Reference": "8094897754",
  "DeliveryDate": "2026-09-09",
  "Status": "",
  "Notes": "Valongo",
  "IsExport": false,
  "Items": []
}
```

---

## 6. Item Object

Each element of the `Items` array represents one SKU/order line belonging to the customer order.

### Item Fields

| Field | Type | Nullable | Description | Example |
|---|---|:---:|---|---|
| `Sku` | String | No | Unique SKU/article identifier used by NETPET/Primavera. | `"1000914"` |
| `Description` | String | No | Product description. | `"Kitty Gato Carne 4kg"` |
| `Unity` | String | Yes | Product  Unity. | `"SAC4"` |
| `Family` | String | Yes | Product family. | `"06MARCAPRO"` |
| `SubFamily` | String | Yes | Product subfamily. | `"KITTY"` |
| `OrderedQuantity` | Number | No | Total quantity ordered for the SKU. | `1600` |
| `ReservedQuantity` | Number | No | Quantity currently reserved for the order. | `0` |
| `PendingQuantity` | Number | No | Quantity that is still pending. | `1600` |
| `TransformedQuantity` | Number | No | Quantity already transformed/processed. | `0` |
| `WeightTON` | Number | No | Total weight associated with the order line, expressed in metric tonnes. | `6.4` |
| `Pallets` | Number | No | Number of pallets associated with the order line. | `10` |

### Example

```json
{
  "Sku": "1000914",
  "Description": "Kitty Gato Carne 4kg",
  "Unity": "SAC4",
  "Family": "06MARCAPRO",
  "SubFamily": "KITTY",
  "OrderedQuantity": 1600,
  "ReservedQuantity": 0,
  "PendingQuantity": 1600,
  "TransformedQuantity": 0,
  "WeightTON": 6.4,
  "Pallets": 10
}
```

---

## 7. Business Rules

### 7.1 Order Identification

`OrderId` is the unique order identifier used across the integration.

It is generated by NETPET using the following format:

```text
Series-DocumentNumber
```

Example:

```text
Series:         2026
DocumentNumber: 4041
OrderId:        2026-4041
```

Addverb should retain and use the `OrderId` supplied by NETPET when referencing the order in subsequent integration messages.

### 7.2 Multiple Items per Order

A customer order can contain one or multiple SKUs.

All SKUs belonging to the same order are grouped inside the `Items` array.

```json
{
  "OrderId": "2026-4041",
  "Series": "2026",
  "DocumentNumber": "4041",
  "Items": [
    {
      "Sku": "1000914",
      "OrderedQuantity": 1600
    },
    {
      "Sku": "1000920",
      "OrderedQuantity": 800
    }
  ]
}
```

### 7.3 Quantities

The interface provides four quantity values for each SKU:

- **`OrderedQuantity`** — Total quantity ordered by the customer.
- **`ReservedQuantity`** — Quantity currently reserved against the order.
- **`PendingQuantity`** — Quantity that remains pending.
- **`TransformedQuantity`** — Quantity already transformed/processed.

A numeric value of `0` is valid and must **not** be interpreted as missing or unavailable data.

### 7.4 Weight

`WeightTON` represents the total weight associated with the order line and is expressed in **metric tonnes**.

The value is returned with a maximum precision of three decimal places.

Example:

```json
"WeightTON": 6.4
```

This represents:

```text
6.4 metric tonnes = 6,400 kg
```

### 7.5 Pallets

`Pallets` represents the number of pallets associated with the order.

Example:

```json
"Pallets": 10
```

### 7.6 Dates

`DeliveryDate` uses the following format:

```text
YYYY-MM-DD
```

Example:

```json
"DeliveryDate": "2026-09-09"
```

If no delivery date is available:

```json
"DeliveryDate": null
```

### 7.7 Null and Empty Values

Fields for which information is unavailable may contain the JSON value `null`.

Example:

```json
{
  "Country": null,
  "DeliveryDate": null
}
```

A `null` value must be distinguished from:

- numeric `0`;
- boolean `false`;
- an empty string `""`.

These values may have different business meanings and must not be treated as equivalent.

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
    ├── OrderId
    ├── Series
    ├── DocumentNumber
    ├── CustomerName
    ├── Country
    ├── Reference
    ├── DeliveryDate
    ├── Status
    ├── Notes
    ├── IsExport
    │
    └── Items[]
        ├── Sku
        ├── Description
        ├── Unity
        ├── Family
        ├── SubFamily
        ├── OrderedQuantity
        ├── ReservedQuantity
        ├── PendingQuantity
        ├── TransformedQuantity
        ├── WeightTON
        └── Pallets
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
    "messageTime": "2026-09-02T08:48:05.244425Z",
    "errorList": []
  },
  "data": [
    {
      "OrderId": "2026-4041",
      "Series": "2026",
      "DocumentNumber": "4041",
      "CustomerName": "PINGO DOCE - DIST. ALIMENTAR, SA",
      "Country": null,
      "Reference": "8094897754",
      "DeliveryDate": "2026-09-09",
      "Status": "",
      "Notes": "Valongo",
      "IsExport": false,
      "Items": [
        {
          "Sku": "1000914",
          "Description": "Kitty Gato Carne 4kg",
          "Unity": "Sac4",
          "Family": "06MARCAPRO",
          "SubFamily": "KITTY",
          "OrderedQuantity": 1600,
          "ReservedQuantity": 0,
          "PendingQuantity": 1600,
          "TransformedQuantity": 0,
          "WeightTON": 6.4,
          "Pallets": 10
        }
      ]
    }
  ]
}
```

---

## 10. Error Handling

When the request cannot be processed, the interface returns an error response containing the same `meta` structure and an `errorList` describing the error.

The `requestId` should be retained when reporting integration issues, as it allows the request to be traced in NETPET logs.

### Example Error Structure

```json
{
  "meta": {
    "status": "Error",
    "requestId": "fad32008-02c5-4834-9c5f-74e809c34302",
    "messageCode": "500",
    "messageDescription": "Internal server error",
    "messageTime": "2026-09-02T08:48:05.244425Z",
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

1. `OrderId` should be considered the primary order identifier for integration purposes.
2. `OrderId` must be returned unchanged by Addverb whenever an order is referenced in subsequent messages.
3. One order may contain multiple entries in `Items`.
4. SKU identifiers should be handled as strings and must not be converted to numeric identifiers.
5. `0`, `false`, `""` and `null` are distinct values and must not be treated as equivalent.
6. `DeliveryDate` is provided in `YYYY-MM-DD` format.
7. `WeightTON` is expressed in metric tonnes.
8. Quantities are provided as numeric values.
9. Consumers should not infer business state solely from the presence or absence of optional fields.

---

**Interface:** Pending Orders Interface  
**Source System:** NETPET / Primavera  
**Target System:** Addverb