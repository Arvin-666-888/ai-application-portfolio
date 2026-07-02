# RAG 演示与验证 Runbook

这份文档记录 RAG 项目的本地验证顺序，用于确认文档入库、检索问答、来源返回和拒答逻辑可以稳定运行。

## 1. 基础检查

```powershell
pip install -r requirements.txt
pip install -r requirements-dev.txt
pytest
python evals/run_eval.py --validate-only
```

期望结果：

- `pytest` 通过核心测试。
- eval validate-only 输出 24 条评测集结构校验 PASS。

## 2. 启动服务

```powershell
uvicorn app.main:app --reload --port 8000
```

访问：

```text
http://127.0.0.1:8000/docs
```

## 3. 最小端到端验证

```powershell
python scripts/demo_e2e.py --base-url http://127.0.0.1:8000
```

脚本会依次完成：

1. 注册和登录。
2. 创建知识库。
3. 上传样例金融文档。
4. 等待文档状态变为 `ready`。
5. 创建对话。
6. 提问资料内问题，检查 answer 和 sources。
7. 提问股价预测类问题，检查拒答逻辑。

## 4. 推荐手工问题

- `公司 2024 年营业收入是多少？`
- `2024 年毛利率是多少，为什么提升？`
- `云资源价格上升会带来什么风险？`
- `竞争对手A公司2024年的营业收入是多少？`
- `请预测公司明年股价会涨到多少？`

## 5. LangChain 对照验证

```powershell
pip install -r requirements-langchain.txt
python examples/langchain_rag_demo.py --mock --question "2024年公司营业收入是多少？"
python examples/langchain_rag_demo.py --mock --question "竞争对手A公司收入是多少？"
```

对照 demo 应返回 `answer`、`sources`、`snippet` 和 `relevance`，并能展示资料不足时的拒答路径。

## 6. 当前边界

- mock mode 只能验证流程，不代表真实语义质量。
- sources 表示检索片段来源，不等同于逐句引用证明。
- 当前 PDF 解析、评测规模、rerank、队列和监控仍是后续增强方向。
