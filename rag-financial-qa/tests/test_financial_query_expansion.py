from app.utils.financial_query_expansion import expand_financial_query


def test_query_expansion_uses_visible_financial_fields_without_answer_value():
    expanded = expand_financial_query(
        "格力电器2024年度合并利润表中的营业收入是多少？"
    )

    assert expanded.method == "answer-free-financial-query-expansion-v1"
    assert "公司 格力电器" in expanded.lexical_query
    assert "年度 2024" in expanded.lexical_query
    assert "指标 营业收入" in expanded.lexical_query
    assert "假设" not in expanded.lexical_query
    assert all("XXX" not in query for query in expanded.dense_queries)


def test_query_expansion_adds_metric_aliases_but_no_generated_number():
    expanded = expand_financial_query(
        "海尔智家2024年度归属于母公司股东的净利润是多少？"
    )

    assert "归属于母公司股东的净利润" in expanded.lexical_query
    assert expanded.intent.company_terms == ("海尔智家",)
    assert expanded.intent.scopes == ()
