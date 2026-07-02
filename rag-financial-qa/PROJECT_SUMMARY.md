# RAG 项目技术概要

## 项目目标

这是一个面向财报、公告和风险说明等金融文档的 RAG 问答后端原型。项目重点验证文档入库、向量检索、上下文增强、来源返回、多轮问答、流式响应和资料外拒答等 AI 应用后端链路。

## 能力覆盖

1. **RAG 链路**：文档解析、文本切分、Embedding、ChromaDB 入库、检索、Prompt 组装和回答生成。
2. **后端工程**：FastAPI 分层架构、JWT 鉴权、SQLite 元数据、文档状态和失败原因记录。
3. **可信回答**：返回来源文档、snippet、relevance，并对资料外问题和金融高风险问题拒答。
4. **验证体系**：pytest、JSONL eval、检索命中率、来源支撑率、拒答准确率和关键词命中率。
5. **可复现运行**：`.env.example`、Dockerfile、docker compose、mock mode 和端到端演示脚本。

## 技术路线

```text
上传文件
  -> 解析 TXT/MD/PDF
  -> RecursiveTextSplitter 分块
  -> Embedding API / mock embedding
  -> ChromaDB collection
  -> 用户提问
  -> 向量候选召回
  -> 关键词重叠重排
  -> Prompt 注入上下文
  -> LLM 生成回答
  -> 返回 answer + sources
```

## 设计取舍

- **来源返回**：金融文档问答不能只给结论，返回 snippet 可以帮助用户核验依据。
- **资料外拒答**：RAG 的目标是基于资料回答，不是让模型回答所有问题。
- **轻量重排**：向量检索对年份、数字和短问题可能不稳定，因此加入关键词重叠分数辅助排序。
- **mock mode**：无 API Key 时用于验证接口和数据流；真实语义质量仍需真实 embedding 和 chat 模型评估。
- **本地存储**：SQLite 和本地 ChromaDB 便于复现，生产环境可替换为 PostgreSQL、对象存储和独立向量数据库。

## 验证方式

- `pytest` 覆盖文本切分、检索、来源构造和评测数据集结构。
- `python evals/run_eval.py --validate-only` 校验 24 条 JSONL 评测集结构。
- `python scripts/demo_e2e.py --base-url http://127.0.0.1:8000` 验证注册、登录、知识库、上传、问答和拒答链路。
- `python examples/langchain_rag_demo.py --mock` 验证 LangChain 对照检索流程。

## 当前边界

- PDF 解析以文本提取为主，复杂表格、跨页表格和图表理解仍需增强。
- sources 只能说明答案关联的文档片段，不能证明每一句回答都有逐句证据。
- 评测集是小规模功能验收集，后续可扩展为更系统的真实模型评测。
- 后台任务、审计日志、Prompt Injection 防护、监控告警和权限模型仍需按生产要求补充。
