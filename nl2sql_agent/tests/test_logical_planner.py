from nl2sql_agent.services.logical_planner import (
    build_logical_plan,
    build_query_mschema,
    build_query_mschema_bundle,
    validate_logical_plan,
)
from nl2sql_agent.services.query_store import QueryStore
from nl2sql_agent.state import (
    NL2SQLState,
    FieldCandidate,
    QueryMSchema,
    QueryPlan,
    QuerySchemaRelation,
    QuerySchemaTable,
    SchemaHit,
    SchemaPlan,
)


def _state(**kwargs):
    base = {"user_query": "查询有贷款的客户", "user_id": "u1", "data_scope": ["risk_mart"]}
    base.update(kwargs)
    return NL2SQLState(**base)


def test_query_mschema_keeps_only_planned_columns_and_relation_keys():
    state = _state(
        retrieved_schema=[
            SchemaHit(table_name="customer", columns=[
                {"name": "CUST_ID", "type": "varchar", "primary_key": True},
                {"name": "NAME", "type": "varchar"},
                {"name": "UNUSED", "type": "varchar"},
            ]),
            SchemaHit(table_name="loan", columns=[
                {"name": "CUST_ID", "type": "varchar"},
                {"name": "LOAN_AMT", "type": "decimal"},
                {"name": "UNUSED", "type": "varchar"},
            ]),
        ],
        schema_plan=SchemaPlan(
            anchor_tables=[{"table_name": "loan", "role": "primary_fact", "selected_columns": ["LOAN_AMT"]}],
            dimension_tables=[{"table_name": "customer", "role": "entity", "selected_columns": ["NAME"]}],
            relations=[{
                "source_table": "loan", "source_columns": ["CUST_ID"],
                "target_table": "customer", "target_columns": ["CUST_ID"],
                "cardinality": "many_to_one", "status": "verified",
            }],
        ),
    )

    query_schema = build_query_mschema(state)
    columns = {table.name: {column.name for column in table.columns} for table in query_schema.tables}
    assert columns == {
        "customer": {"CUST_ID", "NAME"},
        "loan": {"CUST_ID", "LOAN_AMT"},
    }


def test_query_mschema_recall_adds_strong_field_alternative_without_adding_table():
    state = _state(
        retrieved_schema=[
            SchemaHit(table_name="customer", columns=[
                {"name": "CUST_ID", "type": "varchar", "primary_key": True},
                {"name": "REG_ADDR", "type": "varchar", "comment": "户籍地址"},
                {"name": "LIVE_ADDR", "type": "varchar", "comment": "居住地址"},
            ]),
            SchemaHit(table_name="unplanned", columns=[
                {"name": "ADDR", "type": "varchar", "comment": "地址"},
            ]),
        ],
        schema_plan=SchemaPlan(anchor_tables=[{
            "table_name": "customer", "role": "entity", "selected_columns": ["REG_ADDR"],
        }]),
        field_candidates=[
            FieldCandidate(
                table_name="customer", column_name="LIVE_ADDR", query_slot="地址",
                final_score=0.82,
            ),
            FieldCandidate(
                table_name="unplanned", column_name="ADDR", query_slot="地址",
                final_score=0.95,
            ),
        ],
    )

    precision, recall = build_query_mschema_bundle(state)
    precision_columns = {column.name for column in precision.tables[0].columns}
    recall_columns = {column.name for column in recall.tables[0].columns}
    assert precision.profile == "precision"
    assert recall.profile == "recall"
    assert "LIVE_ADDR" not in precision_columns
    assert "LIVE_ADDR" in recall_columns
    assert {table.name for table in recall.tables} == {"customer"}


def test_logical_plan_has_explicit_relational_pipeline_and_grain():
    state = _state()
    plan = QueryPlan(
        target_tables=["customer"],
        filters=[{"table": "customer", "column": "STATUS", "operator": "=", "value": "active"}],
        output_fields=[{"concept": "客户编号", "table": "customer", "column": "CUST_ID"}],
        output_grain={"level": "entity", "entity": "客户", "keys": ["customer.CUST_ID"]},
        covered_atom_ids=[],
        confidence=0.9,
    )

    logical = build_logical_plan(plan, state)
    assert [operation.kind for operation in logical.operations] == ["scan", "filter", "project"]
    assert logical.root_operation_id == "project_1"
    assert logical.output_grain.keys == ["customer.CUST_ID"]


def test_entity_grain_rejects_one_to_many_plain_join():
    plan = QueryPlan(
        target_tables=["customer", "loan"],
        join_logic=[{
            "left_table": "customer", "left_column": "CUST_ID",
            "right_table": "loan", "right_column": "CUST_ID", "join_type": "inner",
        }],
        output_grain={"level": "entity", "entity": "客户", "keys": ["customer.CUST_ID"]},
    )
    logical = build_logical_plan(plan, _state())
    query_schema = QueryMSchema(
        tables=[
            QuerySchemaTable(name="customer", role="entity"),
            QuerySchemaTable(name="loan", role="primary_fact"),
        ],
        relations=[QuerySchemaRelation(
            source_table="loan", source_columns=["CUST_ID"],
            target_table="customer", target_columns=["CUST_ID"],
            cardinality="many_to_one", status="verified",
        )],
    )

    errors = validate_logical_plan(logical, query_schema)
    assert any("放大行数" in error for error in errors)


def test_query_store_persists_logical_plan_and_query_mschema(tmp_path):
    store = QueryStore(tmp_path / "queries.db")
    store.save_query(
        "trace-1",
        logical_plan={"root_operation_id": "project_1"},
        query_mschema={"tables": [{"name": "customer"}]},
        query_mschema_precision={"profile": "precision"},
        query_mschema_recall={"profile": "recall"},
        query_candidates=[{"candidate_id": "sql_1", "status": "validated"}],
    )

    saved = store.get_query("trace-1")
    assert saved["logical_plan"]["root_operation_id"] == "project_1"
    assert saved["query_mschema"]["tables"][0]["name"] == "customer"
    assert saved["query_mschema_precision"]["profile"] == "precision"
    assert saved["query_mschema_recall"]["profile"] == "recall"
    assert saved["query_candidates"][0]["candidate_id"] == "sql_1"
