import pandas as pd
from services.snowflake_connection import run_query_cached, run_query_df_cached


def get_filter_options():
    """Return distinct values for filter dropdowns."""
    vendors = run_query_cached("""
        SELECT VENDOR_SK, VENDOR_NAME
        FROM SAP_P2P_FINANCE_DEV.GOLD.DIM_VENDOR
        ORDER BY VENDOR_NAME
    """)
    plants = run_query_cached("""
        SELECT PLANT_SK, PLANT_NAME, PLANT_ID
        FROM SAP_P2P_FINANCE_DEV.GOLD.DIM_PLANT
        ORDER BY PLANT_NAME
    """)
    categories = run_query_cached("""
        SELECT DISTINCT m.MATERIAL_GROUP,
               COALESCE(m.MATERIAL_DESCRIPTION, m.MATERIAL_GROUP) AS CATEGORY_LABEL
        FROM SAP_P2P_FINANCE_DEV.GOLD.DIM_MATERIAL m
        WHERE m.MATERIAL_GROUP IS NOT NULL
        ORDER BY m.MATERIAL_GROUP
    """)
    return vendors, plants, categories


def get_kpi_metrics(fiscal_year, vendor_ids=None, plant_ids=None, categories=None, max_month=None):
    """Get headline KPI metrics for the dashboard. max_month limits to YTD."""
    # Invoice metrics
    inv_where = []
    if fiscal_year:
        inv_where.append(f"i.FISCAL_YEAR = '{fiscal_year}'")
    if vendor_ids:
        ids = ",".join(f"'{v}'" for v in vendor_ids)
        inv_where.append(f"i.VENDOR_SK IN ({ids})")
    if max_month:
        inv_where.append(f"d.MONTH <= {max_month}")
    inv_clause = "WHERE " + " AND ".join(inv_where) if inv_where else ""
    inv_join = "JOIN SAP_P2P_FINANCE_DEV.GOLD.DIM_DATE d ON i.DATE_KEY = d.DATE_KEY" if max_month else ""

    inv = run_query_cached(f"""
        SELECT COUNT(*) AS INVOICE_COUNT,
               COALESCE(SUM(i.GROSS_INVOICE_AMOUNT), 0) AS TOTAL_SPEND,
               COALESCE(AVG(i.GROSS_INVOICE_AMOUNT), 0) AS AVG_INVOICE
        FROM SAP_P2P_FINANCE_DEV.GOLD.FCT_AP_INVOICES i
        {inv_join}
        {inv_clause}
    """)

    # PO metrics
    po_joins = ""
    po_where = []
    if fiscal_year:
        po_where.append(f"po.FISCAL_YEAR = '{fiscal_year}'")
    if vendor_ids:
        ids = ",".join(f"'{v}'" for v in vendor_ids)
        po_where.append(f"po.VENDOR_SK IN ({ids})")
    if plant_ids:
        ids = ",".join(f"'{p}'" for p in plant_ids)
        po_where.append(f"po.PLANT_SK IN ({ids})")
    if categories:
        cats = ",".join(f"'{c}'" for c in categories)
        po_joins += "\nLEFT JOIN SAP_P2P_FINANCE_DEV.GOLD.DIM_MATERIAL m ON po.MATERIAL_SK = m.MATERIAL_SK"
        po_where.append(f"m.MATERIAL_GROUP IN ({cats})")
    if max_month:
        po_joins += "\nJOIN SAP_P2P_FINANCE_DEV.GOLD.DIM_DATE d2 ON po.DATE_KEY = d2.DATE_KEY"
        po_where.append(f"d2.MONTH <= {max_month}")
    po_clause = "WHERE " + " AND ".join(po_where) if po_where else ""

    po = run_query_cached(f"""
        SELECT COUNT(DISTINCT po.PO_ID) AS PO_COUNT,
               COALESCE(SUM(po.GROSS_VALUE), 0) AS PO_GROSS
        FROM SAP_P2P_FINANCE_DEV.GOLD.FCT_PURCHASE_ORDERS po
        {po_joins}
        {po_clause}
    """)

    return {
        "total_spend": inv[0]["TOTAL_SPEND"] if inv else 0,
        "invoice_count": inv[0]["INVOICE_COUNT"] if inv else 0,
        "avg_invoice": inv[0]["AVG_INVOICE"] if inv else 0,
        "po_count": po[0]["PO_COUNT"] if po else 0,
        "po_gross": po[0]["PO_GROSS"] if po else 0,
    }


def get_spend_trend(fiscal_year, vendor_ids=None, plant_ids=None, categories=None):
    """Monthly spend trend for the selected year."""
    where = []
    if fiscal_year:
        where.append(f"i.FISCAL_YEAR = '{fiscal_year}'")
    if vendor_ids:
        ids = ",".join(f"'{v}'" for v in vendor_ids)
        where.append(f"i.VENDOR_SK IN ({ids})")
    clause = "WHERE " + " AND ".join(where) if where else ""

    return run_query_df_cached(f"""
        SELECT d.MONTH_NAME AS MONTH, d.MONTH AS MONTH_NUM,
               SUM(i.GROSS_INVOICE_AMOUNT) AS SPEND
        FROM SAP_P2P_FINANCE_DEV.GOLD.FCT_AP_INVOICES i
        JOIN SAP_P2P_FINANCE_DEV.GOLD.DIM_DATE d ON i.DATE_KEY = d.DATE_KEY
        {clause}
        GROUP BY d.MONTH_NAME, d.MONTH
        ORDER BY d.MONTH
    """)


def get_spend_by_vendor(fiscal_year, vendor_ids=None, plant_ids=None, categories=None):
    """Top vendors by spend."""
    where = []
    if fiscal_year:
        where.append(f"i.FISCAL_YEAR = '{fiscal_year}'")
    if vendor_ids:
        ids = ",".join(f"'{v}'" for v in vendor_ids)
        where.append(f"i.VENDOR_SK IN ({ids})")
    clause = "WHERE " + " AND ".join(where) if where else ""

    return run_query_df_cached(f"""
        SELECT v.VENDOR_NAME, SUM(i.GROSS_INVOICE_AMOUNT) AS SPEND
        FROM SAP_P2P_FINANCE_DEV.GOLD.FCT_AP_INVOICES i
        JOIN SAP_P2P_FINANCE_DEV.GOLD.DIM_VENDOR v ON i.VENDOR_SK = v.VENDOR_SK
        {clause}
        GROUP BY v.VENDOR_NAME
        ORDER BY SPEND DESC
        LIMIT 10
    """)


def get_order_pipeline(fiscal_year, vendor_ids=None, plant_ids=None, categories=None):
    """GR/IR event counts by month — join DIM_DATE for fiscal year."""
    joins = "JOIN SAP_P2P_FINANCE_DEV.GOLD.DIM_DATE d ON h.DATE_KEY = d.DATE_KEY"
    where = []
    if fiscal_year:
        where.append(f"h.FISCAL_YEAR = '{fiscal_year}'")
    if plant_ids:
        ids = ",".join(f"'{p}'" for p in plant_ids)
        where.append(f"h.PLANT_SK IN ({ids})")
    if categories:
        cats = ",".join(f"'{c}'" for c in categories)
        joins += "\nLEFT JOIN SAP_P2P_FINANCE_DEV.GOLD.DIM_MATERIAL m ON h.MATERIAL_SK = m.MATERIAL_SK"
        where.append(f"m.MATERIAL_GROUP IN ({cats})")
    clause = "WHERE " + " AND ".join(where) if where else ""

    return run_query_df_cached(f"""
        SELECT
            CASE h.EVENT_TYPE WHEN '1' THEN 'Goods Receipt' WHEN '2' THEN 'Invoice Receipt' ELSE 'Other' END AS EVENT_TYPE,
            d.MONTH_NAME AS MONTH, d.MONTH AS MONTH_NUM,
            COUNT(*) AS COUNT
        FROM SAP_P2P_FINANCE_DEV.GOLD.FCT_PO_HISTORY h
        {joins}
        {clause}
        GROUP BY 1, 2, 3
        ORDER BY d.MONTH
    """)


def get_spend_by_region(fiscal_year, vendor_ids=None, plant_ids=None, categories=None):
    """Spend grouped by vendor country/region."""
    where = []
    if fiscal_year:
        where.append(f"i.FISCAL_YEAR = '{fiscal_year}'")
    if vendor_ids:
        ids = ",".join(f"'{v}'" for v in vendor_ids)
        where.append(f"i.VENDOR_SK IN ({ids})")
    clause = "WHERE " + " AND ".join(where) if where else ""

    return run_query_df_cached(f"""
        SELECT v.COUNTRY AS REGION, SUM(i.GROSS_INVOICE_AMOUNT) AS SPEND
        FROM SAP_P2P_FINANCE_DEV.GOLD.FCT_AP_INVOICES i
        JOIN SAP_P2P_FINANCE_DEV.GOLD.DIM_VENDOR v ON i.VENDOR_SK = v.VENDOR_SK
        {clause}
        GROUP BY v.COUNTRY
        ORDER BY SPEND DESC
    """)
