from nl2sql_agent.eval.schema_metrics import evaluate_schema_cases


def test_schema_metrics_cover_retrieval_join_execution_and_cost():
    metrics = evaluate_schema_cases([
        {
            "expected_tables": ["loan", "customer"],
            "predicted_tables": ["loan", "customer", "repayment"],
            "expected_columns": ["loan.LOAN_NO", "customer.CUST_ID"],
            "predicted_columns": ["loan.LOAN_NO"],
            "expected_joins": [["loan.CUST_ID", "customer.CUST_ID"]],
            "predicted_joins": [["loan.CUST_ID", "customer.CUST_ID"]],
            "execution_correct": True,
            "clarified": False,
            "human_modified": True,
            "profile_seconds_per_table": 0.2,
            "llm_cost_per_table": 0.03,
        }
    ])
    assert metrics["table_recall_at_k"] == 1.0
    assert metrics["column_recall"] == 0.5
    assert metrics["join_path_accuracy"] == 1.0
    assert metrics["sql_execution_accuracy"] == 1.0
    assert metrics["human_modification_rate"] == 1.0
    assert metrics["avg_llm_cost_per_table"] == 0.03
