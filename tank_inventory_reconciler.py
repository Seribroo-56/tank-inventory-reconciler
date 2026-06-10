"""
Tank Farm Inventory Reconciler
--------------------------------
Reads daily tank gauge readings from a CSV file, applies temperature
and density corrections to get true product volumes, then compares
opening vs closing stock to flag discrepancies and generate a report.

Usage:
    python tank_inventory_reconciler.py gauge_readings.csv
    python tank_inventory_reconciler.py /path/to/your/data.csv

Expected CSV columns:
    tank_id, product, date, opening_gauge_mm, closing_gauge_mm,
    tank_capacity_litres, temperature_c, density_kg_m3,
    receipts_litres, transfers_litres

A sample CSV (gauge_readings.csv) is included in this repo for reference.

Author: Ogundare Oluwaseyi
"""

import csv
import os
import sys
import argparse
from datetime import datetime


# --- Constants ---
REFERENCE_TEMP_C  = 15.0    # Standard reference temperature in Celsius
VCF_COEFFICIENT   = 0.00065 # Volume Correction Factor coefficient (typical for AGO/PMS)
TOLERANCE_PERCENT = 0.5     # Acceptable variance threshold before flagging
MAX_GAUGE_MM      = 4000    # Maximum gauge height assumed for all tanks


# --- Core Calculations ---

def gauge_to_volume(gauge_mm, tank_capacity_litres):
    """
    Converts a gauge reading in millimetres to a product volume in litres.
    Assumes a vertical cylindrical tank with a linear gauge-to-volume relationship.
    """
    if gauge_mm < 0 or gauge_mm > MAX_GAUGE_MM:
        raise ValueError(
            f"Gauge reading {gauge_mm}mm is outside the valid range "
            f"(0 – {MAX_GAUGE_MM}mm). Check your input data."
        )
    ratio = gauge_mm / MAX_GAUGE_MM
    return round(ratio * tank_capacity_litres, 2)


def apply_vcf(volume_litres, temp_c):
    """
    Applies a Volume Correction Factor (VCF) to convert observed volume
    at ambient temperature to volume at standard reference temperature (15°C).

    Formula: VCF = 1 - (coefficient × (observed_temp - reference_temp))
    This is a simplified form of the ASTM table correction used in the field.
    """
    vcf = 1 - (VCF_COEFFICIENT * (temp_c - REFERENCE_TEMP_C))
    return round(volume_litres * vcf, 2)


def theoretical_closing(opening_vol, receipts, transfers):
    """
    Book stock formula:
        Theoretical Closing = Opening Stock + Receipts - Transfers
    This is what the closing volume should be if there are no losses or gains.
    """
    return round(opening_vol + receipts - transfers, 2)


def calculate_variance(theoretical, actual):
    """
    Compares actual closing stock to theoretical closing stock.
    Returns absolute variance (litres) and percentage variance.
    Positive = gain, Negative = loss.
    """
    absolute = round(actual - theoretical, 2)
    percent  = round((absolute / theoretical) * 100, 4) if theoretical != 0 else 0.0
    return absolute, percent


def flag_status(percent_variance):
    """
    Classifies a tank's variance into OK / WARNING / CRITICAL
    based on the tolerance threshold defined at the top of this file.
    """
    abs_var = abs(percent_variance)
    if abs_var <= TOLERANCE_PERCENT:
        return "OK"
    elif abs_var <= TOLERANCE_PERCENT * 3:
        return "WARNING"
    else:
        return "CRITICAL"


# --- File Handling ---

def validate_csv(filepath):
    """
    Checks that the file exists, is readable, and has the expected headers.
    Raises a clear error message if anything is wrong so the user knows
    exactly what to fix rather than getting a confusing Python traceback.
    """
    required_columns = {
        "tank_id", "product", "date",
        "opening_gauge_mm", "closing_gauge_mm",
        "tank_capacity_litres", "temperature_c",
        "density_kg_m3", "receipts_litres", "transfers_litres"
    }

    if not os.path.exists(filepath):
        print(f"\n[ERROR] File not found: '{filepath}'")
        print("  Make sure the path is correct and the file exists.\n")
        sys.exit(1)

    if not filepath.endswith(".csv"):
        print(f"\n[WARNING] '{filepath}' does not have a .csv extension.")
        print("  Proceeding anyway, but check that it is a valid CSV file.\n")

    with open(filepath, "r") as f:
        reader = csv.DictReader(f)
        headers = set(reader.fieldnames or [])
        missing = required_columns - headers
        if missing:
            print(f"\n[ERROR] Missing required columns in '{filepath}':")
            for col in sorted(missing):
                print(f"  - {col}")
            print("\n  See gauge_readings.csv in this repo for the correct format.\n")
            sys.exit(1)

    print(f"[INFO] File validated: '{filepath}'")


def run_reconciliation(filepath):
    """
    Reads each row of gauge data, runs all calculations, and returns
    a list of result dicts — one per tank per day.
    """
    results = []
    errors  = []

    with open(filepath, "r") as f:
        reader = csv.DictReader(f)

        for line_num, row in enumerate(reader, start=2):  # start=2 because row 1 is headers
            tank_id = row["tank_id"].strip()
            product = row["product"].strip()
            date    = row["date"].strip()

            try:
                capacity      = float(row["tank_capacity_litres"])
                temp          = float(row["temperature_c"])
                receipts      = float(row["receipts_litres"])
                transfers     = float(row["transfers_litres"])
                opening_gauge = float(row["opening_gauge_mm"])
                closing_gauge = float(row["closing_gauge_mm"])
            except ValueError as e:
                errors.append(f"  Line {line_num} ({tank_id}): Non-numeric value — {e}")
                continue

            try:
                # Step 1: Gauge readings → observed volumes
                opening_obs = gauge_to_volume(opening_gauge, capacity)
                closing_obs = gauge_to_volume(closing_gauge, capacity)

                # Step 2: Correct to standard temperature
                opening_std = apply_vcf(opening_obs, temp)
                closing_std = apply_vcf(closing_obs, temp)

                # Step 3: Book stock (what closing volume should be)
                theory = theoretical_closing(opening_std, receipts, transfers)

                # Step 4: Compare actual vs theoretical
                abs_var, pct_var = calculate_variance(theory, closing_std)
                status = flag_status(pct_var)

            except ValueError as e:
                errors.append(f"  Line {line_num} ({tank_id}): {e}")
                continue

            results.append({
                "date":        date,
                "tank_id":     tank_id,
                "product":     product,
                "opening_std": opening_std,
                "receipts":    receipts,
                "transfers":   transfers,
                "theoretical": theory,
                "actual":      closing_std,
                "variance_L":  abs_var,
                "variance_%":  pct_var,
                "status":      status,
            })

    # Report any rows that had bad data
    if errors:
        print(f"\n[WARNING] {len(errors)} row(s) were skipped due to data errors:")
        for err in errors:
            print(err)
        print()

    return results


# --- Report Output ---

def print_report(results, source_file):
    """
    Prints a formatted reconciliation report to the terminal.
    """
    if not results:
        print("\n[ERROR] No valid data to report. Check your input file.\n")
        return

    now       = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    separator = "-" * 95
    warnings  = sum(1 for r in results if r["status"] == "WARNING")
    criticals = sum(1 for r in results if r["status"] == "CRITICAL")

    print("\n" + "=" * 95)
    print(f"  TANK FARM DAILY INVENTORY RECONCILIATION REPORT")
    print(f"  Source file : {source_file}")
    print(f"  Generated   : {now}")
    print("=" * 95)
    print(
        f"{'Date':<12} {'Tank':<6} {'Prod':<5} {'Opening(L)':>12} {'Receipts(L)':>12} "
        f"{'Transfers(L)':>13} {'Theory(L)':>11} {'Actual(L)':>11} "
        f"{'Var(L)':>9} {'Var%':>8}  {'Status'}"
    )
    print(separator)

    for r in results:
        print(
            f"{r['date']:<12} {r['tank_id']:<6} {r['product']:<5} "
            f"{r['opening_std']:>12,.1f} {r['receipts']:>12,.1f} "
            f"{r['transfers']:>13,.1f} {r['theoretical']:>11,.1f} "
            f"{r['actual']:>11,.1f} {r['variance_L']:>9,.1f} "
            f"{r['variance_%']:>7.3f}%  {r['status']}"
        )

    print(separator)
    print(
        f"\n  Summary  : {len(results)} tank(s) reconciled  |  "
        f"{warnings} WARNING(s)  |  {criticals} CRITICAL(s)"
    )
    print(f"  Tolerance: ±{TOLERANCE_PERCENT}%\n")
    print("=" * 95)

    if criticals > 0:
        print(
            "\n[ALERT] CRITICAL variance detected on one or more tanks.\n"
            "        Possible causes: meter fault, leakage, evaporation loss,\n"
            "        or unauthorized product movement. Escalate for investigation.\n"
        )
    elif warnings > 0:
        print(
            "\n[NOTE] Minor variance detected. Monitor closely over the next cycle.\n"
        )
    else:
        print("\n[OK] All tanks within acceptable tolerance.\n")


# --- Entry Point ---

def parse_args():
    parser = argparse.ArgumentParser(
        description="Tank Farm Inventory Reconciler — processes gauge readings and flags variances.",
        epilog=(
            "Example:\n"
            "  python tank_inventory_reconciler.py gauge_readings.csv\n\n"
            "A sample CSV is included in this repo to show the expected format."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "csvfile",
        help="Path to the CSV file containing tank gauge readings"
    )
    return parser.parse_args()


def main():
    args = parse_args()

    print(f"\n[INFO] Tank Farm Inventory Reconciler")
    print(f"[INFO] Input file: {args.csvfile}\n")

    validate_csv(args.csvfile)
    results = run_reconciliation(args.csvfile)
    print_report(results, args.csvfile)


if __name__ == "__main__":
    main()
