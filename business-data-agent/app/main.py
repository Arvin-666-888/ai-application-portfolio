import logging
import sqlite3
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine, text

from app.config import settings
from app.database import init_db
from app.routers import analysis, auth, datasources

logger = logging.getLogger("business_data_agent")

_SAMPLE_DATA_CONTRACT = {
    "sales_records": {
        "shop_id", "platform", "marketplace", "timezone", "currency", "order_date",
        "sku", "product_name", "units_sold", "gross_sales", "refunds", "platform_fees", "cogs",
    },
    "ad_performance": {
        "shop_id", "platform", "marketplace", "timezone", "currency", "report_date",
        "campaign_name", "sku", "impressions", "clicks", "ad_spend", "attributed_sales",
        "attributed_refunds", "attributed_platform_fees", "attributed_cogs", "attributed_orders",
    },
    "inventory_snapshots": {
        "shop_id", "platform", "marketplace", "timezone", "currency", "snapshot_date",
        "sku", "product_name", "on_hand_units", "average_inventory_units_30d",
        "inbound_units", "trailing_30d_units_sold", "unit_cost",
    },
    "competitor_prices": {
        "shop_id", "platform", "marketplace", "timezone", "currency", "observed_at",
        "sku", "product_name", "own_price", "competitor_name", "competitor_price",
    },
}

logging.basicConfig(
    level=logging.INFO if not settings.DEBUG else logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()],
)


class AppException(Exception):
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    _init_sample_data()
    logger.info("%s started", settings.APP_NAME)
    logger.info("API_KEY configured: %s", "Yes" if settings.API_KEY else "No (using mock mode)")
    yield
    logger.info("%s shutdown", settings.APP_NAME)


def _sample_data_contract_satisfied(path: str) -> bool:
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return False
    try:
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if not set(_SAMPLE_DATA_CONTRACT) <= tables:
            return False
        for table, required_columns in _SAMPLE_DATA_CONTRACT.items():
            columns = {
                row[1] for row in connection.execute(f"PRAGMA table_info('{table}')")
            }
            if not required_columns <= columns:
                return False
            if connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] <= 0:
                return False
        return True
    finally:
        connection.close()


def _init_sample_data():
    # MIGRATION: 企业财务样例表 -> Amazon/TikTok Shop/Shopee 销售、广告、库存与竞品事实表。
    if _sample_data_contract_satisfied(settings.SAMPLE_DB_PATH):
        logger.info("Cross-border ecommerce sample database already satisfies contract")
        return

    logger.info("Initializing cross-border ecommerce sample database...")
    engine = create_engine(f"sqlite:///{settings.SAMPLE_DB_PATH}")
    with engine.begin() as conn:
        for table in (
            "revenue_records",
            "expense_records",
            "budget_records",
            "receivables",
            "cashflow_records",
            "sales_records",
            "ad_performance",
            "inventory_snapshots",
            "competitor_prices",
        ):
            conn.execute(text(f"DROP TABLE IF EXISTS {table}"))

        conn.execute(text("""
            CREATE TABLE sales_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shop_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                marketplace TEXT NOT NULL,
                timezone TEXT NOT NULL,
                currency TEXT NOT NULL,
                order_date TEXT NOT NULL,
                sku TEXT NOT NULL,
                product_name TEXT NOT NULL,
                units_sold INTEGER NOT NULL,
                gross_sales REAL NOT NULL,
                refunds REAL NOT NULL,
                platform_fees REAL NOT NULL,
                cogs REAL NOT NULL
            )
        """))
        conn.execute(text("""
            CREATE TABLE ad_performance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shop_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                marketplace TEXT NOT NULL,
                timezone TEXT NOT NULL,
                currency TEXT NOT NULL,
                report_date TEXT NOT NULL,
                campaign_name TEXT NOT NULL,
                sku TEXT NOT NULL,
                impressions INTEGER NOT NULL,
                clicks INTEGER NOT NULL,
                ad_spend REAL NOT NULL,
                attributed_sales REAL NOT NULL,
                attributed_refunds REAL NOT NULL,
                attributed_platform_fees REAL NOT NULL,
                attributed_cogs REAL NOT NULL,
                attributed_orders INTEGER NOT NULL
            )
        """))
        conn.execute(text("""
            CREATE TABLE inventory_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shop_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                marketplace TEXT NOT NULL,
                timezone TEXT NOT NULL,
                currency TEXT NOT NULL,
                snapshot_date TEXT NOT NULL,
                sku TEXT NOT NULL,
                product_name TEXT NOT NULL,
                on_hand_units INTEGER NOT NULL,
                average_inventory_units_30d REAL NOT NULL,
                inbound_units INTEGER NOT NULL,
                trailing_30d_units_sold INTEGER NOT NULL,
                unit_cost REAL NOT NULL
            )
        """))
        conn.execute(text("""
            CREATE TABLE competitor_prices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shop_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                marketplace TEXT NOT NULL,
                timezone TEXT NOT NULL,
                currency TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                sku TEXT NOT NULL,
                product_name TEXT NOT NULL,
                own_price REAL NOT NULL,
                competitor_name TEXT NOT NULL,
                competitor_price REAL NOT NULL
            )
        """))

        conn.execute(text("""
            INSERT INTO sales_records
            (shop_id, platform, marketplace, timezone, currency, order_date, sku, product_name,
             units_sold, gross_sales, refunds, platform_fees, cogs) VALUES
            ('amazon-us', 'Amazon', 'US', 'America/Los_Angeles', 'USD', '2026-01-05', 'AMZ-HUB-01', 'USB-C Hub', 120, 4798.80, 199.95, 719.82, 1920.00),
            ('amazon-us', 'Amazon', 'US', 'America/Los_Angeles', 'USD', '2026-01-20', 'AMZ-CHG-02', 'GaN Charger', 90, 3599.10, 79.98, 539.87, 1530.00),
            ('amazon-us', 'Amazon', 'US', 'America/Los_Angeles', 'USD', '2026-02-08', 'AMZ-HUB-01', 'USB-C Hub', 145, 5798.55, 119.97, 869.78, 2320.00),
            ('amazon-us', 'Amazon', 'US', 'America/Los_Angeles', 'USD', '2026-02-22', 'AMZ-CHG-02', 'GaN Charger', 110, 4398.90, 119.97, 659.84, 1870.00),
            ('tiktok-uk', 'TikTok Shop', 'UK', 'Europe/London', 'GBP', '2026-01-09', 'TT-LAMP-01', 'Sunset Lamp', 210, 4197.90, 167.92, 629.69, 1470.00),
            ('tiktok-uk', 'TikTok Shop', 'UK', 'Europe/London', 'GBP', '2026-02-11', 'TT-LAMP-01', 'Sunset Lamp', 260, 5197.40, 199.90, 779.61, 1820.00),
            ('shopee-sg', 'Shopee', 'SG', 'Asia/Singapore', 'SGD', '2026-01-14', 'SP-BTL-01', 'Thermal Bottle', 180, 4498.20, 149.94, 674.73, 1710.00),
            ('shopee-sg', 'Shopee', 'SG', 'Asia/Singapore', 'SGD', '2026-02-17', 'SP-BTL-01', 'Thermal Bottle', 205, 5122.95, 124.95, 768.44, 1947.50)
        """))
        conn.execute(text("""
            INSERT INTO ad_performance
            (shop_id, platform, marketplace, timezone, currency, report_date, campaign_name, sku,
             impressions, clicks, ad_spend, attributed_sales, attributed_refunds,
             attributed_platform_fees, attributed_cogs, attributed_orders) VALUES
            ('amazon-us', 'Amazon', 'US', 'America/Los_Angeles', 'USD', '2026-01-31', 'Hub Search', 'AMZ-HUB-01', 84000, 2100, 900.00, 4200.00, 160.00, 630.00, 1680.00, 105),
            ('amazon-us', 'Amazon', 'US', 'America/Los_Angeles', 'USD', '2026-01-31', 'Charger Search', 'AMZ-CHG-02', 61000, 1525, 780.00, 3120.00, 70.00, 468.00, 1326.00, 78),
            ('amazon-us', 'Amazon', 'US', 'America/Los_Angeles', 'USD', '2026-02-28', 'Hub Search', 'AMZ-HUB-01', 96000, 2400, 1080.00, 5184.00, 110.00, 777.60, 2073.60, 130),
            ('amazon-us', 'Amazon', 'US', 'America/Los_Angeles', 'USD', '2026-02-28', 'Charger Search', 'AMZ-CHG-02', 72000, 1800, 920.00, 3864.00, 105.00, 579.60, 1642.20, 96),
            ('tiktok-uk', 'TikTok Shop', 'UK', 'Europe/London', 'GBP', '2026-01-31', 'Creator Spark', 'TT-LAMP-01', 180000, 5400, 1100.00, 3850.00, 150.00, 577.50, 1347.50, 193),
            ('tiktok-uk', 'TikTok Shop', 'UK', 'Europe/London', 'GBP', '2026-02-28', 'Creator Spark', 'TT-LAMP-01', 225000, 7200, 1300.00, 4940.00, 190.00, 741.00, 1729.00, 247),
            ('shopee-sg', 'Shopee', 'SG', 'Asia/Singapore', 'SGD', '2026-01-31', 'Discovery Ads', 'SP-BTL-01', 99000, 2970, 850.00, 3400.00, 120.00, 510.00, 1292.00, 136),
            ('shopee-sg', 'Shopee', 'SG', 'Asia/Singapore', 'SGD', '2026-02-28', 'Discovery Ads', 'SP-BTL-01', 118000, 3540, 960.00, 4032.00, 100.00, 604.80, 1532.16, 161)
        """))
        conn.execute(text("""
            INSERT INTO inventory_snapshots
            (shop_id, platform, marketplace, timezone, currency, snapshot_date, sku, product_name,
             on_hand_units, average_inventory_units_30d, inbound_units, trailing_30d_units_sold, unit_cost) VALUES
            ('amazon-us', 'Amazon', 'US', 'America/Los_Angeles', 'USD', '2026-02-28', 'AMZ-HUB-01', 'USB-C Hub', 220, 180.0, 80, 145, 16.00),
            ('amazon-us', 'Amazon', 'US', 'America/Los_Angeles', 'USD', '2026-02-28', 'AMZ-CHG-02', 'GaN Charger', 75, 105.0, 120, 110, 17.00),
            ('tiktok-uk', 'TikTok Shop', 'UK', 'Europe/London', 'GBP', '2026-02-28', 'TT-LAMP-01', 'Sunset Lamp', 95, 140.0, 60, 260, 7.00),
            ('shopee-sg', 'Shopee', 'SG', 'Asia/Singapore', 'SGD', '2026-02-28', 'SP-BTL-01', 'Thermal Bottle', 310, 255.0, 0, 205, 9.50)
        """))
        conn.execute(text("""
            INSERT INTO competitor_prices
            (shop_id, platform, marketplace, timezone, currency, observed_at, sku, product_name,
             own_price, competitor_name, competitor_price) VALUES
            ('amazon-us', 'Amazon', 'US', 'America/Los_Angeles', 'USD', '2026-02-28T10:00:00-08:00', 'AMZ-HUB-01', 'USB-C Hub', 39.99, 'Brand X', 42.99),
            ('amazon-us', 'Amazon', 'US', 'America/Los_Angeles', 'USD', '2026-02-28T10:00:00-08:00', 'AMZ-CHG-02', 'GaN Charger', 39.99, 'Brand Y', 37.99),
            ('tiktok-uk', 'TikTok Shop', 'UK', 'Europe/London', 'GBP', '2026-02-28T18:00:00+00:00', 'TT-LAMP-01', 'Sunset Lamp', 19.99, 'Glow Home', 18.49),
            ('shopee-sg', 'Shopee', 'SG', 'Asia/Singapore', 'SGD', '2026-03-01T09:00:00+08:00', 'SP-BTL-01', 'Thermal Bottle', 24.99, 'Daily Flask', 26.50)
        """))
    logger.info("Cross-border ecommerce sample database initialized")
    engine.dispose()


app = FastAPI(title=settings.APP_NAME, version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(AppException)
async def app_exception_handler(request: Request, exc: AppException):
    return JSONResponse(status_code=exc.code, content={"code": exc.code, "message": exc.message})


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error("Unhandled exception: %s", exc, exc_info=True)
    return JSONResponse(status_code=500, content={"code": 500, "message": "服务器内部错误"})


app.include_router(auth.router)
app.include_router(datasources.router)
app.include_router(analysis.router)
app.mount("/charts", StaticFiles(directory=settings.CHART_DIR), name="charts")


@app.get("/")
async def root():
    return {
        "app": settings.APP_NAME,
        "version": "1.0.0",
        "docs": "/docs",
        "api_key_configured": bool(settings.API_KEY),
    }


@app.get("/health")
async def health():
    return {"status": "ok"}
