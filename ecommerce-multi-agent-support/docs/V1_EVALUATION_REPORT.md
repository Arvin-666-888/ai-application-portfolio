# V1 Evaluation Report

- Generated at: `2026-08-02T13:55:41.889160+00:00`
- Mode: `local_rule_fallback`
- Cases: `47`
- Passed: `47`
- Overall pass rate: `100.0%`
- Route accuracy: `100.0%`
- Tool selection accuracy: `100.0%`
- Security cases passed: `4/4`

## Case Results

| Case | Route | Result | Details |
|---|---|---|---|
| `route_catalog_charger` | `product_inquiry` | PASS | OK |
| `route_catalog_power_bank` | `product_inquiry` | PASS | OK |
| `route_catalog_cable` | `product_inquiry` | PASS | OK |
| `catalog_exact_sku` | `product_inquiry` | PASS | OK |
| `catalog_legacy_sku` | `product_inquiry` | PASS | OK |
| `catalog_no_match` | `product_inquiry` | PASS | OK |
| `order_owned_1` | `logistics_tracking` | PASS | OK |
| `order_owned_13` | `logistics_tracking` | PASS | OK |
| `order_missing_number` | `logistics_tracking` | PASS | OK |
| `order_cross_user` | `logistics_tracking` | PASS | OK |
| `order_nonexistent` | `logistics_tracking` | PASS | OK |
| `aftersales_confirmed_damage` | `aftersales_handling` | PASS | OK |
| `aftersales_claim_only_damage` | `aftersales_handling` | PASS | OK |
| `aftersales_confirmed_lost_cancelled_order` | `aftersales_handling` | PASS | OK |
| `aftersales_confirmed_delay` | `aftersales_handling` | PASS | OK |
| `aftersales_customs_compensation` | `aftersales_handling` | PASS | OK |
| `aftersales_cancel_paid` | `aftersales_handling` | PASS | OK |
| `aftersales_return` | `aftersales_handling` | PASS | OK |
| `aftersales_warranty` | `aftersales_handling` | PASS | OK |
| `aftersales_missing_number` | `aftersales_handling` | PASS | OK |
| `aftersales_cross_user` | `aftersales_handling` | PASS | OK |
| `unsupported_poem` | `unsupported` | PASS | OK |
| `unsupported_programming` | `unsupported` | PASS | OK |
| `unsupported_finance` | `unsupported` | PASS | OK |
| `unsupported_sensitive_execution` | `unsupported` | PASS | OK |
| `catalog_english_charger` | `product_inquiry` | PASS | OK |
| `catalog_wireless_charger` | `product_inquiry` | PASS | OK |
| `order_lowercase_number` | `logistics_tracking` | PASS | OK |
| `aftersales_wrong_item_replace` | `aftersales_handling` | PASS | OK |
| `aftersales_cancel_delivered` | `aftersales_handling` | PASS | OK |
| `security_prompt_injection_order` | `order_query` | PASS | OK |
| `address_change_review` | `aftersales_handling` | PASS | OK |
| `route_order_query_only` | `order_query` | PASS | OK |
| `currency_usd_catalog` | `product_inquiry` | PASS | OK |
| `currency_eur_catalog` | `product_inquiry` | PASS | OK |
| `currency_gbp_catalog` | `product_inquiry` | PASS | OK |
| `timezone_us_order` | `logistics_tracking` | PASS | OK |
| `timezone_eu_order` | `logistics_tracking` | PASS | OK |
| `timezone_uk_order` | `logistics_tracking` | PASS | OK |
| `aftersales_natural_cancel_proposal_only` | `aftersales_handling` | PASS | OK |
| `aftersales_natural_address_proposal_only` | `aftersales_handling` | PASS | OK |
| `catalog_cross_currency_fail_closed` | `product_inquiry` | PASS | OK |
| `catalog_thousands_budget` | `product_inquiry` | PASS | OK |
| `catalog_legacy_sku_eu_fail_closed` | `product_inquiry` | PASS | OK |
| `address_capability_not_change` | `unsupported` | PASS | OK |
| `cancel_with_order_number` | `aftersales_handling` | PASS | OK |
| `invoice_negation_not_cancel` | `order_query` | PASS | OK |

## Boundary

This report validates the deterministic local V1 path. It does not claim cloud-model accuracy, production traffic, or real-platform business outcomes.
