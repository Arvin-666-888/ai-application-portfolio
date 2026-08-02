# Migration Release Checklist

- [x] RouteName 外部契约迁移为五路客服意图。
- [x] Supervisor prompt 与确定性规则同步，地址变更优先归售后。
- [x] 订单/物流独立路由复用 OrderStatusNode/GetOrderStatusTool/Repository。
- [x] 地址变更产生 approval-only proposal，不保存地址、不执行动作。
- [x] JWT `sub + shop_id` 与数据库用户记录复核。
- [x] 商品按 shop、订单/物流按 shop + user + order_no。
- [x] User/Product/Order/Audit 最小单店字段完成。
- [x] 固定种子覆盖 US/EU/UK、USD/EUR/GBP 和三种 IANA timezone。
- [x] trace/audit 地址脱敏测试。
- [x] pytest：97 passed。
- [x] eval：47/47，route/tool 100%，security 4/4。
- [x] 隔离 SQLite smoke：商品、订单、物流、售后、越权和 approval-only 链路通过。

历史边界（迁移验收时）：当时未提交 commit，也未变更架构基建、CI、Compose、Docker 或 requirements。后续依赖职责整理已将 pytest 移入 `requirements-dev.txt`；旧 SQLite schema 仍不自动迁移，需重建本地演示库。
