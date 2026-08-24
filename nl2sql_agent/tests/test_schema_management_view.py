import json

from nl2sql_agent.api import _schema_rows_from_mschema


def test_schema_management_view_reads_selected_artifact_without_namespace_filter(tmp_path):
    path = tmp_path / "m-schema.json"
    path.write_text(json.dumps({
        "namespace": "artifact_namespace",
        "tables": {
            "customer": {
                "comment": "客户表",
                "fields": {
                    "cust_id": {
                        "type": "varchar",
                        "raw_type": "varchar(64)",
                        "comment": "客户编号",
                        "sensitive": False,
                    }
                },
            }
        },
    }, ensure_ascii=False), encoding="utf-8")

    rows = _schema_rows_from_mschema(path, {
        ("customer", "cust_id"): "统一客户编号",
    })

    assert [row["table_name"] for row in rows] == ["customer"]
    assert rows[0]["columns"] == [{
        "name": "cust_id",
        "type": "varchar(64)",
        "comment": "客户编号",
        "eff_comment": "统一客户编号",
        "overridden": True,
        "sensitive": False,
    }]


def test_schema_management_view_isolated_by_artifact_path(tmp_path):
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text(json.dumps({"tables": {"table_a": {"fields": {}}}}), encoding="utf-8")
    second.write_text(json.dumps({"tables": {"table_b": {"fields": {}}}}), encoding="utf-8")

    assert _schema_rows_from_mschema(first, {})[0]["table_name"] == "table_a"
    assert _schema_rows_from_mschema(second, {})[0]["table_name"] == "table_b"
