"""
Tank Farm Inventory Reconciler
--------------------------------
Reads daily tank gauge readings from a CSV file, applies temperature
and density corrections to get true product volumes, then compares
opening vs closing stock to flag discrepancies and generate a report.

Usage:
    python tank_inventory_reconciler.py gauge_readings.csv
    python tank_inventory_reconciler.py gauge_readings.csv --tolerance 1.0
    python tank_inventory_reconciler.py gauge_readings.csv --strapping tank_strapping.csv

Expected CSV columns:
    tank_id, product, date, opening_gauge_mm, closing_gauge_mm,
    tank_capacity_litres, temperature_c, receipts_litres, transfers_litres

Optional column (used for mass reconciliation if present):
    density_kg_m3

Optional strapping table CSV (--strapping):
    tank_id, gauge_mm, volume_litres
    Maps gauge heights to calibrated volumes per tank.
    If not provided, linear interpolation is used as a fallback.

A sample CSV (gauge_readings.csv) is included in this repo for reference.

Author: Ogundare Oluwaseyi
"""

import csv
import os
import sys
import logging
import argparse
from datetime import datetime


# ── Logging setup ─────────────────────────────────────────────────────────────
# Using structured logging instead of bare print() so output level can be
# controlled at runtime (e.g. suppress INFO in production, keep WARNING+).
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO
)
log = logging.getLogger(__name__)


# ── Constants ─────────────────────────────────────────────────────────────────
REFERENCE_TEMP_C  = 15.0     # Standard reference temperature (ASTM/IP basis)
VCF_COEFFICIENT   = 0.00065  # Simplified VCF coefficient for PMS/AGO/DPK
                              # NOTE: Full accuracy requires ASTM Table 54B/54C
                              # lookup by density and temperature. This linear
                              # approximation is adequate for variance flagging
                              # but should not be used for custody transfer.
DEFAULT_TOLERANCE = 0.5      # Default variance threshold (%)
MAX_GAUGE_MM      = 4000     # Default max gauge height if no strapping table


# ── Strapping table (calibration) ─────────────────────────────────────────────

def load_strapping_table(filepath):
    """
    Loads a tank strapping table from CSV.
    Returns a dict: { tank_id: [(gauge_mm, volume_litres), ...] }
    sorted by gauge_mm ascending for binary search interpolation.

    Strapping tables map physical gauge heights to calibrated volumes,
    accounting for tank shape irregularities, welds, and fittings that
    make the real relationship non-linear.

    CSV format:
        tank_id, gauge_mm, volume_litres
        T01, 0, 0
        T01, 500, 63200
        T01, 1000, 127100
        ...
    """
    tables = {}

    if not os.path.exists(filepath):
        log.error(f"Strapping table file not found: '{filepath}'")
        sys.exit(1)

    with open(filepath, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            tank_id    = row["tank_id"].strip()
            gauge_mm   = float(row["gauge_mm"])
            volume_ltr = float(row["volume_litres"])

            if tank_id not in tables:
                tables[tank_id] = []
            tables[tank_id].append((gauge_mm, volume_ltr))

    # Sort each tank's table by gauge height ascending
    for tank_id in tables:
        tables[tank_id].sort(key=lambda x: x[0])

    log.info(f"Strapping table loaded: {len(tables)} tank(s) found in '{filepath}'")
    return tables


def interpolate_volume(gauge_mm, calibration_points):
    """
    Converts a gauge reading to volume using linear interpolation
    between two known calibration points in the strapping table.

    If the gauge is below the minimum or above the maximum calibrated
    point, the function extrapolates from the nearest boundary pair
    and logs a warning — extrapolation is less accurate.
    """
    gauges  = [p[0] for p in calibration_points]
    volumes = [p[1] for p in calibration_points]

    # Below minimum calibrated point
    if gauge_mm <= gauges[0]:
        log.warning(f"Gauge {gauge_mm}mm is below calibration range minimum "
                    f"({gauges[0]}mm). Extrapolating.")
        g0, v0 = calibration_points[0]
        g1, v1 = calibration_points[1]
    # Above maximum calibrated point
    elif gauge_mm >= gauges[-1]:
        log.warning(f"Gauge {gauge_mm}mm exceeds calibration range maximum "
                    f"({gauges[-1]}mm). Extrapolating.")
        g0, v0 = calibration_points[-2]
        g1, v1 = calibration_points[-1]
    else:
        # Find the two surrounding calibration points
        for i in range(len(gauges) - 1):
            if gauges[i] <= gauge_mm <= gauges[i + 1]:
                g0, v0 = calibration_points[i]
                g1, v1 = calibration_points[i + 1]
                break

    # Linear interpolation formula
    if g1 == g0:
        return v0
    ratio = (gauge_mm - g0) / (g1 - g0)
    return round(v0 + ratio * (v1 - v0), 2)


def gauge_to_volume(gauge_mm, tank_id, tank_capacity_litres,
                    strapping_tables=None):
    """
    Converts a gauge reading to volume in litres.

    If a strapping table is available for this tank, uses calibrated
    interpolation (more accurate, accounts for tank shape).

    Falls back to linear approximation if no strapping table is provided,
    which assumes a perfect vertical cylinder — acceptable for screening
    purposes but less accurate for real custody transfer work.
    """
    if strapping_tables and tank_id in strapping_tables:
        return interpolate_volume(gauge_mm, strapping_tables[tank_id])

    # Linear fallback
    if gauge_mm < 0 or gauge_mm > MAX_GAUGE_MM:
        raise ValueError(
            f"Gauge reading {gauge_mm}mm is outside valid range "
            f"(0–{MAX_GAUGE_MM}mm). Check input data."
        )
    return round((gauge_mm / MAX_GAUGE_MM) * tank_capacity_litres, 2)


# ── Volume and mass corrections ───────────────────────────────────────────────

def apply_vcf(volume_litres, temp_c):
    """
    Applies a simplified Volume Correction Factor (VCF) to normalise
    observed volume at ambient temperature to standard 15°C reference.

    Formula: VCF = 1 - K × (T_observed - T_reference)
    where K = 0.00065 (approximate for middle-distillate products).

    Limitation: Full ASTM Table 54B/54C correction requires a density
    lookup at 15°C and is product-specific. This simplified form is
    suitable for variance detection but not for custody transfer metering.
    """
    vcf = 1 - (VCF_COEFFICIENT * (temp_c - REFERENCE_TEMP_C))
    return round(volume_litres * vcf, 2)


def volume_to_mass(volume_litres, density_kg_m3):
    """
    Converts a volume in litres to mass in kilograms.
    1 litre = 0.001 m³, so mass = volume × density × 0.001

    Mass-based reconciliation is more accurate for custody transfer
    because density is not affected by temperature changes.
    Returns None if density is missing or zero.
    """
    if not density_kg_m3 or density_kg_m3 <= 0:
        return None
    return round(volume_litres * density_kg_m3 * 0.001, 2)


# ── Reconciliation logic ──────────────────────────────────────────────────────

def theoretical_closing(opening_vol, receipts, transfers):
    """
    Book stock formula:
        Theoretical Closing = Opening Stock + Receipts - Transfers
    This is what the closing stock should be with zero loss or gain.
    """
    return round(opening_vol + receipts - transfers, 2)


def calculate_variance(theoretical, actual):
    """
    Compares actual closing stock to theoretical.
    Returns (absolute_variance_litres, percentage_variance).
    Positive = gain, Negative = loss.
    """
    absolute = round(actual - theoretical, 2)
    percent  = round((absolute / theoretical) * 100, 4) if theoretical != 0 else 0.0
    return absolute, percent


def flag_status(percent_variance, tolerance):
    """
    Classifies variance as OK / WARNING / CRITICAL based on
    the tolerance threshold passed in from CLI or config.
    """
    abs_var = abs(percent_variance)
    if abs_var <= tolerance:
        return "OK"
    elif abs_var <= tolerance * 3:
        return "WARNING"
    else:
        return "CRITICAL"


# ── File handling ─────────────────────────────────────────────────────────────

REQUIRED_COLUMNS = {
    "tank_id", "product", "date",
    "opening_gauge_mm", "closing_gauge_mm",
    "tank_capacity_litres", "temperature_c",
    "receipts_litres", "transfers_litres"
}

# density_kg_m3 is optional — used if present, ignored if absent
OPTIONAL_COLUMNS = {"density_kg_m3"}


def validate_csv(filepath):
    """
    Checks the file exists and contains all required columns.
    Warns if optional columns are missing (they won't cause failure).
    """
    if not os.path.exists(filepath):
        log.error(f"File not found: '{filepath}'")
        sys.exit(1)

    with open(filepath, "r") as f:
        reader = csv.DictReader(f)
        headers = set(reader.fieldnames or [])

    missing_required = REQUIRED_COLUMNS - headers
    if missing_required:
        log.error(f"Missing required columns in '{filepath}':")
        for col in sorted(missing_required):
            log.error(f"  - {col}")
        log.error("See gauge_readings.csv for the correct format.")
        sys.exit(1)

    missing_optional = OPTIONAL_COLUMNS - headers
    if missing_optional:
        log.warning(
            f"Optional column(s) not found: {', '.join(missing_optional)}. "
            f"Mass-based reconciliation will be skipped."
        )

    log.info(f"File validated: '{filepath}'")
    return headers


def run_reconciliation(filepath, tolerance, strapping_tables=None,
                       available_columns=None):
    """
    Main reconciliation loop. Reads each row, runs volume and mass
    calculations, compares actual vs theoretical, and returns results.
    """
    results = []
    errors  = []
    has_density = available_columns and "density_kg_m3" in available_columns

    with open(filepath, "r") as f:
        reader = csv.DictReader(f)

        for line_num, row in enumerate(reader, start=2):
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
                density       = float(row["density_kg_m3"]) if has_density \
                                    and row.get("density_kg_m3", "").strip() \
                                    else None
            except ValueError as e:
                errors.append(f"Line {line_num} ({tank_id}): non-numeric value — {e}")
                continue

            try:
                # Step 1: Gauge → observed volume
                opening_obs = gauge_to_volume(opening_gauge, tank_id,
                                              capacity, strapping_tables)
                closing_obs = gauge_to_volume(closing_gauge, tank_id,
                                              capacity, strapping_tables)

                # Step 2: Temperature correction to 15°C standard
                opening_std = apply_vcf(opening_obs, temp)
                closing_std = apply_vcf(closing_obs, temp)

                # Step 3: Book stock
                theory = theoretical_closing(opening_std, receipts, transfers)

                # Step 4: Variance
                abs_var, pct_var = calculate_variance(theory, closing_std)
                status = flag_status(pct_var, tolerance)

                # Step 5: Mass reconciliation (if density available)
                mass_opening = volume_to_mass(opening_std, density)
                mass_closing = volume_to_mass(closing_std, density)
                mass_theory  = volume_to_mass(theory, density)

            except ValueError as e:
                errors.append(f"Line {line_num} ({tank_id}): {e}")
                continue

            results.append({
                "date":         date,
                "tank_id":      tank_id,
                "product":      product,
                "opening_std":  opening_std,
                "receipts":     receipts,
                "transfers":    transfers,
                "theoretical":  theory,
                "actual":       closing_std,
                "variance_L":   abs_var,
                "variance_%":   pct_var,
                "status":       status,
                "mass_open_kg": mass_opening,
                "mass_close_kg":mass_closing,
                "mass_theory_kg":mass_theory,
                "has_mass":     mass_opening is not None,
            })

    if errors:
        log.warning(f"{len(errors)} row(s) skipped due to data errors:")
        for err in errors:
            log.warning(f"  {err}")

    return results


# ── Report output ─────────────────────────────────────────────────────────────

def print_report(results, source_file, tolerance):
    """
    Prints a formatted reconciliation report to the terminal.
    Shows mass columns if density data was available.
    """
    if not results:
        log.error("No valid data to report. Check your input file.")
        return

    now       = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    separator = "-" * 98
    warnings  = sum(1 for r in results if r["status"] == "WARNING")
    criticals = sum(1 for r in results if r["status"] == "CRITICAL")
    show_mass = any(r["has_mass"] for r in results)

    print("\n" + "=" * 98)
    print("  TANK FARM DAILY INVENTORY RECONCILIATION REPORT")
    print(f"  Source file : {source_file}")
    print(f"  Generated   : {now}")
    print(f"  Tolerance   : ±{tolerance}%")
    print("=" * 98)
    print(
        f"{'Date':<12} {'Tank':<6} {'Prod':<5} {'Opening(L)':>11} {'Receipts(L)':>12} "
        f"{'Transfers(L)':>13} {'Theory(L)':>11} {'Actual(L)':>11} "
        f"{'Var(L)':>9} {'Var%':>8}  {'Status'}"
    )
    print(separator)

    for r in results:
        print(
            f"{r['date']:<12} {r['tank_id']:<6} {r['product']:<5} "
            f"{r['opening_std']:>11,.1f} {r['receipts']:>12,.1f} "
            f"{r['transfers']:>13,.1f} {r['theoretical']:>11,.1f} "
            f"{r['actual']:>11,.1f} {r['variance_L']:>9,.1f} "
            f"{r['variance_%']:>7.3f}%  {r['status']}"
        )

    print(separator)

    # Mass summary if density was provided
    if show_mass:
        print("\n  MASS RECONCILIATION (kg)")
        print("  " + "-" * 70)
        print(f"  {'Tank':<6} {'Product':<6} {'Opening(kg)':>13} "
              f"{'Theory(kg)':>13} {'Actual(kg)':>13}")
        print("  " + "-" * 70)
        for r in results:
            if r["has_mass"]:
                print(f"  {r['tank_id']:<6} {r['product']:<6} "
                      f"{r['mass_open_kg']:>13,.1f} "
                      f"{r['mass_theory_kg']:>13,.1f} "
                      f"{r['mass_close_kg']:>13,.1f}")

    print(
        f"\n  Summary  : {len(results)} tank(s) reconciled  |  "
        f"{warnings} WARNING(s)  |  {criticals} CRITICAL(s)"
    )
    print("=" * 98)

    if criticals > 0:
        log.warning(
            "CRITICAL variance detected. Possible causes: meter fault, leakage, "
            "evaporation loss, or unauthorized product movement. Escalate for investigation."
        )
    elif warnings > 0:
        log.warning("Minor variance detected. Monitor closely over the next cycle.")
    else:
        log.info("All tanks within acceptable tolerance.")


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        description="Tank Farm Inventory Reconciler — processes gauge readings and flags variances.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python tank_inventory_reconciler.py gauge_readings.csv\n"
            "  python tank_inventory_reconciler.py gauge_readings.csv --tolerance 1.0\n"
            "  python tank_inventory_reconciler.py gauge_readings.csv --strapping tank_strapping.csv\n\n"
            "A sample gauge_readings.csv is included in this repo.\n"
            "A sample tank_strapping.csv is also included to demonstrate calibration table usage."
        )
    )
    parser.add_argument(
        "csvfile",
        help="Path to the CSV file containing tank gauge readings"
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=DEFAULT_TOLERANCE,
        help=f"Variance threshold (%%) before flagging WARNING (default: {DEFAULT_TOLERANCE})"
    )
    parser.add_argument(
        "--strapping",
        type=str,
        default=None,
        help="Optional path to a tank strapping table CSV for calibrated gauge-to-volume conversion"
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress INFO log messages, show WARNING and ERROR only"
    )
    return parser.parse_args()


def main():
    args = parse_args()

    if args.quiet:
        logging.getLogger().setLevel(logging.WARNING)

    log.info("Tank Farm Inventory Reconciler started")
    log.info(f"Input file : {args.csvfile}")
    log.info(f"Tolerance  : ±{args.tolerance}%")

    strapping_tables = None
    if args.strapping:
        log.info(f"Strapping table: {args.strapping}")
        strapping_tables = load_strapping_table(args.strapping)
    else:
        log.warning(
            "No strapping table provided. Using linear gauge-to-volume approximation. "
            "For accurate results, supply a calibrated strapping table with --strapping."
        )

    available_columns = validate_csv(args.csvfile)
    results = run_reconciliation(
        args.csvfile, args.tolerance, strapping_tables, available_columns
    )
    print_report(results, args.csvfile, args.tolerance)


if __name__ == "__main__":
    main()

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
