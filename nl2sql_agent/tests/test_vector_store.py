"""向量存储适配层测试:adapter 接口、语义检索行为、后端显式切换。"""

from __future__ import annotations

from nl2sql_agent.services.deps import build_vector_store
from nl2sql_agent.services.embedding.router import fake_embedding
from nl2sql_agent.services.vector_store import InMemoryVectorStore, PgVectorStore

from .conftest import deps  # noqa: F401 - 复用 fixture


def test_adapter_upsert_and_search():
    vs = InMemoryVectorStore(catalog=None, embed=fake_embedding)
    vs.upsert("t", "id1", "贷款金额 放款金额", {"business_line": "dwd"})
    vs.upsert("t", "id2", "逾期本金 逾期余额", {"business_line": "dwd"})
    # 查询词与 id1 的 doc 重叠更多 → id1 排前
    res = vs.search("t", "放款金额", 2, {})
    assert res[0]["id"] == "id1"
    assert res[0]["score"] > 0
    # filters 生效
    res2 = vs.search("t", "放款", 2, {"business_line": "dwd"})
    assert len(res2) == 2
    res3 = vs.search("t", "放款", 2, {"business_line": "other"})
    assert res3 == []


def test_semantic_search_hits_related_table(deps):
    # fake embed(词袋):查询与表 doc 词重叠高 → 命中借据表;无关查询低置信
    vs = deps.vector_store
    hits = vs.search_scored("逾期本金", 3, ["risk_mart"])
    assert hits and hits[0][0].table_name == "dwd_ar_loan_info"
    unrelated = vs.search_scored("新能源汽车交付量", 3, ["risk_mart"])
    assert not unrelated or unrelated[0][1] < 0.7  # 语义无关 → 不误判为高置信


def test_rebuild_index(deps):
    vs = deps.vector_store
    vs.search_scored("逾期本金", 3, ["risk_mart"])  # 触发构建
    n = len(vs._store.get(vs.COLLECTION_TABLE, {}))  # noqa: SLF001
    vs.rebuild_index()
    assert len(vs._store.get(vs.COLLECTION_TABLE, {})) == n  # noqa: SLF001 重建后仍完整


def test_backend_switch_memory_by_config(deps, monkeypatch):
    # 显式配置 backend=memory → InMemoryVectorStore(不再靠环境变量隐式)
    from nl2sql_agent.services.config_loader import ConfigLoader
    from nl2sql_agent.services.deps import CONFIG_DIR

    loader = ConfigLoader(CONFIG_DIR)
    vs = build_vector_store(loader, deps.catalog, embed=fake_embedding)
    assert isinstance(vs, InMemoryVectorStore)


def test_backend_switch_pgvector_by_config(deps, monkeypatch):
    # 显式配置 backend=pgvector → PgVectorStore;缺 url 报错
    from nl2sql_agent.services.config_loader import ConfigLoader
    from nl2sql_agent.services.deps import CONFIG_DIR

    class FakeLoader:
        def load(self, name):
            return {"backend": "pgvector", "pgvector": {"url": "${PGVECTOR_URL}"}}

    with monkeypatch.context() as m:
        m.setenv("PGVECTOR_URL", "postgresql://u:p@localhost/db")
        vs = build_vector_store(FakeLoader(), deps.catalog, embed=fake_embedding)
        assert isinstance(vs, PgVectorStore)
        assert vs.url == "postgresql://u:p@localhost/db"
