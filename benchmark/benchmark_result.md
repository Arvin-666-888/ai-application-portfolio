# 压测结果

状态：部分通过

未生成真实性能数字。

## 原因

- 历史运行未机器验证所有目标虚拟用户均完成初始化
- 历史 success 判定未拒绝 HTTP 200 中的模型失败或 mock 回答

## 已验证

- 提交的 Locust stats CSV 记录 Agent 26、RAG 44、Aggregated 70 个请求且 Failure Count 为 0
- 提交的 failures CSV 只有表头
- P95/P99 可从提交的 stats CSV 重算

## 未验证

- 10 个目标虚拟用户全部完成初始化并持续参与测量
- 70 个 HTTP 200 均为真实模型业务成功而非错误文本或 mock 回答

## 运行元数据

- 测试日期：2026-07-24
- 运行时间：2m
- 并发用户数：10
- Spawn rate：2
- Agent Base URL：http://127.0.0.1:8001
- RAG Base URL：http://127.0.0.1:8000
- 运行模式：real LLM
- 数据集/Conversation ID：Agent 内置财务样例库；RAG conversation 2
- 硬件：Windows 11 Pro 本机
- Python：3.12.4
- 本机 Docker：是
- P95/P99 来源：Locust stats CSV 的 `95%` 与 `99%` 字段（生成器也校验兼容字段名）。
- Stats CSV SHA-256：057fa87ecd1097cdc2de574eb06274422914b175d05e5ecba9323e98e9b99b4b
- Failures CSV SHA-256：48ea7dc61427abba01680829dd9fb55b50a69604f28ae3139e85d888b289349b

## 失败摘要

- 提交的 failures CSV 只有表头。
