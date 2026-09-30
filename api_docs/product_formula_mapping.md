# Product Formula Mapping Interface

**NETPET → Addverb**

> **Interface:** Product Formula Mapping  
> **Direction:** NETPET → Addverb  
> **Format:** JSON  
> **Character encoding:** UTF-8

---

## 1. Overview

The **Product Formula Mapping Interface** provides Addverb with the relationship between production formulas and the finished products associated with each formula.

A single formula may be associated with multiple finished product SKUs. For this reason, the response is grouped by `FormulaCode`, with all associated finished products returned inside the `Products` array.

This interface provides the link between:

- the **Formulations Interface**, through `FormulaCode`; and
- the finished product/inventory information, through `Sku`.

Conceptually:

```text
Formulation
FormulaCode
    │
    ▼
Product Formula Mapping
FormulaCode → Products[]
                  │
                  ▼
                 Sku
                  │
                  ▼
Inventory / Finished Product
```

---

## 2. Endpoint

### Request

```http
GET /fabrica/addverb/product-formulas/
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
| `data` | Array | List of formulas and their associated finished products. |

### Example

```json
{
  "meta": {
    "status": "Success",
    "requestId": "fad32008-02c5-4834-9c5f-74e809c34302",
    "messageCode": "200",
    "messageDescription": "Success",
    "messageTime": "2026-09-02T11:30:00Z",
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
| `requestId` | String / UUID | No | Unique request identifier used for traceability and troubleshooting. |
| `messageCode` | String | No | Result code associated with the request. |
| `messageDescription` | String | No | Human-readable description of the request result. |
| `messageTime` | DateTime | No | Date and time when the response was generated, in ISO 8601 format. |
| `errorList` | Array | No | List of validation or processing errors. Empty when no errors occurred. |

---

## 5. Formula Mapping Object

Each element of the `data` array represents one formula and the finished products associated with it.

### Fields

| Field | Type | Nullable | Description | Example |
|---|---|:---:|---|---|
| `FormulaCode` | String | No | Formula identifier used to link this mapping with the Formulations Interface. | `"230020-P"` |
| `FormulaName` | String | No | Formula name or description. | `"Pet Zêzere Cão Adulto"` |
| `Products` | Array | No | List of finished products associated with the formula. | `[...]` |

### Example

```json
{
  "FormulaCode": "230020-P",
  "FormulaName": "Pet Zêzere Cão Adulto",
  "Products": [
    {
      "Sku": "1000914",
      "Description": "Pet Zêzere Cão Adulto 4kg"
    },
    {
      "Sku": "1000915",
      "Description": "Pet Zêzere Cão Adulto 10kg"
    }
  ]
}
```

---

## 6. Product Object

Each element of the `Products` array represents one finished product associated with the formula.

### Fields

| Field | Type | Nullable | Description | Example |
|---|---|:---:|---|---|
| `Sku` | String | No | Finished product SKU / commercial product code. | `"1000914"` |
| `Description` | String | No | Finished product description. | `"Pet Zêzere Cão Adulto 4kg"` |

### Example

```json
{
  "Sku": "1000914",
  "Description": "Pet Zêzere Cão Adulto 4kg"
}
```

---

## 7. Business Rules

### 7.1 Formula Grouping

The response is grouped by `FormulaCode`.

If several finished products are associated with the same formula, the formula is returned only once and all finished products are included inside its `Products` array.

For example, source records such as:

```text
230020-P | SKU001 | Finished Product A
230020-P | SKU002 | Finished Product B
230020-P | SKU003 | Finished Product C
```

are returned as:

```json
{
  "FormulaCode": "230020-P",
  "Products": [
    {
      "Sku": "SKU001",
      "Description": "Finished Product A"
    },
    {
      "Sku": "SKU002",
      "Description": "Finished Product B"
    },
    {
      "Sku": "SKU003",
      "Description": "Finished Product C"
    }
  ]
}
```

### 7.2 Formula to Product Relationship

The relationship is:

```text
1 Formula → N Finished Products
```

A formula can therefore be used for multiple finished product SKUs.

The `FormulaCode` identifies the production formula, while `Sku` identifies the corresponding finished product.

### 7.3 Link with the Formulations Interface

`FormulaCode` is the integration key used to associate this interface with the **Formulations Interface**.

For example:

```json
{
  "FormulaCode": "230020-P"
}
```

in this interface corresponds to the formulation exposed with the same normalized `FormulaCode`.

The formulation source may contain a more detailed code, for example:

```text
230020-P.00376.10
```

For integration purposes, the formula code is normalized using the part before the first `.`:

```text
230020-P.00376.10
        ↓
230020-P
```

Therefore:

```text
Formulations.FormulaCode = ProductFormulaMapping.FormulaCode
```

### 7.4 Link with Finished Product / Inventory Data

`Sku` is the finished product identifier.

It can be used to associate the product with other Addverb interfaces that expose the same SKU identifier, including inventory information where applicable.

Conceptually:

```text
FormulaCode
    │
    ├── SKU001 ──→ Inventory
    ├── SKU002 ──→ Inventory
    └── SKU003 ──→ Inventory
```

### 7.5 Valid Mappings

Only records containing both a formula code and a finished product SKU are returned.

Records where either value is missing are excluded from the interface.

### 7.6 Identifier Handling

`FormulaCode` and `Sku` must be handled as strings.

Consumers must not assume that these identifiers are numeric, even when a particular value contains only digits.

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
    │
    └── Products[]
        ├── Sku
        └── Description
```

---

## 9. Relationship Between Interfaces

The Product Formula Mapping Interface acts as the connection between formulations and finished products.

```text
FORMULATIONS INTERFACE

FormulaCode: 230020-P
Ingredients:
    Raw Material A
    Raw Material B
    Raw Material C

            │
            │ FormulaCode
            ▼

PRODUCT FORMULA MAPPING INTERFACE

FormulaCode: 230020-P
Products:
    SKU001
    SKU002
    SKU003

            │
            │ Sku
            ▼

INVENTORY INTERFACE

Sku: SKU001
Batch: ...
StockQuantity: ...
WeightTON: ...
```

This allows Addverb to determine:

```text
Finished Product SKU
        ↓
Production Formula
        ↓
Required Raw Materials
```

---

## 10. Complete Response Example

```json
{
  "meta": {
    "status": "Success",
    "requestId": "fad32008-02c5-4834-9c5f-74e809c34302",
    "messageCode": "200",
    "messageDescription": "Success",
    "messageTime": "2026-09-02T11:30:00Z",
    "errorList": []
  },
  "data": [
    {
      "FormulaCode": "230020-P",
      "FormulaName": "Pet Zêzere Cão Adulto",
      "Products": [
        {
          "Sku": "SKU001",
          "Description": "Pet Zêzere Cão Adulto 4kg"
        },
        {
          "Sku": "SKU002",
          "Description": "Pet Zêzere Cão Adulto 10kg"
        },
        {
          "Sku": "SKU003",
          "Description": "Pet Zêzere Cão Adulto 20kg"
        }
      ]
    },
    {
      "FormulaCode": "230021-P",
      "FormulaName": "Pet Zêzere Cão Junior",
      "Products": [
        {
          "Sku": "SKU010",
          "Description": "Pet Zêzere Cão Junior 10kg"
        }
      ]
    }
  ]
}
```

---

## 11. Error Handling

When product/formula mappings cannot be retrieved, the interface returns an error response.

The `requestId` should be retained when reporting integration issues because it allows the request to be traced in NETPET logs.

### Example

```json
{
  "meta": {
    "status": "Error",
    "requestId": "fad32008-02c5-4834-9c5f-74e809c34302",
    "messageCode": "500",
    "messageDescription": "Internal server error",
    "messageTime": "2026-09-02T11:30:00Z",
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

## 12. Source Data Mapping

| API Field | Source Field | Description |
|---|---|---|
| `FormulaCode` | `Produtos.PRD_Codigo` | Formula identifier. |
| `FormulaName` | `Produtos.PRD_Nome` | Formula name/description. |
| `Sku` | `CodigosComerciais.CDC_CodigoComercial` | Finished product SKU/commercial code. |
| `Description` | `CodigosComerciais.CDC_Descricao` | Finished product description. |

The source relationship is obtained through `CCPorProduto`.

---

## 13. Integration Notes

1. `FormulaCode` is the key connecting this interface with the Formulations Interface.
2. `Sku` is the key connecting the mapping with the corresponding finished product.
3. One formula may be associated with multiple finished products.
4. The API groups all finished products belonging to the same formula inside `Products`.
5. Formula codes in the Formulations Interface are normalized using the portion before the first `.` when the source formulation code contains additional suffixes.
6. Formula and SKU identifiers must be handled as strings.
7. Records without a valid formula code or finished product SKU are not exposed.
8. Addverb should use identifiers rather than descriptions when establishing relationships.
9. Descriptions are informational and should not be used as integration keys.
10. `requestId` should be retained for troubleshooting and support purposes.

---

**Interface:** Product Formula Mapping Interface  
**Source System:** NETPET / Multidos  
**Target System:** Addverb
