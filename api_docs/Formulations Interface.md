# Formulations Interface

**NETPET → Addverb**

> **Interface:** Product Formulations / Recipes  
> **Direction:** NETPET → Addverb  
> **Format:** JSON  
> **Character encoding:** UTF-8  
> **Date/time format:** ISO 8601

---

## 1. Overview

The **Formulations Interface** provides Addverb with the formulations available in the formulation system used by NETPET.

Each formulation is identified by a unique formula code and contains general formula information together with an `Ingredients` array describing the raw materials and their required quantities.

The response is grouped by `FormulaCode`, meaning that each formula is returned once and all raw materials belonging to that formula are included inside its `Ingredients` array.

---

## 2. Endpoint

### Request

```http
GET /fabrica/addverb/formulations/
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
| `data` | Array | List of available formulations. |

### Example

```json
{
  "meta": {
    "status": "Success",
    "requestId": "fad32008-02c5-4834-9c5f-74e809c34302",
    "messageCode": "200",
    "messageDescription": "Success",
    "messageTime": "2026-09-02T10:00:00Z",
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

---

## 5. Formula Object

Each element of the `data` array represents one formulation.

A formula may contain one or multiple raw materials, represented by the `Ingredients` array.

### Formula Fields

| Field | Type | Nullable | Description | Example |
|---|---|:---:|---|---|
| `FormulaCode` | String | No | Unique formula identifier. | `"F001"` |
| `FormulaName` | String | No | Formula name or description. | `"Adult Chicken Formula"` |
| `CreatedAt` | DateTime | Yes | Date and time when the formula was created. | `"2025-01-15T10:20:00"` |
| `UpdatedAt` | DateTime | Yes | Date and time when the formula was last modified. | `"2026-08-20T15:35:00"` |
| `IsDeleted` | Boolean | No | Indicates whether the formula is marked as deleted in the source system. | `false` |
| `Ingredients` | Array | No | List of raw materials and quantities belonging to the formula. | `[...]` |

### Example

```json
{
  "FormulaCode": "F001",
  "FormulaName": "Adult Chicken Formula",
  "CreatedAt": "2025-01-15T10:20:00",
  "UpdatedAt": "2026-08-20T15:35:00",
  "IsDeleted": false,
  "Ingredients": []
}
```

---

## 6. Ingredient Object

Each element of the `Ingredients` array represents one raw material used in the formulation.

### Ingredient Fields

| Field | Type | Nullable | Description | Example |
|---|---|:---:|---|---|
| `RawMaterial` | String | No | Unique raw material/product identifier. | `"MP001"` |
| `RawMaterialName` | String | No | Raw material/product description. | `"Maize"` |
| `QtyKg` | Number | No | Quantity of the raw material required by the formulation, expressed in kilograms. | `350.0` |

### Example

```json
{
  "RawMaterial": "MP001",
  "RawMaterialName": "Maize",
  "QtyKg": 350.0
}
```

---

## 7. Business Rules

### 7.1 Formula Identification

`FormulaCode` is the unique identifier of the formulation.

All rows belonging to the same `FormulaCode` are grouped into a single formula object.

Example:

```text
FormulaCode: F001
```

The same `FormulaCode` must not be returned as multiple formula objects. Its raw materials must instead be grouped inside the corresponding `Ingredients` array.

### 7.2 Multiple Ingredients per Formula

A formula can contain one or multiple raw materials.

For example, source records such as:

```text
F001 | MP001 | 350 kg
F001 | MP002 | 200 kg
F001 | MP003 | 150 kg
```

are returned as:

```json
{
  "FormulaCode": "F001",
  "Ingredients": [
    {
      "RawMaterial": "MP001",
      "QtyKg": 350.0
    },
    {
      "RawMaterial": "MP002",
      "QtyKg": 200.0
    },
    {
      "RawMaterial": "MP003",
      "QtyKg": 150.0
    }
  ]
}
```

### 7.3 Ingredient Quantity

`QtyKg` represents the quantity of the corresponding raw material required by the formula and is expressed in **kilograms**.

Example:

```json
"QtyKg": 350.0
```

represents:

```text
350 kg of the specified raw material
```

### 7.4 Formula Availability

Only formulas configured as available in at least one formulation location are included in the interface.

Formula availability is determined by the source system configuration (`FPL_Disponivel = 1`).

### 7.5 Deleted Formulas

`IsDeleted` indicates whether the formula is marked as deleted in the source system.

```json
"IsDeleted": false
```

means that the formula is not marked as deleted.

```json
"IsDeleted": true
```

means that the formula is marked as deleted.

This field should be retained by Addverb so that formula lifecycle changes can be correctly identified during synchronization.

### 7.6 Creation and Modification Dates

`CreatedAt` represents the date and time when the formula was created.

`UpdatedAt` represents the date and time of the most recent formula modification.

These values are returned in ISO 8601 format when available.

Example:

```json
{
  "CreatedAt": "2025-01-15T10:20:00",
  "UpdatedAt": "2026-08-20T15:35:00"
}
```

### 7.7 Null and Zero Values

Fields for which information is unavailable may contain the JSON value `null`.

A numeric value of `0` is valid and must not automatically be interpreted as missing data.

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
    ├── FormulaCode
    ├── FormulaName
    ├── CreatedAt
    ├── UpdatedAt
    ├── IsDeleted
    │
    └── Ingredients[]
        ├── RawMaterial
        ├── RawMaterialName
        └── QtyKg
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
    "messageTime": "2026-09-02T10:00:00Z",
    "errorList": []
  },
  "data": [
    {
      "FormulaCode": "F001",
      "FormulaName": "Adult Chicken Formula",
      "CreatedAt": "2025-01-15T10:20:00",
      "UpdatedAt": "2026-08-20T15:35:00",
      "IsDeleted": false,
      "Ingredients": [
        {
          "RawMaterial": "MP001",
          "RawMaterialName": "Maize",
          "QtyKg": 350.0
        },
        {
          "RawMaterial": "MP002",
          "RawMaterialName": "Wheat",
          "QtyKg": 200.0
        },
        {
          "RawMaterial": "MP003",
          "RawMaterialName": "Meat Meal",
          "QtyKg": 150.0
        }
      ]
    }
  ]
}
```

---

## 10. Error Handling

When formulations cannot be retrieved, the interface returns an error response.

The `requestId` should be retained when reporting integration issues because it allows the request to be traced in NETPET logs.

### Example

```json
{
  "meta": {
    "status": "Error",
    "requestId": "fad32008-02c5-4834-9c5f-74e809c34302",
    "messageCode": "500",
    "messageDescription": "Internal server error",
    "messageTime": "2026-09-02T10:00:00Z",
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

1. `FormulaCode` is the unique formulation identifier.
2. The response is grouped by `FormulaCode`.
3. Each formula is returned once, with all associated raw materials grouped inside `Ingredients`.
4. `RawMaterial` identifies the raw material/product used by the formula.
5. `QtyKg` is always expressed in kilograms.
6. Only formulas configured as available in the source system are included.
7. `IsDeleted` indicates the deletion state of the formula in the source system.
8. Formula and raw material identifiers should be handled as strings.
9. Date/time fields use ISO 8601 format.
10. `0`, `false`, `""` and `null` are distinct values and must not be treated as equivalent.

---

## 12. Source Data Mapping

| API Field | Source Field |
|---|---|
| `FormulaCode` | `Formulas.FML_Codigo` |
| `FormulaName` | `Formulas.FML_Nome` |
| `CreatedAt` | `Formulas.FML_DataCriacao` |
| `UpdatedAt` | `Formulas.FML_DataAlteracao` |
| `IsDeleted` | `Formulas.FML_Eliminada` |
| `RawMaterial` | `Produtos.PRD_Codigo` |
| `RawMaterialName` | `Produtos.PRD_Nome` |
| `QtyKg` | `Formulacoes.FMC_Qtde` |

---

**Interface:** Formulations Interface  
**Source System:** NETPET / Formulation System  
**Target System:** Addverb
