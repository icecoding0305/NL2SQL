from nl2sql_agent.eval.schema_metrics import evaluate_schema_cases


def test_schema_metrics_cover_retrieval_join_execution_and_cost():
    metrics = evaluate_schema_cases([
        {
            "expected_tables": ["loan", "customer"],
            "predicted_tables": ["loan", "customer", "repayment"],
            "forbidden_tables": ["repayment"],
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
    assert metrics["column_labeled_case_count"] == 1
    assert metrics["join_labeled_case_count"] == 1
    assert metrics["forbidden_table_rate"] == 1.0
    assert metrics["join_path_accuracy"] == 1.0
    assert metrics["sql_execution_accuracy"] == 1.0
    assert metrics["human_modification_rate"] == 1.0
    assert metrics["avg_llm_cost_per_table"] == 0.03


def test_unlabeled_columns_and_joins_do_not_inflate_metrics():
    metrics = evaluate_schema_cases([
        {"expected_tables": ["loan"], "predicted_tables": ["loan"]},
        {
            "expected_tables": ["customer"],
            "predicted_tables": ["customer"],
            "expected_columns": ["customer.CUST_ID"],
            "predicted_columns": [],
        },
    ])
    assert metrics["column_labeled_case_count"] == 1
    assert metrics["column_recall"] == 0.0
    assert metrics["join_labeled_case_count"] == 0
    assert metrics["join_path_accuracy"] == 0.0


def test_schema_metrics_cover_plan_and_clarification_expectations():
    metrics = evaluate_schema_cases([
        {
            "expected_tables": ["loan"],
            "predicted_tables": ["loan"],
            "plan_labeled": True,
            "schema_plan_exact": True,
            "expected_clarification": False,
            "clarified": False,
        },
        {
            "expected_tables": ["claim"],
            "predicted_tables": ["claim"],
            "plan_labeled": True,
            "schema_plan_exact": False,
            "expected_clarification": True,
            "clarified": False,
        },
    ])
    assert metrics["schema_plan_exact_match"] == 0.5
    assert metrics["clarification_accuracy"] == 0.5
