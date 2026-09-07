"""Phase 6 data preparation — download, clean, and analyse UCI Online Retail II.

This is the script that produces every Phase-6 number in the README. It is
the missing piece a fresh clone needs: without it, `data/sales_clean.csv`
does not exist and `harness/simulate.py` cannot run.

Steps, in order:

  1. download   data/online_retail_II.zip  from the UCI ML repository,
                 if data/online_retail_II.xlsx is not already present
  2. unzip      -> data/online_retail_II.xlsx
  3. convert    both Excel sheets ("Year 2009-2010", "Year 2010-2011")
                 -> data/online_retail_II.csv
  4. clean      -> data/sales_clean.csv, keeping only:
                   - non-credit invoices (Invoice not starting with 'C')
                   - Quantity > 0 and Price > 0
                   - StockCode matching ^\\d{5,6}[A-Za-z]?$
                     (this drops postage/adjustment/fee pseudo-codes such as
                     POST, D, M, BANK CHARGES, ADJUST, DOT, C2, ...)
                 then computes, per StockCode, ListPrice = median(Price)
                 across the surviving rows, and dev_pct = the deviation of
                 each line's Price from that ListPrice, and drops rows with
                 |dev_pct| > 500 as data-quality noise (mispunched prices,
                 not real commercial deviation).
  5. analyse    -> reports/dataset_analysis.json, with two independent
                 answers to "how often does real commerce sell off list":

                 median_reference     — list price := median price per SKU
                                        across ALL customers. This is what
                                        the README headline number uses.
                                        It is partly circular for a
                                        wholesaler with two price points
                                        (wholesale + retail): a SKU sold at
                                        two prices in roughly equal volume
                                        will show ~half its lines "off
                                        list" by construction, regardless
                                        of whether any individual sale was
                                        a pricing mistake.

                 per_customer_reference — list price := the modal (most
                                        common) price that SPECIFIC
                                        customer paid for that SKU. This
                                        asks a different, less circular
                                        question: "how often does a
                                        customer pay something other than
                                        their own usual price for this
                                        item?" It is computed only over
                                        lines that have a Customer ID.

                 Two further cuts of the per-customer view are reported
                 alongside it, because a (SKU, customer) pair bought only
                 once is trivially "at reference" (with one sample, the
                 mode IS that sale):

                 per_customer_reference_excl_single_purchase — the same
                                        per-customer definition, excluding
                                        pairs bought exactly once, so it
                                        isolates cases where the
                                        customer's usual price is actually
                                        established by repetition.

                 per_customer_reference_min6_purchases — restricted
                                        further, to pairs with at least 6
                                        purchases: an established,
                                        well-sampled buying pattern.

                 All four are reported side by side, undoctored. This
                 script does not decide which is "correct" — that is a
                 judgment call about what "list price" should mean, and
                 the reader should see all of them.

Usage:
    ./.venv/bin/python harness/prepare_data.py

Idempotent: if data/online_retail_II.xlsx already exists, the download and
unzip are skipped. The CSV conversion, cleaning, and analysis steps always
re-run (they are the actual "did this reproduce" check, so short-circuiting
them would defeat the point) and always overwrite their outputs.
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import zipfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd  # noqa: E402
import requests  # noqa: E402

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
REPORTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports")

SOURCE_URL = "https://archive.ics.uci.edu/static/public/502/online+retail+ii.zip"
ZIP_PATH = os.path.join(DATA_DIR, "online_retail_II.zip")
XLSX_PATH = os.path.join(DATA_DIR, "online_retail_II.xlsx")
RAW_CSV_PATH = os.path.join(DATA_DIR, "online_retail_II.csv")
CLEAN_CSV_PATH = os.path.join(DATA_DIR, "sales_clean.csv")
ANALYSIS_PATH = os.path.join(REPORTS_DIR, "dataset_analysis.json")

SHEETS = ["Year 2009-2010", "Year 2010-2011"]
STOCKCODE_RE = re.compile(r"^\d{5,6}[A-Za-z]?$")
# A line is "at list" if it is within half a penny of the reference price.
# Below that, floating-point noise from median/mode computation would
# otherwise misclassify exact matches as off-list.
TOL = 0.005
IMPLAUSIBLE_DEV_PCT = 500


def download_and_extract() -> None:
    if os.path.exists(XLSX_PATH):
        print(f"  [skip] {XLSX_PATH} already present")
        return
    os.makedirs(DATA_DIR, exist_ok=True)
    print(f"  downloading {SOURCE_URL}")
    r = requests.get(SOURCE_URL, timeout=300, stream=True)
    r.raise_for_status()
    with open(ZIP_PATH, "wb") as f:
        for chunk in r.iter_content(chunk_size=1 << 20):
            f.write(chunk)
    print(f"  wrote {ZIP_PATH} ({os.path.getsize(ZIP_PATH):,} bytes)")

    print(f"  extracting xlsx from {ZIP_PATH}")
    with zipfile.ZipFile(ZIP_PATH) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".xlsx")]
        if not names:
            raise RuntimeError(f"no .xlsx member found in {ZIP_PATH}: {zf.namelist()}")
        with zf.open(names[0]) as src, open(XLSX_PATH, "wb") as dst:
            dst.write(src.read())
    print(f"  wrote {XLSX_PATH} ({os.path.getsize(XLSX_PATH):,} bytes)")


def convert_to_csv() -> pd.DataFrame:
    print(f"  reading {XLSX_PATH} (sheets: {SHEETS})")
    frames = []
    for sheet in SHEETS:
        sheet_df = pd.read_excel(XLSX_PATH, sheet_name=sheet, dtype={"Invoice": str, "StockCode": str})
        print(f"    {sheet}: {len(sheet_df):,} rows")
        frames.append(sheet_df)
    df = pd.concat(frames, ignore_index=True)
    df = df.rename(columns={"Customer ID": "CustomerID"})
    os.makedirs(DATA_DIR, exist_ok=True)
    df.to_csv(RAW_CSV_PATH, index=False)
    print(f"  wrote {RAW_CSV_PATH} ({len(df):,} rows)")
    return df


def clean_and_analyse(df: pd.DataFrame) -> dict:
    raw_rows = len(df)

    non_credit = df[~df.Invoice.astype(str).str.startswith("C", na=False)]
    credit_dropped = raw_rows - len(non_credit)

    positive = non_credit[(non_credit.Quantity > 0) & (non_credit.Price > 0)]
    nonpositive_dropped = len(non_credit) - len(positive)

    is_product_code = positive.StockCode.astype(str).apply(lambda s: bool(STOCKCODE_RE.match(s)))
    products = positive[is_product_code].copy()
    noise_lines_stripped = len(positive) - len(products)

    list_price = products.groupby("StockCode").Price.median()
    products["ListPrice"] = products.StockCode.map(list_price)
    products["dev_pct"] = (products.Price - products.ListPrice) / products.ListPrice * 100

    clean = products[products.dev_pct.abs() <= IMPLAUSIBLE_DEV_PCT].copy()
    implausible_dropped = len(products) - len(clean)

    os.makedirs(DATA_DIR, exist_ok=True)
    clean.to_csv(CLEAN_CSV_PATH, index=False)
    print(f"  wrote {CLEAN_CSV_PATH} ({len(clean):,} rows)")

    # ---- median_reference: list price = median price per SKU, all customers pooled ----
    diff = clean.Price - clean.ListPrice
    at_list = int((diff.abs() < TOL).sum())
    below_list = int((diff <= -TOL).sum())
    above_list = int((diff >= TOL).sum())
    off_list = below_list + above_list
    lines_analysed = len(clean)
    off_list_pct = off_list / lines_analysed * 100

    off_mask = diff.abs() >= TOL
    revenue_total = float((clean.Price * clean.Quantity).sum())
    revenue_off = float((clean.loc[off_mask, "Price"] * clean.loc[off_mask, "Quantity"]).sum())
    off_list_revenue_pct = revenue_off / revenue_total * 100

    sku_price_counts = clean.groupby("StockCode").Price.nunique()
    skus_multi_price_pct = float((sku_price_counts > 1).sum() / len(sku_price_counts) * 100)

    median_reference = {
        "description": (
            "list price := median(Price) per StockCode, pooled across all "
            "customers. Partly circular for a wholesaler with two price "
            "points (wholesale + retail): a SKU split roughly evenly "
            "between them will show ~half its lines off-list by "
            "construction, independent of whether any given sale was a "
            "pricing mistake."
        ),
        "lines_analysed": lines_analysed,
        "at_list": at_list,
        "below_list": below_list,
        "above_list": above_list,
        "off_list_pct": off_list_pct,
        "off_list_revenue_pct": off_list_revenue_pct,
    }

    # ---- per_customer_reference: list price = modal price that specific customer paid ----
    with_customer = clean[clean.CustomerID.notna()].copy()
    lines_excluded_no_customer_id = len(clean) - len(with_customer)

    def _mode_price(s: pd.Series) -> float:
        m = s.mode()
        return float(m.iloc[0]) if len(m) else float("nan")

    pair = with_customer.groupby(["StockCode", "CustomerID"]).Price
    modal_price = pair.agg(_mode_price)
    pair_size = pair.size()
    pair_index = with_customer.set_index(["StockCode", "CustomerID"]).index
    with_customer["ModalPrice"] = pair_index.map(modal_price)
    with_customer["PairPurchases"] = pair_index.map(pair_size)

    def _reference_stats(d: pd.DataFrame, ref_col: str) -> dict:
        """at/off counts and revenue share of lines vs. a per-line reference price."""
        diff = d.Price - d[ref_col]
        at = int((diff.abs() < TOL).sum())
        off_mask = diff.abs() >= TOL
        off = int(off_mask.sum())
        n = len(d)
        revenue_total = float((d.Price * d.Quantity).sum())
        revenue_off = float((d.loc[off_mask, "Price"] * d.loc[off_mask, "Quantity"]).sum())
        return {
            "lines": n,
            "at_reference": at,
            "off_reference": off,
            "off_list_pct": off / n * 100 if n else float("nan"),
            "off_list_revenue_pct": revenue_off / revenue_total * 100 if revenue_total else float("nan"),
        }

    # A pair bought only once is trivially "at reference": with one sample,
    # the mode IS that sale, so it can never register as off-list. Reported
    # separately so that fact is visible rather than diluting the other cuts.
    single_purchase = with_customer[with_customer.PairPurchases == 1]
    repeat_purchase = with_customer[with_customer.PairPurchases >= 2]
    established_pattern = with_customer[with_customer.PairPurchases >= 6]

    per_customer_reference = {
        "description": (
            "list price := the modal (most frequent) price that THIS "
            "customer paid for THIS SKU, i.e. each customer's own usual "
            "price treated as their list. Answers 'how often does a "
            "customer pay something other than what they usually pay for "
            "this item', which is not circular in the same way the "
            "pooled-median definition is. Computed only over lines that "
            "carry a Customer ID."
        ),
        "lines_with_customer_id": len(with_customer),
        "lines_excluded_no_customer_id": lines_excluded_no_customer_id,
        **_reference_stats(with_customer, "ModalPrice"),
    }

    per_customer_reference_excl_single_purchase = {
        "description": (
            "Same as per_customer_reference, but excludes (StockCode, "
            "CustomerID) pairs bought only once: with a single sample the "
            "mode is that sale, so it is trivially 'at reference' and "
            "cannot show a deviation. This isolates cases where the "
            "customer's usual price is actually established by repetition."
        ),
        "single_purchase_pairs_pct_of_customer_lines": (
            len(single_purchase) / len(with_customer) * 100 if len(with_customer) else float("nan")
        ),
        **_reference_stats(repeat_purchase, "ModalPrice"),
    }

    per_customer_reference_min6_purchases = {
        "description": (
            "Same reference (each customer's own modal price for the SKU), "
            "restricted to (StockCode, CustomerID) pairs with at least 6 "
            "purchases -- an established, well-sampled buying pattern, not "
            "just 'not a one-off'."
        ),
        **_reference_stats(established_pattern, "ModalPrice"),
    }

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        # Top-level keys kept flat and unchanged for backward compatibility
        # with anything reading this file already -- these mirror
        # median_reference exactly, since that is the definition the
        # original README prose uses.
        "lines_analysed": lines_analysed,
        "at_list": at_list,
        "below_list": below_list,
        "above_list": above_list,
        "off_list_pct": off_list_pct,
        "off_list_revenue_pct": off_list_revenue_pct,
        "skus_multi_price_pct": skus_multi_price_pct,
        "noise_lines_stripped": noise_lines_stripped,
        "implausible_dropped": implausible_dropped,
        "method": (
            "list price = median price per SKU; non-product codes and "
            ">500% deviations excluded"
        ),
        "median_reference": median_reference,
        "per_customer_reference": per_customer_reference,
        "per_customer_reference_excl_single_purchase": per_customer_reference_excl_single_purchase,
        "per_customer_reference_min6_purchases": per_customer_reference_min6_purchases,
    }
    return report


def main() -> int:
    print("== step 1/4: download + extract ==")
    download_and_extract()

    print("== step 2/4: convert xlsx -> csv ==")
    if os.path.exists(RAW_CSV_PATH):
        print(f"  {RAW_CSV_PATH} exists; regenerating from xlsx to stay in sync")
    df = convert_to_csv()

    print("== step 3/4: clean + write sales_clean.csv ==")
    report = clean_and_analyse(df)

    print("== step 4/4: write dataset_analysis.json ==")
    os.makedirs(REPORTS_DIR, exist_ok=True)
    with open(ANALYSIS_PATH, "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"  wrote {ANALYSIS_PATH}")

    print("\n" + "=" * 70)
    print(f"  lines analysed          : {report['lines_analysed']:,}")
    print(f"  at list                 : {report['at_list']:,}  "
          f"({report['at_list'] / report['lines_analysed'] * 100:.1f}%)")
    print(f"  below list              : {report['below_list']:,}")
    print(f"  above list              : {report['above_list']:,}")
    print(f"  OFF LIST (median ref)   : {report['median_reference']['off_list_pct']:.1f}%  "
          f"(revenue {report['median_reference']['off_list_revenue_pct']:.1f}%)")
    pc = report["per_customer_reference"]
    print(f"  OFF LIST (per-cust ref) : {pc['off_list_pct']:.1f}%  "
          f"(revenue {pc['off_list_revenue_pct']:.1f}%)  "
          f"[{pc['lines_with_customer_id']:,} lines with a Customer ID]")
    pcx = report["per_customer_reference_excl_single_purchase"]
    print(f"    excl. single-purchase pairs ({pcx['single_purchase_pairs_pct_of_customer_lines']:.1f}% "
          f"of those lines) : {pcx['off_list_pct']:.1f}%  (revenue {pcx['off_list_revenue_pct']:.1f}%)")
    pc6 = report["per_customer_reference_min6_purchases"]
    print(f"    pairs with 6+ purchases only            : {pc6['off_list_pct']:.1f}%  "
          f"(revenue {pc6['off_list_revenue_pct']:.1f}%)  [{pc6['lines']:,} lines]")
    print(f"  SKUs multi-price        : {report['skus_multi_price_pct']:.1f}%")
    print(f"  noise lines stripped    : {report['noise_lines_stripped']:,}")
    print(f"  implausible dropped     : {report['implausible_dropped']:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
