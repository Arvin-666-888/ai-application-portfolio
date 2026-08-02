# 架构说明

## 数据流

```text
HTTP 请求
  -> JWT 解码 sub + shop_id
  -> 数据库匹配 users.id + users.shop_id
  -> GraphState 注入 user_id / shop_id / market / timezone
  -> Supervisor 五路外部契约
     -> product_inquiry -> Catalog Agent -> shop 商品查询
     -> order_query -> OrderStatusNode ─┐
     -> logistics_tracking -> 同一节点 ├-> GetOrderStatusTool
                                      └-> shop + user + order_no Repository
     -> aftersales_handling -> 订单事实 -> 固定政策 -> 待审批方案
     -> unsupported -> 固定拒答
  -> 回答 + 脱敏 trace
  -> shop + user 范围 Audit
```

## 数据模型

系统保持六张表，新增最小单店上下文字段：

- `users`: `shop_id / market / timezone`
- `products`: `shop_id / currency`
- `orders`: `shop_id / currency`
- `audit_logs`: `shop_id`
- `order_items` 和 `shipments` 通过订单关系继承业务范围

金额使用 `Decimal` 与显式 ISO 4217 code，不做 FX 换算。时间仍以 UTC 存储，订单回答按用户店铺 IANA timezone 展示预计送达时间。

## 安全边界

```sql
-- 商品
WHERE products.shop_id = :shop_id

-- 订单/物流
WHERE orders.shop_id = :shop_id
  AND orders.user_id = :user_id
  AND orders.order_no = :order_no
```

`shop_id` 和 `user_id` 都来自通过数据库复核的 JWT 上下文，不接受聊天文本控制。不存在、跨用户和跨店订单使用相同失败结果。

## 路由与复用

外部 RouteName：

- `product_inquiry`
- `aftersales_handling`
- `order_query`
- `logistics_tracking`
- `unsupported`

`order_query` 与 `logistics_tracking` 是独立路由节点，但都调用同一 `OrderStatusNode` 与 `GetOrderStatusTool`，没有复制 Repository 查询逻辑。

地址变更关键词优先于泛化订单关键词，并路由到 `aftersales_handling`。政策输出：

- `issue_type=address_change`
- `proposed_action=address_change_review`
- `requires_approval=true`

这只是提案。系统没有地址字段、地址写入、审批恢复或动作执行器；完整地址不写入 trace/audit。

## 可复现种子

固定随机种子覆盖三个店铺语境：US/USD、EU/EUR、UK/GBP，以及 Los Angeles、Berlin、London 时区。种子只用于本地仿真。

## 当前边界

- `Base.metadata.create_all()` 不升级已有 SQLite schema，旧演示库需重建。
- 审批标记不等于完整 Human-in-the-loop 工作流。
- 固定政策不代表真实平台政策；云模型效果未由本地规则评测证明。
