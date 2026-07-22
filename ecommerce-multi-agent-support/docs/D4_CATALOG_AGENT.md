# D4 Catalog Agent

## 直觉

Catalog Agent 负责把自然语言购买需求转换成结构化筛选条件；`search_products` Tool 负责查询真实商品数据并执行确定性规格过滤。

```text
用户需求
    -> Supervisor 路由到 catalog
    -> Catalog Agent 提取 category / max_price / power_w
    -> search_products Tool
    -> CatalogRepository
    -> 确定性规格过滤
    -> 仅使用返回的 SKU、价格、库存和规格生成回答
```

## 为什么规格过滤在 Tool 中

例如“300 元以内的 65W 充电器”同时包含业务类目、预算和精确规格。Repository 先查询类目、价格和库存，Tool 再检查 `specifications.power_w == 65`。这样 45W 充电器不会因为名称相似而混入结果。

Agent 不写 SQL，也不能自行补充商品。回答中出现的每个 SKU、价格、库存和规格都必须来自 Tool 返回值。

## Tool Trace

每次调用记录：

- `request_id`
- 工具名称
- 筛选参数
- 是否成功
- 结果数量
- 执行耗时

## 当前边界

- 无 API Key 时使用确定性规则提取常见中文和英文商品条件。
- 配置 API Key 后可使用 Pydantic 结构化模型输出；模型失败会退回规则提取。
- 当前回答为确定性模板，不调用第二次模型润色，优先保证事实不被改写。
- `order` 使用确定性 Order Node；`aftersales` 已接入订单/物流事实与固定政策；`unsupported` 使用固定拒答。
