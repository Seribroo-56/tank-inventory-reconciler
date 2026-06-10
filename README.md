# Tank Farm Inventory Reconciler

A Python automation tool for processing daily tank gauge readings, applying petroleum industry-standard volume corrections, and generating a variance report that flags discrepancies in product stock.

Built from hands-on experience in downstream petroleum operations.

---

## What It Does

Tank farms record product volume by measuring the height of liquid in a tank (called a gauge reading). Because petroleum products expand and contract with temperature, raw gauge readings aren't reliable on their own — they need to be corrected to a standard reference temperature before any meaningful comparison can be made.

This tool automates that entire workflow:

1. Reads daily gauge readings from a CSV file
2. Converts gauge heights (mm) to observed volumes (litres)
3. Applies a **Volume Correction Factor (VCF)** to normalise volumes to 15°C reference temperature — a simplified form of the ASTM table correction used in the field
4. Calculates theoretical closing stock using the book formula:
   ```
   Theoretical Closing = Opening Stock + Receipts - Transfers
   ```
5. Compares theoretical vs actual closing stock
6. Flags each tank as **OK**, **WARNING**, or **CRITICAL** based on variance tolerance
7. Prints a formatted reconciliation report to the terminal

---

## Sample Output

```
===========================================================================================
  TANK FARM DAILY INVENTORY RECONCILIATION REPORT
  Source file : gauge_readings.csv
  Generated   : 2024-06-01 08:30:00
===========================================================================================
Date         Tank   Prod   Opening(L)  Receipts(L)  Transfers(L)   Theory(L)   Actual(L)   Var(L)    Var%  Status
-------------------------------------------------------------------------------------------
2024-06-01   T01    PMS    296,587.5    50,000.0      80,000.0     266,587.5   265,693.0    -894.5  -0.336%  OK
2024-06-01   T02    AGO    575,205.0         0.0      20,000.0     555,205.0   565,927.5  10,722.5   1.931%  CRITICAL
-------------------------------------------------------------------------------------------

  Summary  : 5 tank(s) reconciled  |  0 WARNING(s)  |  2 CRITICAL(s)
  Tolerance: ±0.5%

[ALERT] CRITICAL variance detected on one or more tanks.
        Possible causes: meter fault, leakage, evaporation loss,
        or unauthorized product movement. Escalate for investigation.
```

---

## Usage

```bash
python tank_inventory_reconciler.py gauge_readings.csv
```

Pass any CSV file that matches the expected column format.

---

## Input Format

Your CSV file must have these columns (see `gauge_readings.csv` for a working example):

| Column | Description |
|---|---|
| `tank_id` | Tank identifier (e.g. T01) |
| `product` | Product type (PMS, AGO, DPK, etc.) |
| `date` | Reading date (YYYY-MM-DD) |
| `opening_gauge_mm` | Opening gauge height in millimetres |
| `closing_gauge_mm` | Closing gauge height in millimetres |
| `tank_capacity_litres` | Total tank capacity in litres |
| `temperature_c` | Ambient product temperature in Celsius |
| `density_kg_m3` | Product density in kg/m³ |
| `receipts_litres` | Volume received into tank during the day |
| `transfers_litres` | Volume transferred out of tank during the day |

---

## Variance Thresholds

| Status | Condition |
|---|---|
| OK | Variance within ±0.5% |
| WARNING | Variance between ±0.5% and ±1.5% |
| CRITICAL | Variance beyond ±1.5% |

The threshold is defined as `TOLERANCE_PERCENT` at the top of the script and can be adjusted to match your site's standards.

---

## Requirements

No external libraries needed. Runs on standard Python 3.6+.

```bash
python --version  # Python 3.6 or higher
```

---

## Background

This tool was built using knowledge from a 6-month SIWES internship in downstream petroleum operations at EMADEB Energy Services Limited, Lagos — where daily tank gauging and product reconciliation were core operational tasks.

---

## Author

**Ogundare Oluwaseyi**
B.Tech Chemical Engineering, LAUTECH
[GitHub](https://github.com/Seribroo-56) · [LinkedIn](https://www.linkedin.com/in/oluwaseyi-ayokunle-ogundare)
