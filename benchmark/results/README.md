# 精选压测证据

本目录只保留可复算报告所需的最小脱敏工件：

- `benchmark_stats.csv`：Locust 场景和聚合统计；
- `benchmark_failures.csv`：失败摘要；
- `metadata.json`：运行环境与证据边界。

HTML、stats history、异常明细和临时 smoke 输出仍被忽略。历史运行未机器验证全部目标虚拟用户完成初始化，因此当前报告只能标为“部分通过”；需用升级后的门禁重新运行后才能改为“通过”。
