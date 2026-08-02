# D7 Order Node

订单状态和物流追踪是两条外部路由：`order_query` 与 `logistics_tracking`。两者复用同一个确定性 `OrderStatusNode`、`GetOrderStatusTool` 及 Repository，不调用 LLM，也不复制 SQL。

```text
消息 -> 提取 VLT-YYYY-NNNN
     -> GetOrderStatusTool
     -> OrderRepository(shop_id, user_id, order_no)
     -> ShipmentRepository(shop_id, user_id, order_no)
     -> 订单/物流事实回答
```

可信 `user_id / shop_id / timezone` 来自经过数据库复核的 JWT 上下文。trace 只显示订单号，不显示 user/shop 安全上下文。

订单金额使用订单自己的 currency。UTC 物流时间按用户店铺 timezone 转换后回答。

当前节点只读；地址变更会在 Supervisor 中优先进入 `aftersales_handling`，不会通过普通查询节点修改订单。
