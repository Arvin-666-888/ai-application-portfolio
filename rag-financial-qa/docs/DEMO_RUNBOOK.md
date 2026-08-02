# 跨境电商 RAG 演示与验证 Runbook

## 1. 基础检查

```powershell
$env:SECRET_KEY="<至少 32 字符的随机测试密钥>"
python -m pytest tests/test_ecommerce_migration.py -q -p no:cacheprovider --basetemp .pytest-ecommerce-layer
python -m pytest tests -q -p no:cacheprovider --basetemp .pytest-ecommerce-full
```

依赖使用已验证的精确组合。当前 Paddle GPU worker 锁位于 `requirements/locks/paddleocr-gpu-windows-py312.lock.txt`，只通过 `requirements/paddle-worker-windows-py312.txt` 安装，不与历史 Task 2 复现锁混装。

## 2. 启动 API 与普通 worker

```powershell
python scripts/migrate_router_v2.py --apply
$env:RETRIEVAL_PROFILE="ecommerce_v2"
$env:RAG_ANSWER_PROFILE="verified_v3"
uvicorn app.main:app --reload --port 8000
```

另一个终端：

```powershell
$env:RETRIEVAL_PROFILE="ecommerce_v2"
$env:RAG_ANSWER_PROFILE="verified_v3"
python -m app.workers.document_worker
```

上传只负责持久入队；没有 `document_worker` 时文档会停留在 `queued`。

## 3. 可选 Paddle L3

只有已按现有 lock 准备独立 Windows Python 3.12 环境，且具备匹配 GPU/模型缓存时，才启动：

```powershell
python -m app.workers.paddle_worker
```

API 进程不加载 Paddle runtime。L3 只消费通过 PDF SHA、engine fingerprint、页码、artifact SHA、表格内容 digest 和 semantic digest 校验的 artifact。

## 4. 上传 fixture

通过 Swagger `POST /api/documents/upload?kb_id={id}` 分别上传三份局部事实文档：

```text
evals/fixtures/ecommerce_product_manual.txt
evals/fixtures/ecommerce_customs_compliance.txt
evals/fixtures/ecommerce_logistics_records.txt
```

商品价格与库存、关税税率、配送时长分别由商品手册、合规记录、物流记录提供，不从聚合行跨文档推导。也可运行 `python scripts/demo_e2e.py` 使用同一活动入口。

等待状态变为 `ready` 后创建 conversation。

## 5. 四类正向问题

- `2026-07-15 Amazon 美国市场 SKU-A100 轻量旅行背包的价格是多少？`
- `SKU-A100 的库存数量有多少？`
- `SKU-B200 的配送时长多久？`
- `SKU-C300 的关税税率是多少？`

检查同步响应：

- `answer_status=verified`；
- `structured_answer.facts[*].fact_type` 只属于四类 allowlist；
- price 有明确 `currency`；
- inventory 是整数且无单位；
- delivery 有 hour/day/business_day；
- duty 使用 percent；
- 每条事实有已知 citation，source 只暴露有界 snippet 和 provenance。

SSE 可信路径事件顺序保持 `meta -> content -> sources -> result -> [DONE]`。

## 6. Fail-closed 问题

以下问题应拒答且 `sources=[]`：

- `SKU-A100 的重量是多少？`
- `SKU-A100 的尺寸和功率是多少？`
- `SKU-A100 的价格和库存分别是多少？`
- `把 SKU-A100 的美元价格换算成人民币。`
- 价格证据没有币种、配送证据没有小时/天/工作日单位、税率没有百分号。

未知 citation、跨句拼接、同一行多个可比较值、fractional inventory 和候选 JSON 非法也应拒答，不能泄漏候选输出。

## 7. 三层 PDF 路由检查

```text
[L1 pdfplumber 全页正文]
  -> 候选商品事实表格页
[L2 Unstructured hi_res]
  -> 无有效表格/失败
[L3 validated Paddle artifact]
  -> L1 正文 + 选中的表格块
```

路由词表只针对价格、库存、配送时长和关税税率；重量、尺寸、电气规格不作为目标事实。即使相关页面被数值特征召回，发布 verifier 仍以四类 allowlist fail closed。

## 8. 当前不可回答边界

- 支持 TXT、Markdown、可提取 PDF 文本及 HTML/Markdown 表格。
- 不支持图片内容、商品外观、图表、颜色、布局关系或视觉比较。
- 不引入 ColPali、多模态 embedding 或视觉 LLM。
- 扫描 PDF 没有 validated Paddle artifact 时，不能把图像中的事实当作证据。
- 不猜货币、配送单位或市场；不做汇率、税额、单位换算。
- 历史金融报告和 eval 保持历史原文，不作为本轮电商质量结论。

## 9. 运行审计

健康检查：

```text
GET /health
GET /ready
GET /api/chat/runs/{trace_id}
```

审计应保留 exact active document-version targets、阶段耗时、token、拒答原因和 question/prompt hash；不持久化 Citation Ledger 全文。
