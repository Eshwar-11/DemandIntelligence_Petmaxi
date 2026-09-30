# Unit Conversion Interface

**NETPET → Addverb**

## Overview

This endpoint provides the unit conversion factors required to convert
product quantities between **units, kilograms and pallets**.

The `Unit` value corresponds to the sales unit provided in the Pending
Orders interface.

## Endpoint

``` http
GET /fabrica/addverb/unit-conversions/
```

## Response Fields

  -----------------------------------------------------------------------
  Field                   Type                    Description
  ----------------------- ----------------------- -----------------------
  `Unit`                  String                  Sales unit code used by
                                                  the product.

  `UnitsPerPallet`        Number                  Number of units
                                                  contained in one full
                                                  pallet.

  `KgPerPallet`           Number                  Total weight, in
                                                  kilograms, of one full
                                                  pallet.

  `KgPerUnit`             Number                  Weight, in kilograms,
                                                  of one unit.
  -----------------------------------------------------------------------

## Example

``` json
{
  "meta": {
    "status": "Success",
    "messageCode": "200",
    "messageDescription": "Success"
  },
  "data": [
    {
      "Unit": "S190",
      "UnitsPerPallet": 1536,
      "KgPerPallet": 291.84,
      "KgPerUnit": 0.19
    },
    {
      "Unit": "SA20A",
      "UnitsPerPallet": 51,
      "KgPerPallet": 1020,
      "KgPerUnit": 20
    }
  ]
}
```

## Usage

The `Unit` field provides the link with the product sales unit exposed
by other NETPET interfaces.

``` text
Unit: S190

1 unit   = 0.19 kg
1 pallet = 1536 units
1 pallet = 291.84 kg
```

This information allows Addverb to convert production requirements
between **tonnes, kilograms, units/bags and pallets**.

### Conversion Examples

``` text
Units -> Kg       = Units x KgPerUnit
Kg -> Units       = Kg / KgPerUnit
Pallets -> Units  = Pallets x UnitsPerPallet
Pallets -> Kg     = Pallets x KgPerPallet
Tonnes -> Kg      = Tonnes x 1000
```

> **Important:** `Unit` should be treated as a string and used as the
> integration key for unit conversion.
