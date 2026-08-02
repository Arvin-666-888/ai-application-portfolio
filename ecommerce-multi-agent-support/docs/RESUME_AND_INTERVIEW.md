# Resume and Interview Notes

## 可验证能力

- LangGraph 五路客服契约：商品、售后、订单、物流、拒答。
- JWT `sub + shop_id` 数据库复核与 Repository 多租户过滤。
- 订单/物流独立意图复用同一确定性节点和工具，避免复制数据访问逻辑。
- US/EU/UK 店铺语境、USD/EUR/GBP 和店铺时区。
- 地址变更 approval-only proposal；完整地址不进入 trace/audit。
- pytest 61 项、33 条本地确定性 eval 全通过。

## 不可夸大

- 未接入真实 Amazon/Shopee/TikTok Shop/WooCommerce。
- 未实现真实地址写入、退款执行、审批记录或 LangGraph interrupt/resume。
- 未实现政策 RAG、来源引用或生产流量验证。
- `requires_approval=true` 不是已经执行或已经创建审批单。

## 面试重点

1. 为什么 shop_id 必须来自 JWT 并与数据库记录复核？
2. 为什么商品、订单、物流和审计都要在真实查询点应用 shop scope？
3. 为什么 order_query/logistics_tracking 分路由但复用一个节点？
4. 如何保证地址变更不落库且不泄露到 trace/audit？
5. 为什么本地规则 100% 不等于云模型或生产效果 100%？
