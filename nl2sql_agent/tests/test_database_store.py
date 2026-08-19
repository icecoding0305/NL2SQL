from nl2sql_agent.services.database_store import DatabaseConfigStore


def test_database_registry_masks_password_and_switches_default(tmp_path, monkeypatch):
    schema = tmp_path / "data" / "schema" / "primary"
    schema.mkdir(parents=True)
    (schema / "m-schema.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("DATABASE_URL", "mysql://first:secret@db.example:3307/primary")
    store = DatabaseConfigStore(tmp_path / "registry.db", tmp_path)

    seeded = store.get()
    assert seeded is not None
    assert seeded["database_name"] == "primary"
    assert seeded["schema_status"] == "ready"
    assert seeded["password_configured"] is True
    assert "password" not in seeded

    second = store.create({
        "name": "分析库",
        "engine": "mysql",
        "host": "db2.example",
        "port": 3306,
        "database_name": "analytics",
        "username": "reader",
        "password": "p@ss word",
        "namespace": "analytics",
    })
    assert store.set_default(second["id"])
    assert store.get()["id"] == second["id"]
    assert "reader:p%40ss%20word@db2.example" in store.connection_url(second["id"])

    store.update(second["id"], {"name": "新名称", "password": ""})
    assert store.get(second["id"])["name"] == "新名称"
    assert "p%40ss%20word" in store.connection_url(second["id"])
