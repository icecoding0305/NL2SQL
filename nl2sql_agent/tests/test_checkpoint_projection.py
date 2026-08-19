from nl2sql_agent.services.checkpoint import checkpoint_serializer
from nl2sql_agent.state import (
    NL2SQLState,
    ProjectionDecision,
    ProjectionFieldExclusion,
    ProjectionFieldSelection,
)


def test_checkpoint_allows_projection_decision_types():
    state = NL2SQLState(
        user_query="查询客户基本信息",
        user_id="u1",
        projection_decision=ProjectionDecision(
            request="基本信息",
            target_entity="客户",
            understood_description="返回姓名和电话",
            selected_fields=[ProjectionFieldSelection(
                table_name="customer",
                column_name="NAME",
                business_label="姓名",
            )],
            excluded_fields=[ProjectionFieldExclusion(
                business_label="证件号码",
                reason="用户未明确要求",
            )],
            confidence=0.9,
        ),
    )
    serializer = checkpoint_serializer()

    restored = serializer.loads_typed(serializer.dumps_typed(state))

    assert restored.projection_decision.selected_fields[0].column_name == "NAME"
    assert restored.projection_decision.excluded_fields[0].business_label == "证件号码"
