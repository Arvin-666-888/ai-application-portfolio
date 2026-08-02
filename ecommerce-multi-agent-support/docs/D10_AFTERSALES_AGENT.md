# D10 Aftersales Agent

`aftersales_handling` 组合用户诉求、当前店铺中的本人订单/物流事实和固定政策，只输出方案，不执行动作。

```text
用户诉求
 -> issue_type / requested_action
 -> get_order_status(shop_id + user_id + order_no)
 -> evaluate_aftersales_policy
 -> proposed_action + requires_approval
```

地址变更是售后类型：`address_change / change_address`。未发货订单可提出 `address_change_review`，已发货或不可处理状态转人工复核；两者都不保存新地址、不修改订单。

敏感动作包括退款、换货、补偿、取消、退货、质保、地址变更和人工复核。`requires_approval=true` 只是风险标记，不表示存在审批记录或已执行动作。

Tool trace 只记录 issue/action、订单状态和物流异常，不记录完整地址。聊天审计只保存消息长度、路由、工具名和提案元数据。

当前边界：固定政策用于验证 Agent 与安全控制；真实政策 RAG、LangGraph interrupt/resume、幂等执行器仍未实现。
