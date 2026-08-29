"""Import customer name/address/phone/email from a billing-system spreadsheet
export into the customer_billing table. This data does NOT come from Neptune —
Neptune's API has no customer-contact endpoint (see README).

Usage:
    python3 import_billing.py "/path/to/Customer Listing.xlsx"
"""
import sys

import pandas as pd

import neptune_db as db

# Maps the billing export's column headers (as they actually appear, including
# Excel's embedded \r\n line-break artifacts) to our internal field names.
COLUMN_MAP = {
    "Account No.": "account_number",
    "Customer Name": "customer_name",
    "Location": "location",
    "Location No": "location_no",
    "Parcel Id": "parcel_id",
    "Meter Id": "meter_id",
    "Primary_x000D_\nPhone": "primary_phone",
    "Secondary_x000D_\nPhone": "secondary_phone",
    "Email_x000D_\nAddress": "email_address",
}


def _clean_id(value):
    """Excel gives us these as floats/ints (e.g. 1581225704.0); we want a
    plain string with no trailing '.0', matching how Neptune's IDs are stored."""
    if pd.isna(value):
        return None
    if isinstance(value, float):
        return str(int(value))
    return str(value).strip()


def _clean_str(value):
    if pd.isna(value):
        return None
    s = str(value).strip()
    return s or None


def load(path, sheet_name=0):
    df = pd.read_excel(path, sheet_name=sheet_name)
    missing = set(COLUMN_MAP) - set(df.columns)
    if missing:
        raise ValueError(
            f"Expected columns not found in {path}: {missing}. "
            f"Actual columns: {list(df.columns)}"
        )
    df = df.rename(columns=COLUMN_MAP)

    rows = []
    for _, r in df.iterrows():
        rows.append({
            "meter_id": _clean_id(r["meter_id"]),
            "account_number": _clean_id(r["account_number"]),
            "customer_name": _clean_str(r["customer_name"]),
            "location": _clean_str(r["location"]),
            "location_no": _clean_id(r["location_no"]),
            "parcel_id": _clean_str(r["parcel_id"]),
            "primary_phone": _clean_str(r["primary_phone"]),
            "secondary_phone": _clean_str(r["secondary_phone"]),
            "email_address": _clean_str(r["email_address"]),
        })
    rows = [r for r in rows if r["meter_id"]]  # meter_id is the primary key

    conn = db.get_conn()
    db.upsert_customer_billing(conn, rows, source_file=path)
    return len(rows)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python3 import_billing.py <path to .xlsx>")
        sys.exit(1)
    n = load(sys.argv[1])
    print(f"Imported {n} customer_billing rows from {sys.argv[1]}")
