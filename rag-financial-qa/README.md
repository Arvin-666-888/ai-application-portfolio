# 跨境电商商品事实 RAG

这是一个基于 FastAPI、ChromaDB 和 OpenAI-compatible API 的纯文本/表格 RAG 原型。系统只发布四类可核验事实：

- `price`：必须有明确币种的价格；
- `inventory_quantity`：整数库存数量；
- `delivery_duration`：带小时、天或工作日单位的配送时长；
- `customs_duty_rate`：带百分号的关税税率。

每条可信事实可绑定 `SKU / product / platform / market / date / citation`。缺失币种或单位时不猜测；重量、尺寸、电压、功率等规格不在回答范围内。

## 架构与数据流

```text
[TXT / MD / PDF 上传]
    │ 文件 SHA + 持久任务
    ▼
[SQLite job / lease / heartbeat]
    │ 解析快照与 artifact hash
    ▼
[L1 pdfplumber 全页正文]
    │ 候选表格页
    ▼
[L2 Unstructured hi_res]
    │ 无有效表格或失败
    ▼
[L3 validated Paddle artifact]
    │ L1 正文 + 可信表格块
    ▼
[versioned Chroma staging]
    │ finalize lease fence + CAS 发布
    ▼
[电商意图检索 / rerank]
    │ 私有 Citation Ledger + verifier
    ├── 校验失败 ──► [fail-closed 拒答，sources=[]]
    ▼
[四类结构化事实 + 最小化引用]
```

L1→L2→L3、artifact SHA、job/lease/heartbeat/finalize、版本化 Chroma staging/CAS 和 Citation Ledger 隐私控制均保留。SQLite 与本地 Chroma 适合单机原型，不等于多机生产基础设施。

## 安装

依赖沿用冻结提交 `8d403ca` 的精确版本组合；PaddleOCR lock 不升级。

```powershell
pip install -r requirements.txt
pip install -r requirements-dev.txt
```

如需 Paddle worker，请在独立 Windows Python 3.12 环境中使用现有锁文件：

```powershell
pip install -r requirements-paddleocr-windows-py312.lock.txt
```

## 本地启动

```powershell
python scripts/migrate_router_v2.py --apply
uvicorn app.main:app --reload --port 8000
```

另一个终端启动普通文档 worker；未启动时，上传任务会停留在 `queued`：

```powershell
python -m app.workers.document_worker
```

只有启用 L3 且已准备独立 Paddle 环境时，才启动：

```powershell
python -m app.workers.paddle_worker
```

Swagger：`http://127.0.0.1:8000/docs`

Docker 模式可同时启动 API 与普通 document worker：

```powershell
copy .env.example .env
docker compose up --build
```

Docker Compose 不包含 Paddle GPU worker；L3 仍需匹配锁文件、共享路径和 fingerprint 的独立受控环境。

## 配置可信链路

默认保留 legacy 配置，显式开启新路径：

```powershell
$env:RETRIEVAL_PROFILE="ecommerce_v2"
$env:RAG_ANSWER_PROFILE="verified_v3"
```

`verified_v3` 只允许上述四类事实。候选答案的事实类型、值、明确单位或币种、身份字段和 citation 必须由同一句话或同一表格行支持；未知 citation、跨片段拼接、歧义多值、额外数字或不支持字段都会拒答。

## 演示问题

上传以下三份活动 fixture 后可提问；事实按商品手册、关税合规、物流记录分别局部绑定，避免从一份聚合清单跨行拼接：

- `evals/fixtures/ecommerce_product_manual.txt`
- `evals/fixtures/ecommerce_customs_compliance.txt`
- `evals/fixtures/ecommerce_logistics_records.txt`

- `2026-07-15 Amazon 美国市场 SKU-A100 的价格是多少？`
- `SKU-A100 的库存数量有多少？`
- `SKU-B200 的配送时长多久？`
- `SKU-C300 的关税税率是多少？`

应拒答：

- `SKU-A100 的重量是多少？`
- `SKU-A100 的尺寸和功率是多少？`
- `把 USD 价格换算成人民币。`
- 证据只有 `79.90` 而没有明确币种的价格问题。

## 测试

Windows 下建议使用项目内 basetemp：

```powershell
python -m pytest tests/test_ecommerce_migration.py tests/test_answer_verification.py tests/test_retrieval.py -q -p no:cacheprovider --basetemp .pytest-ecommerce-layer
python -m pytest tests -q -p no:cacheprovider --basetemp .pytest-ecommerce-full
```

测试覆盖四类事实、单位不猜测、Citation Ledger、fail-closed、PDF 三层路由、artifact 校验、worker lease/heartbeat 和版本化索引发布。本轮 fresh 全量结果为 `378 passed`；活动 `questions.jsonl` 结构校验为 11 条（4 条正向、7 条拒答）。这证明工程合同和固定活动集，不代表真实模型语义质量或重新执行 Paddle GPU OCR。

## 历史金融验收保护

活动电商入口不改变历史 Gate 结论：historical/disclosed Gate B 仍为 provisional `12/24`，缺独立人工 attestation；Gate C 仍为真实失败的 `verified_v3=0/24 accepted`。原 holdout 已解封，规则修复后的正式声明必须使用新的 sealed holdout，并在 Ground Truth 前冻结候选；独立人工复核不能由代码替代。历史金融报告、冻结 eval/artifact 和失败指标保持原文。

## 内容边界

- 支持 TXT、Markdown 和 PDF 中可抽取的纯文本、HTML/Markdown 表格。
- 不引入 ColPali、多模态模型或图像 embedding。
- 图片、图表、颜色、商品外观、版式关系和仅存在于扫描图像且未被 OCR artifact 提取的内容不可回答。
- 跨页表格仅在稳定 header signature 与显式前页语义绑定时继承；不主动进行视觉合并。
- 历史金融 eval、报告和冻结哈希继续作为历史证据保存，不会改写成电商质量结论。新增电商 fixture 和活动测试只证明迁移后的工程合同。

详细步骤见 `docs/DEMO_RUNBOOK.md`，能力与边界摘要见 `PROJECT_SUMMARY.md`。
