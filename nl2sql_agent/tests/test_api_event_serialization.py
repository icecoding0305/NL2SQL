from datetime import date, datetime
from decimal import Decimal
import json

from fastapi.encoders import jsonable_encoder


def test_query_event_with_database_values_is_json_serializable():
    event = {
        "event": "node_complete",
        "node": "sandbox_execution",
        "data": {
            "execution_result": [{
                "贷款金额": Decimal("210000.00"),
                "逾期本金余额": Decimal("69347.302314"),
                "业务日期": date(2026, 8, 17),
                "执行时间": datetime(2026, 8, 17, 12, 30, 5),
            }],
        },
    }

    encoded = jsonable_encoder(event)
    json.dumps(encoded, ensure_ascii=False)

    row = encoded["data"]["execution_result"][0]
    assert row["贷款金额"] == 210000.0
    assert row["逾期本金余额"] == 69347.302314
    assert row["业务日期"] == "2026-08-17"

