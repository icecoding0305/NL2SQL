import json

from nl2sql_agent.services.config_loader import ConfigLoader
from nl2sql_agent.services.schema_catalog import SchemaCatalog


def test_catalog_loads_directly_from_mschema_without_yaml(tmp_path):
    config_dir = tmp_path / "config"
    schema_dir = tmp_path / "schema"
    config_dir.mkdir()
    schema_dir.mkdir()
    (config_dir / "settings.yaml").write_text(
        "schema_source:\n  mode: m_schema\n  m_schema_path: ../schema/m-schema.json\n",
        encoding="utf-8",
    )
    (schema_dir / "m-schema.json").write_text(
        json.dumps({
            "format_version": "1.0",
            "db_id": "vectortest",
            "namespace": "risk_mart",
            "tables": {
                "customer": {
                    "comment": "客户信息",
                    "fields": {
                        "CUST_ID": {
                            "type": "varchar",
                            "comment": "客户编号",
                            "primary_key": True,
                            "dim_or_meas": "dimension",
                        }
                    },
                }
            },
            "relations": [],
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    catalog = SchemaCatalog(ConfigLoader(config_dir))

    assert catalog.metadata["source"] == "effective-m-schema"
    assert catalog.metadata["datasource"] == "vectortest"
    tables = catalog.tables_for_scope(["risk_mart"])
    assert [table.name for table in tables] == ["customer"]
    assert tables[0].columns[0]["primary_key"] is True

