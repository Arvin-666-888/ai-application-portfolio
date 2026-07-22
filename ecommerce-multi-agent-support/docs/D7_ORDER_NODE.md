# D7 Order Node

## 直觉

订单查询是确定性业务操作，不需要 LLM 推理。Order Node 从消息中提取规范订单号，然后使用 JWT 中的可信 `user_id` 查询订单和物流事实。

```text
用户问题
    -> Supervisor 路由到 order
    -> Order Node 提取 VLT-YYYY-NNNN
    -> get_order_status Tool
    -> OrderRepository.get_owned_order(order_no, user_id)
    -> ShipmentRepository.get_owned_order_shipment(order_no, user_id)
    -> 基于返回事实生成回答
```

## 权限边界

Tool 的业务参数只有 `order_no`。`user_id` 不是模型生成参数，也不接收前端请求值，而是由 FastAPI JWT 依赖注入到 `GraphState`，再作为后端执行上下文传给 Tool。

Repository 查询必须同时匹配：

```sql
WHERE order_no = :order_no AND user_id = :user_id
```

订单不存在和无权访问使用相同文案，避免泄露订单号是否存在。订单查询失败后不继续查询物流。

## Tool Trace

Trace 记录订单号、成功状态、结果数量和耗时，不记录 `user_id`。身份信息属于后端安全上下文，不是模型可控制的工具参数。

## 当前边界

- 仅支持规范订单号 `VLT-YYYY-NNNN`。
- 缺少订单号时要求用户补充，不调用 Tool。
- 当前只读查询订单与物流，不修改订单，也不执行退款。
- 售后问题仍由 `aftersales` 分支处理，D7 不会绕过未来的审批流程。
