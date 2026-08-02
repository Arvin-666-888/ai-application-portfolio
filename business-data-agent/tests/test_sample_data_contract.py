import sqlite3
from pathlib import Path


SAMPLE_DB = Path(__file__).resolve().parents[1] / "sample_data" / "sample.db"
EXPECTED_COLUMNS = {
    "sales_records": {
        "shop_id", "platform", "marketplace", "timezone", "currency",
        "order_date", "sku", "product_name", "units_sold", "gross_sales",
        "refunds", "platform_fees", "cogs",
    },
    "ad_performance": {
        "shop_id", "platform", "marketplace", "timezone", "currency",
        "report_date", "campaign_name", "sku", "impressions", "clicks",
        "ad_spend", "attributed_sales", "attributed_refunds",
        "attributed_platform_fees", "attributed_cogs", "attributed_orders",
    },
    "inventory_snapshots": {
        "shop_id", "platform", "marketplace", "timezone", "currency",
        "snapshot_date", "sku", "product_name", "on_hand_units",
        "average_inventory_units_30d", "inbound_units",
        "trailing_30d_units_sold", "unit_cost",
    },
    "competitor_prices": {
        "shop_id", "platform", "marketplace", "timezone", "currency",
        "observed_at", "sku", "product_name", "own_price",
        "competitor_name", "competitor_price",
    },
}


def test_tracked_sample_database_contract_is_read_only_and_complete():
    assert SAMPLE_DB.is_file()
    connection = sqlite3.connect(f"file:{SAMPLE_DB.as_posix()}?mode=ro", uri=True)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert tables == set(EXPECTED_COLUMNS)
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

        for table, expected_columns in EXPECTED_COLUMNS.items():
            actual_columns = {
                row[1] for row in connection.execute(f"PRAGMA table_info('{table}')")
            }
            assert expected_columns <= actual_columns
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] > 0

        scopes = {
            row
            for row in connection.execute(
                "SELECT shop_id, marketplace, currency, timezone FROM sales_records"
            )
        }
        assert scopes == {
            ("amazon-us", "US", "USD", "America/Los_Angeles"),
            ("tiktok-uk", "UK", "GBP", "Europe/London"),
            ("shopee-sg", "SG", "SGD", "Asia/Singapore"),
        }
    finally:
        connection.close()
