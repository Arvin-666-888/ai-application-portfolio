# D10 Aftersales Agent

## 直觉

Aftersales Agent 将用户诉求、本人订单事实、物流事实和固定售后政策组合为结构化处理方案，但不执行退款、补偿、取消、退货或换货。

```text
用户投诉
    -> Supervisor 路由到 aftersales
    -> Aftersales Agent 分类 issue_type / requested_action
    -> get_order_status Tool
    -> order_no + JWT user_id 归属校验
    -> evaluate_aftersales_policy Tool
    -> proposed_action + required_evidence + requires_approval
    -> 事实化回复
```

## 用户陈述与系统事实

系统明确区分：

- 用户陈述：“收到的商品破损”。
- 系统事实：物流记录中的 `exception_type`。

当用户声称破损而物流记录为 `none` 时，政策结果会写明“物流记录未确认 damaged，当前仅记录用户陈述”，并要求提交商品、包装和面单照片。

## V1 固定政策

V1 使用确定性 Python 规则覆盖：破损、错发、丢件、延误、取消、退货和质保。V2 会使用真实公开政策文档和 RAG 提供来源，但退款金额、权限和审批仍由确定性规则控制。

敏感建议包括：

- `refund_review`
- `replacement_review`
- `compensation_review`
- `cancellation_review`
- `return_review`
- `warranty_review`

这些动作都只能设置 `requires_approval=true`。D10 没有 Action Executor，不会更新订单、创建退款或调用外部支付 API。

## Tool Trace

正常售后链路包含两个工具：

1. `get_order_status`
2. `evaluate_aftersales_policy`

越权或订单不存在时，只执行第一个工具并立即停止，不查询政策，也不泄露物流。

## 当前边界

- 固定政策用于验证 Agent 与风险控制边界，不代表真实平台政策。
- 缺少订单号时要求补充，不调用工具。
- 配置 API Key 后可使用 Pydantic 结构化问题分类；模型失败会退回规则分类。
- 人工审批、状态恢复和幂等执行属于 V3。
