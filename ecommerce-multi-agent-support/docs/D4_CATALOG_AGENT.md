# D4 Catalog Agent

`product_inquiry` 将购买需求转换为结构化筛选条件，`search_products` Tool 查询当前 JWT 店铺中的真实商品并执行规格过滤。

```text
自然语言 -> category / max_price / power_w
        -> search_products(shop_id)
        -> CatalogRepository
        -> SKU / price / currency / stock / specifications
```

Repository 首先应用 `products.shop_id = trusted_shop_id`。价格过滤只在当前店铺商品的原始币种中进行，不做 FX 换算；回答显式展示 USD/EUR/GBP code，不使用固定货币符号。

Agent 不写 SQL、不自行补商品。trace 记录业务筛选条件，但不暴露 JWT 中的 shop/user 上下文。
