# 跨境电商商品事实 RAG 技术概要

## 目标

在不改动成熟摄取与发布基础设施的前提下，把金融领域问答迁移为跨境电商商品事实问答。发布面严格限制为 `price / inventory_quantity / delivery_duration / customs_duty_rate`。

## 数据流

```text
[上传文本/PDF]
  -> [持久 job + lease/heartbeat]
  -> [L1 pdfplumber 正文]
  -> [L2 hi_res 候选表格]
  -> [L3 validated Paddle artifact]
  -> [artifact SHA / 解析快照]
  -> [versioned Chroma staging]
  -> [finalize fence + CAS active version]
  -> [ecommerce_v2 检索与重排]
  -> [私有 Citation Ledger]
  -> [确定性 verifier]
  -> [可信回答或 fail-closed 拒答]
```

## 事实合同

| 类型 | 有效形式 | 拒绝条件 |
|---|---|---|
| `price` | 数值 + 明确 CNY/USD/HKD | 缺币种、猜币种、换算 |
| `inventory_quantity` | 整数 | 小数、猜单位 |
| `delivery_duration` | 数值 + hour/day/business_day | 缺单位、工作日与自然日混用 |
| `customs_duty_rate` | 数值 + `%` | 缺百分号、推导税额 |

事实可带 `SKU、product、platform、market、date、citation_ids`。不扩展重量、尺寸、电压、功率和其他商品规格。

## 活动样例入口

三份纯文本 fixture 分别承载商品手册、关税合规和物流记录；每个事实只绑定自身的 SKU、平台、市场与日期上下文。活动 `questions.jsonl` 当前为 11 条：四类允许事实各一条正向题，另有 7 条资料外、超范围、多事实、换算/复杂公式与证据不足拒答。历史金融报告、冻结 eval/artifact 与既有 Gate 结论保持原文，不作为活动入口改写。

## 保留的工程合同

- SQLite 持久队列、条件 claim、lease ownership、heartbeat、有限重试和 stale recovery；
- upload/PDF/artifact SHA、engine fingerprint 和 artifact 内容摘要；
- L1 全页正文保底、L2→L3 回退、degraded audit 和物理页码；
- 版本化 Chroma staging、精确 `(doc_id,index_version)` 过滤、finalize lease fence 与 CAS 发布；
- 请求内私有 Citation Ledger，不把完整证据写入公开 source；
- 同步/SSE 共用可信决策，校验失败不泄漏候选答案且返回 `sources=[]`。

## 纯文本和表格边界

系统处理可提取纯文本与结构化表格；不引入 ColPali 或多模态。图片、图表、颜色、外观和仅靠视觉关系才能理解的内容不可回答。扫描 PDF 只有在独立 Paddle worker 生成且通过 hash/fingerprint 校验的 artifact 存在时，才可作为 L3 文本/表格证据。

## 证据边界

历史金融 eval、报告、冻结哈希和真实失败指标保持原文，仍只代表历史金融阶段。新增电商 fixture 与活动测试证明迁移后的工程合同，不证明真实 Paddle GPU 执行或生产业务质量。

## 验证

```powershell
python -m pytest tests/test_ecommerce_migration.py -q -p no:cacheprovider --basetemp .pytest-ecommerce-layer
python -m pytest tests -q -p no:cacheprovider --basetemp .pytest-ecommerce-full
```

PaddleOCR 使用 `8d403ca` 现有 Windows Python 3.12 lock；未升级版本。若本机没有匹配 GPU、模型缓存和独立 venv，只能验证 artifact adapter/worker 合同，不能声称重新执行了真实 Paddle OCR。
