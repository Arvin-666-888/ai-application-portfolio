# Resume and Interview Notes

## 简历项目名称

**基于 LangGraph 的跨境电商多 Agent 智能客服系统｜独立开发**

## 当前可写能力

- Supervisor 四路路由。
- Catalog Agent + 商品 Tool。
- 确定性 Order Node + JWT 归属校验。
- Aftersales Agent + 固定政策 + 待审批方案。
- Tool Trace、脱敏审计、pytest、30 条离线评测、Docker。

## 当前不能写

- 已接入真实 Amazon/Shopee/TikTok Shop。
- 已接入 WooCommerce。
- 已实现政策 RAG、来源引用或 Checkpointer。
- 已执行真实退款或完成 Human-in-the-loop 恢复。
- 已在线上生产环境运行。

## 60 秒讲解结构

```text
业务问题
-> 为什么使用 LangGraph
-> 为什么 Catalog/Aftersales 是 Agent，而 Order 是普通节点
-> Tool/Repository 如何约束事实和权限
-> 敏感动作为什么只生成待审批方案
-> pytest、评测和已知边界
```

## 高频追问

1. 为什么不是一个 Agent 加三个 Tool？
2. 为什么 Order Node 不使用 LLM？
3. 用户在 Prompt 里伪造 user_id 怎么办？
4. 用户声称破损与系统确认破损如何区分？
5. `requires_approval=true` 是否等于已经退款？
6. 规则评测 100% 能否证明真实模型也有 100%？
7. 如何从 SQLite 替换为 WooCommerce？
8. V2 为什么使用 LlamaIndex + ChromaDB？
