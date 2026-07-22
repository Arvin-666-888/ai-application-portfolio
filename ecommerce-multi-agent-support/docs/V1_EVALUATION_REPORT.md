# V1 Evaluation Report

- Generated at: `2026-07-22T01:34:44.110888+00:00`
- Mode: `local_rule_fallback`
- Cases: `30`
- Passed: `30`
- Overall pass rate: `100.0%`
- Route accuracy: `100.0%`
- Tool selection accuracy: `100.0%`
- Security cases passed: `4/4`

## Case Results

| Case | Route | Result | Details |
|---|---|---|---|
| `route_catalog_charger` | `catalog` | PASS | OK |
| `route_catalog_power_bank` | `catalog` | PASS | OK |
| `route_catalog_cable` | `catalog` | PASS | OK |
| `catalog_exact_sku` | `catalog` | PASS | OK |
| `catalog_no_match` | `catalog` | PASS | OK |
| `order_owned_1` | `order` | PASS | OK |
| `order_owned_13` | `order` | PASS | OK |
| `order_missing_number` | `order` | PASS | OK |
| `order_cross_user` | `order` | PASS | OK |
| `order_nonexistent` | `order` | PASS | OK |
| `aftersales_confirmed_damage` | `aftersales` | PASS | OK |
| `aftersales_claim_only_damage` | `aftersales` | PASS | OK |
| `aftersales_confirmed_lost_cancelled_order` | `aftersales` | PASS | OK |
| `aftersales_confirmed_delay` | `aftersales` | PASS | OK |
| `aftersales_customs_compensation` | `aftersales` | PASS | OK |
| `aftersales_cancel_paid` | `aftersales` | PASS | OK |
| `aftersales_return` | `aftersales` | PASS | OK |
| `aftersales_warranty` | `aftersales` | PASS | OK |
| `aftersales_missing_number` | `aftersales` | PASS | OK |
| `aftersales_cross_user` | `aftersales` | PASS | OK |
| `unsupported_poem` | `unsupported` | PASS | OK |
| `unsupported_programming` | `unsupported` | PASS | OK |
| `unsupported_finance` | `unsupported` | PASS | OK |
| `unsupported_sensitive_execution` | `unsupported` | PASS | OK |
| `catalog_english_charger` | `catalog` | PASS | OK |
| `catalog_wireless_charger` | `catalog` | PASS | OK |
| `order_lowercase_number` | `order` | PASS | OK |
| `aftersales_wrong_item_replace` | `aftersales` | PASS | OK |
| `aftersales_cancel_delivered` | `aftersales` | PASS | OK |
| `security_prompt_injection_order` | `order` | PASS | OK |

## Boundary

This report validates the deterministic local V1 path. It does not claim cloud-model accuracy, production traffic, or real-platform business outcomes.
