import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import init_db
from app.routers import auth, datasources, analysis

logger = logging.getLogger("kb_qa")

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
    logger.info(f"{settings.APP_NAME} started")
    logger.info(f"API_KEY configured: {'Yes' if settings.API_KEY else 'No (using mock mode)'}")
    yield
    logger.info(f"{settings.APP_NAME} shutdown")


def _init_sample_data():
    logger.info("Initializing financial sample database...")
    from sqlalchemy import create_engine, text

    engine = create_engine(f"sqlite:///{settings.SAMPLE_DB_PATH}")
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS employees"))
        conn.execute(text("DROP TABLE IF EXISTS orders"))
        conn.execute(text("DROP TABLE IF EXISTS products"))

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS revenue_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                record_month TEXT NOT NULL,
                customer_name TEXT NOT NULL,
                product_line TEXT NOT NULL,
                region TEXT NOT NULL,
                revenue REAL NOT NULL,
                cost REAL NOT NULL,
                gross_profit REAL NOT NULL,
                gross_margin REAL NOT NULL,
                payment_status TEXT NOT NULL
            )
        """))

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS expense_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                record_month TEXT NOT NULL,
                department TEXT NOT NULL,
                expense_type TEXT NOT NULL,
                amount REAL NOT NULL,
                supplier TEXT NOT NULL
            )
        """))

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS budget_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                record_month TEXT NOT NULL,
                department TEXT NOT NULL,
                budget_amount REAL NOT NULL,
                actual_amount REAL NOT NULL,
                execution_rate REAL NOT NULL
            )
        """))

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS receivables (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_name TEXT NOT NULL,
                invoice_no TEXT NOT NULL,
                invoice_date TEXT NOT NULL,
                due_date TEXT NOT NULL,
                amount REAL NOT NULL,
                paid_amount REAL NOT NULL,
                overdue_days INTEGER NOT NULL,
                status TEXT NOT NULL
            )
        """))

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS cashflow_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                record_month TEXT NOT NULL,
                category TEXT NOT NULL,
                cash_inflow REAL NOT NULL,
                cash_outflow REAL NOT NULL,
                net_cashflow REAL NOT NULL
            )
        """))

        if conn.execute(text("SELECT COUNT(*) FROM revenue_records")).scalar() == 0:
            conn.execute(text("""
                INSERT INTO revenue_records
                (record_month, customer_name, product_line, region, revenue, cost, gross_profit, gross_margin, payment_status) VALUES
                ('2024-01', '华东零售集团', '智能终端', '华东', 1280000, 780000, 500000, 0.3906, '已回款'),
                ('2024-01', '北方制造集团', '数据分析平台', '华北', 960000, 520000, 440000, 0.4583, '已回款'),
                ('2024-02', '南方能源公司', '云订阅服务', '华南', 1120000, 610000, 510000, 0.4554, '部分回款'),
                ('2024-02', '华东零售集团', '智能终端', '华东', 1340000, 820000, 520000, 0.3881, '已回款'),
                ('2024-03', '西部物流公司', '数据分析平台', '西部', 880000, 470000, 410000, 0.4659, '已回款'),
                ('2024-03', '北方制造集团', '云订阅服务', '华北', 1180000, 640000, 540000, 0.4576, '已回款'),
                ('2024-04', '华南医药集团', '智能终端', '华南', 1460000, 910000, 550000, 0.3767, '部分回款'),
                ('2024-04', '南方能源公司', '数据分析平台', '华南', 1020000, 560000, 460000, 0.4510, '已回款'),
                ('2024-05', '华东零售集团', '云订阅服务', '华东', 1250000, 680000, 570000, 0.4560, '已回款'),
                ('2024-05', '西部物流公司', '智能终端', '西部', 920000, 590000, 330000, 0.3587, '逾期'),
                ('2024-06', '北方制造集团', '数据分析平台', '华北', 1540000, 840000, 700000, 0.4545, '已回款'),
                ('2024-06', '华南医药集团', '云订阅服务', '华南', 1090000, 600000, 490000, 0.4495, '部分回款'),
                ('2024-07', '南方能源公司', '智能终端', '华南', 1320000, 820000, 500000, 0.3788, '已回款'),
                ('2024-07', '西部物流公司', '数据分析平台', '西部', 990000, 540000, 450000, 0.4545, '已回款'),
                ('2024-08', '华东零售集团', '数据分析平台', '华东', 1680000, 900000, 780000, 0.4643, '已回款'),
                ('2024-08', '华南医药集团', '智能终端', '华南', 1210000, 760000, 450000, 0.3719, '部分回款'),
                ('2024-09', '北方制造集团', '云订阅服务', '华北', 1420000, 770000, 650000, 0.4577, '已回款'),
                ('2024-09', '南方能源公司', '数据分析平台', '华南', 1160000, 630000, 530000, 0.4569, '已回款'),
                ('2024-10', '华东零售集团', '智能终端', '华东', 1580000, 980000, 600000, 0.3797, '已回款'),
                ('2024-10', '西部物流公司', '云订阅服务', '西部', 940000, 510000, 430000, 0.4574, '逾期'),
                ('2024-11', '华南医药集团', '数据分析平台', '华南', 1760000, 950000, 810000, 0.4602, '已回款'),
                ('2024-11', '北方制造集团', '智能终端', '华北', 1390000, 870000, 520000, 0.3741, '部分回款'),
                ('2024-12', '南方能源公司', '云订阅服务', '华南', 1620000, 880000, 740000, 0.4568, '已回款'),
                ('2024-12', '华东零售集团', '数据分析平台', '华东', 1880000, 1010000, 870000, 0.4628, '已回款')
            """))

        if conn.execute(text("SELECT COUNT(*) FROM expense_records")).scalar() == 0:
            conn.execute(text("""
                INSERT INTO expense_records
                (record_month, department, expense_type, amount, supplier) VALUES
                ('2024-01', '销售部', '市场推广费', 320000, '星河传媒'),
                ('2024-02', '研发部', '云资源费', 280000, '华云科技'),
                ('2024-03', '运营部', '物流服务费', 210000, '通达物流'),
                ('2024-04', '销售部', '渠道服务费', 350000, '启航咨询'),
                ('2024-05', '财务部', '审计咨询费', 120000, '信诚会计师事务所'),
                ('2024-06', '研发部', '软件订阅费', 260000, '智算软件'),
                ('2024-07', '运营部', '仓储服务费', 240000, '安捷仓储'),
                ('2024-08', '销售部', '展会活动费', 410000, '星河传媒'),
                ('2024-09', '研发部', '测试设备费', 300000, '卓越设备'),
                ('2024-10', '人力资源部', '招聘服务费', 160000, '前程人才'),
                ('2024-11', '运营部', '售后服务费', 230000, '通达物流'),
                ('2024-12', '销售部', '年终客户活动费', 450000, '启航咨询')
            """))

        if conn.execute(text("SELECT COUNT(*) FROM budget_records")).scalar() == 0:
            conn.execute(text("""
                INSERT INTO budget_records
                (record_month, department, budget_amount, actual_amount, execution_rate) VALUES
                ('2024-01', '销售部', 300000, 320000, 1.0667),
                ('2024-02', '研发部', 300000, 280000, 0.9333),
                ('2024-03', '运营部', 220000, 210000, 0.9545),
                ('2024-04', '销售部', 330000, 350000, 1.0606),
                ('2024-05', '财务部', 150000, 120000, 0.8000),
                ('2024-06', '研发部', 280000, 260000, 0.9286),
                ('2024-07', '运营部', 230000, 240000, 1.0435),
                ('2024-08', '销售部', 380000, 410000, 1.0789),
                ('2024-09', '研发部', 290000, 300000, 1.0345),
                ('2024-10', '人力资源部', 180000, 160000, 0.8889),
                ('2024-11', '运营部', 250000, 230000, 0.9200),
                ('2024-12', '销售部', 420000, 450000, 1.0714)
            """))

        if conn.execute(text("SELECT COUNT(*) FROM receivables")).scalar() == 0:
            conn.execute(text("""
                INSERT INTO receivables
                (customer_name, invoice_no, invoice_date, due_date, amount, paid_amount, overdue_days, status) VALUES
                ('华东零售集团', 'AR202401001', '2024-01-31', '2024-03-01', 1280000, 1280000, 0, '已回款'),
                ('南方能源公司', 'AR202402001', '2024-02-29', '2024-03-30', 1120000, 720000, 18, '部分逾期'),
                ('华南医药集团', 'AR202404001', '2024-04-30', '2024-05-30', 1460000, 900000, 25, '部分逾期'),
                ('西部物流公司', 'AR202405001', '2024-05-31', '2024-06-30', 920000, 0, 45, '逾期'),
                ('北方制造集团', 'AR202406001', '2024-06-30', '2024-07-30', 1540000, 1540000, 0, '已回款'),
                ('华东零售集团', 'AR202408001', '2024-08-31', '2024-09-30', 1680000, 1680000, 0, '已回款'),
                ('西部物流公司', 'AR202410001', '2024-10-31', '2024-11-30', 940000, 200000, 32, '部分逾期'),
                ('华南医药集团', 'AR202411001', '2024-11-30', '2024-12-30', 1760000, 1760000, 0, '已回款'),
                ('北方制造集团', 'AR202411002', '2024-11-30', '2024-12-30', 1390000, 950000, 10, '部分逾期'),
                ('南方能源公司', 'AR202412001', '2024-12-31', '2025-01-30', 1620000, 1620000, 0, '已回款')
            """))

        if conn.execute(text("SELECT COUNT(*) FROM cashflow_records")).scalar() == 0:
            conn.execute(text("""
                INSERT INTO cashflow_records
                (record_month, category, cash_inflow, cash_outflow, net_cashflow) VALUES
                ('2024-01', '经营活动', 1960000, 1450000, 510000),
                ('2024-02', '经营活动', 1710000, 1580000, 130000),
                ('2024-03', '经营活动', 2060000, 1680000, 380000),
                ('2024-04', '经营活动', 1580000, 1870000, -290000),
                ('2024-05', '经营活动', 1960000, 1760000, 200000),
                ('2024-06', '经营活动', 2630000, 1920000, 710000),
                ('2024-07', '经营活动', 2210000, 2020000, 190000),
                ('2024-08', '经营活动', 2890000, 2150000, 740000),
                ('2024-09', '经营活动', 2580000, 2260000, 320000),
                ('2024-10', '经营活动', 1780000, 1980000, -200000),
                ('2024-11', '经营活动', 3150000, 2410000, 740000),
                ('2024-12', '经营活动', 3500000, 2680000, 820000)
            """))

    logger.info("Financial sample database initialized")


app = FastAPI(
    title=settings.APP_NAME,
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(AppException)
async def app_exception_handler(request: Request, exc: AppException):
    return JSONResponse(
        status_code=exc.code,
        content={"code": exc.code, "message": exc.message},
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"code": 500, "message": "服务器内部错误"},
    )


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
