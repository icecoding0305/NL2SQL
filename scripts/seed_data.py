"""生成并插入 ~1000 条真实感业务数据到 6 张表。

数据为合成的演示数据(姓名/证件/手机均为生成,非真实个人)。表间逻辑关联:
客户 → 授信申请 / 借据;借据 → 还款计划 / 还款明细;逾期借据 → 代偿。
日期覆盖 2025-03 ~ 2026-08(含 8 月,便于按月份查询)。

用法:
    uv run python scripts/seed_data.py [--count 1000]
可重复运行(LOAN_NO/CUST_ID 带运行时间戳前缀,不会与已有数据冲突)。
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import random
import sys
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from nl2sql_agent.services.deps import load_env  # noqa: E402

SURNAMES = ["王", "李", "张", "刘", "陈", "杨", "赵", "黄", "周", "吴", "徐", "孙", "胡", "朱", "高",
            "林", "何", "郭", "马", "罗", "梁", "宋", "郑", "谢", "韩", "唐", "冯", "于", "董", "萧",
            "程", "曹", "袁", "邓", "许", "傅", "沈", "曾", "彭", "吕", "苏", "卢", "蒋", "蔡", "贾",
            "丁", "魏", "薛", "叶", "阎", "余", "潘", "杜", "戴", "夏", "钟", "汪", "田", "任", "姜"]
GIVEN = ["伟", "芳", "娜", "敏", "静", "磊", "军", "洋", "勇", "艳", "杰", "娟", "涛", "明", "超",
         "文轩", "子涵", "雨桐", "欣怡", "浩然", "思远", "梓萱", "嘉懿", "若曦", "晨曦", "俊杰", "慧敏", "志强", "雅静", "天宇"]
REGION = ["110101", "310101", "440101", "440301", "330101", "320101", "510101", "500101",
          "120101", "210101", "350101", "420101", "430101", "610101", "620101", "230101"]
PLATFORMS = ["XXD", "ZJ", "WL", "ZAD", "XW", "DD", "MS", "ZB", "WSD", "360", "WDHX"]
PRD_CODES = ["P01", "P02", "P03", "P04", "P05", "P06"]
PROVINCES = ["北京市", "上海市", "广东省", "浙江省", "江苏省", "四川省", "湖北省", "湖南省"]
CITIES = ["市辖区", "广州市", "深圳市", "杭州市", "南京市", "成都市", "武汉市", "长沙市"]
BANKS = ["中国工商银行", "中国建设银行", "招商银行", "浦发银行", "平安银行", "兴业银行"]
AGENCIES = ["微业贷事业部", "新信贷中心", "普惠金融部", "线上信贷部", "消费金融部"]


def gen_name(rng: random.Random) -> str:
    return rng.choice(SURNAMES) + rng.choice(GIVEN)


def gen_idnum(rng: random.Random, birthday: dt.date) -> str:
    body = rng.choice(REGION) + birthday.strftime("%Y%m%d") + f"{rng.randint(0, 999):03d}"
    weights = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
    check = "10X98765432"
    total = sum(int(body[i]) * weights[i] for i in range(17))
    return body + check[total % 11]


def gen_phone(rng: random.Random) -> str:
    return rng.choice(["13", "15", "17", "18", "19"]) + "".join(str(rng.randint(0, 9)) for _ in range(9))


def gen_date(rng: random.Random, start: str, end: str) -> dt.date:
    d0 = dt.date.fromisoformat(start)
    d1 = dt.date.fromisoformat(end)
    return d0 + dt.timedelta(days=rng.randint(0, (d1 - d0).days))


def gen_birthday(rng: random.Random) -> dt.date:
    return gen_date(rng, "1968-01-01", "2002-12-31")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=1000, help="总数据量(约)")
    args = parser.parse_args()
    load_env()

    import pymysql

    u = urlsplit(os.getenv("DATABASE_URL"))
    conn = pymysql.connect(host=u.hostname, port=u.port or 3306, user=u.username, password=u.password,
                           database=u.path.lstrip("/"), charset="utf8mb4", connect_timeout=15)
    cur = conn.cursor()
    rng = random.Random(20260803)

    stamp = dt.datetime.now().strftime("%y%m%d%H%M")
    total = args.count
    n_cust = max(80, int(total * 0.12))
    n_app = max(120, int(total * 0.15))
    n_loan = max(160, int(total * 0.20))
    n_plan = int(total * 0.32)
    n_detail = int(total * 0.22)
    n_claim = int(total * 0.06)

    # ---------- 客户 ----------
    customers = []
    for i in range(n_cust):
        bd = gen_birthday(rng)
        customers.append({
            "cust_id": f"C{stamp}{i:05d}",
            "name": gen_name(rng),
            "idnum": gen_idnum(rng, bd),
            "phone": gen_phone(rng),
            "birthday": bd,
            "age": (dt.date(2026, 8, 3) - bd).days // 365,
            "prov": rng.choice(PROVINCES),
            "city": rng.choice(CITIES),
            "unit": f"{rng.choice(['华信', '远航', '中科', '蓝海', '天诚'])}科技有限公司",
            "income": rng.randint(50, 300) * 100,
            "marriage": rng.choice(["已婚", "未婚", "离异"]),
        })
    try:
        cur.executemany(
            "INSERT INTO dwd_ip_indv_cust_info (CUST_ID, NAME, ID_TYPE, IDNUM, SEX, NATIONALITY, BIRTHDAY, AGE, "
            "HIGHEST_SCHOOLING, HOUSEADD, PHONE_NO, CUST_TYPE, CUST_STAT, MARRIAGE, PER_MON_INCOME, UNIT_NAME, "
            "PROFESSION, HHDIST, RESIADDR, RESIDIST, ACCESSION_DT, PLATFORM_CODE) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            [(c["cust_id"], c["name"], "IDCARD", c["idnum"], rng.choice(["男", "女"]), "汉", c["birthday"], c["age"],
              rng.choice(["本科", "大专", "硕士", "高中"]), c["prov"] + c["city"] + "某路123号", c["phone"],
              "个人", rng.choice(["正常", "正常", "正常", "冻结"]), c["marriage"], c["income"], c["unit"],
              rng.choice(["制造业", "信息技术", "批发零售", "服务业"]), c["prov"], c["prov"] + c["city"] + "某小区8号楼",
              c["prov"], dt.date(2018, 1, 1), rng.choice(PLATFORMS)) for c in customers]
        )
        print(f"dwd_ip_indv_cust_info +{n_cust}")
    except Exception as e:
        print("客户表失败:", e)
        for c in customers[:2]:
            print("  样例:", c["name"], len(c["name"]), "|", c["idnum"], "|", c["phone"])
        raise

    # ---------- 授信申请 ----------
    app_rows = []
    for i in range(n_app):
        c = rng.choice(customers)
        amt = rng.randint(3, 50) * 10000
        app_rows.append((
            f"A{stamp}{i:06d}", rng.choice(["循环贷", "分期贷", "消费贷"]), "审批通过",
            gen_date(rng, "2025-01-01", "2026-06-30"), rng.choice(PRD_CODES), c["name"],
            rng.choice(["男", "女"]), "IDCARD", c["idnum"], None, None, None, None, c["birthday"], "中国", "汉",
            c["phone"], rng.choice(["本科", "大专", "硕士"]), rng.choice(["工程师", "销售", "管理", "文员"]),
            f"{c['name']}{rng.randint(100, 999)}@mail.com", str(rng.randint(10, 120)), c["marriage"],
            c["prov"], c["city"], "", c["prov"] + c["city"] + "某路456号",
            c["prov"], c["city"], "", c["unit"] + "3栋", rng.choice(BANKS) + "营业部", gen_phone(rng),
            gen_name(rng), gen_phone(rng), rng.choice(["父母", "配偶", "朋友", "同事"]),
            gen_name(rng), gen_phone(rng), rng.choice(["父母", "配偶", "朋友", "同事"]),
            c["name"], rng.choice(BANKS), "".join(str(rng.randint(0, 9)) for _ in range(16)), "",
            amt, int(amt * rng.uniform(0.8, 1.0)), "有效", "12期", rng.choice(["经营", "消费", "装修"]),
            rng.uniform(0.06, 0.18), f"客户经理{rng.randint(1, 50)}", rng.choice(AGENCIES),
            None, None, None, None, rng.choice(PLATFORMS),
        ))
    print(f"[诊断] 授信 tuple={len(app_rows[0])}")
    cur.executemany(
        "INSERT INTO dwd_ev_indv_crd_app (APP_NO, CRED_LINE_TYPE, APP_NODE, APPLY_DATE, PRD_CODE, NAME, SEX, "
        "ID_TYPE, CERT_CODE, CODE_END, CODE_START, CERT_ADDRESS, CERT_SIGNING_ORG, BIRTHDAY, NATION, ETHNIC, "
        "PHONE_NO, DIPLOMA, CAREER, EMAIL, YEAR_INCOME, MARR_STATUS, LIVING_ADDRESS_PROV, LIVING_ADDRESS_CITY, "
        "LIVING_ADDRESS_DISTR, LIVING_ADDRESS, COMPA_ADDRESS_PROV, COMPA_ADDRESS_CITY, COMPA_ADDRESS_DISTR, "
        "COMPA_ADDRESS, COMPA_NAME, COMPA_PHONE, FIRST_CONTACT_NAME, FIRST_CONTACT_PHONE, FIRST_CONTACT_RELATION, "
        "SECOND_CONTACT_NAME, SECOND_CONTACT_PHONE, SECOND_CONTACT_RELATION, DEBIT_ACCOUNT_NAME, "
        "DEBIT_OPEN_ACCOUNT_BANK, DEBIT_ACCOUNT_NO, DEBIT_OPEN_ACCOUNT_BANK_CODE, APP_CRED_AMT, APPRV_CRED_AMT, "
        "APPRV_STATE, LOAN_TERM, LOAN_PURPOSE, CUST_RATE, ACCOUNT_MANAGER, AGENCY, CRED_DATE_START, "
        "CRED_DATE_END, LAST_UPDATE_DATE, LAST_UPDATE_TIME, PLATFORM_CODE) "
        "VALUES (" + ",".join(["%s"] * len(app_rows[0])) + ")",
        app_rows,
    )
    print(f"dwd_ev_indv_crd_app +{n_app}")

    # ---------- 借据(日期覆盖到 2026-08,让按月份查询有数据) ----------
    loans = []
    loan_rows = []
    for i in range(n_loan):
        c = rng.choice(customers)
        start = gen_date(rng, "2025-03-01", "2026-08-20")
        amt = rng.randint(1, 50) * 10000
        terms = rng.choice([6, 12, 12, 24, 36])
        status = rng.choices(["正常", "正常", "正常", "逾期", "结清", "提前结清"], weights=[40, 20, 15, 15, 7, 3])[0]
        rate = rng.uniform(0.05, 0.18)
        prin = amt  # 初始本金余额
        ovd = 0.0
        if status == "逾期":
            ovd = amt * rng.uniform(0.05, 0.5)
        elif status in ("结清", "提前结清"):
            prin = 0.0
        else:
            prin = amt * rng.uniform(0.5, 1.0)
        loan_no = f"LN{stamp}{i:06d}"
        loans.append({"no": loan_no, "cust": c, "amt": amt, "terms": terms, "status": status,
                      "start": start, "prin": prin, "ovd": ovd, "prd": rng.choice(PRD_CODES), "rate": rate})
        loan_rows.append((
            loan_no, f"CT{stamp}{i:06d}", c["cust_id"], rng.choice(PRD_CODES), c["name"], "IDCARD", c["idnum"],
            start, start + dt.timedelta(days=terms * 30), status, 0, None,
            start + dt.timedelta(days=terms * 30) if status in ("结清", "提前结清") else None,
            terms, 3, 0, None, rng.choice(["固定", "浮动"]), rate, rate / 365, rate * 1.5, rate,
            1.0, amt, prin, prin - ovd, ovd,
            start, start, dt.date(2026, 8, 3), dt.date(2026, 8, 3),
            "2026-08-03", "20260803", rng.choice(PLATFORMS),
        ))
    cur.executemany(
        "INSERT INTO dwd_ar_loan_info (LOAN_NO, CONT_NO, CUST_ID, PRD_CODE, NAME, IDTYPE, IDNUM, START_DATE, "
        "END_DATE, LOAN_STATUS, END_SIGN, CANCEL_REASON, PAYOFF_DATE, TOTAL_TERMS, GRACE_DAY, IS_DEBT_TR, "
        "IS_DEBT_TR_DATE, RATE_TYPE, YEAR_RATE, DAY_RATE, PNLT_RATE, YEAR_RATE_PROFIT, CAPITAL_RATIO, LOAN_AMT, "
        "PRIN_BAL, NORMAL_BAL, OVD_BAL, INPUT_DATE, INPUT_TIME, LAST_UPDATE_DATE, LAST_UPDATE_TIME, DATA_DATE, "
        "DATA_DATE_NUM, PLATFORM_CODE) "
        "VALUES (" + ",".join(["%s"] * len(loan_rows[0])) + ")",
        loan_rows,
    )
    print(f"dwd_ar_loan_info +{n_loan}")

    # ---------- 还款计划 ----------
    plan_rows = []
    plan_seen: set = set()
    for i in range(n_plan):
        ln = rng.choice(loans)
        total_terms = ln["terms"]
        term_no = rng.randint(1, total_terms)
        key = (ln["no"], term_no)
        if key in plan_seen:  # 主键 (LOAN_NO, TERM_NO),跳过重复避免 IntegrityError
            continue
        plan_seen.add(key)
        principal_each = ln["amt"] / total_terms
        plan_rows.append((
            ln["no"], ln["prd"], ln["amt"], None, None, total_terms, term_no,
            principal_each + principal_each * ln["rate"], principal_each, principal_each * ln["rate"], 0.0,
            ln["start"] + dt.timedelta(days=(term_no - 1) * 30), ln["start"] + dt.timedelta(days=term_no * 30),
            ln["start"], 3, dt.date(2026, 8, 3), rng.choice(PLATFORMS),
        ))
    cur.executemany(
        "INSERT INTO dwd_ev_repay_plan (LOAN_NO, PRD_CODE, LOAN_AMT, TRANS_DATE, TRANS_AMT, TOTAL_TERMS, TERM_NO, "
        "RPY_AMT, RPY_PRINC, RPY_INT, RPY_OVD, START_DATE, END_DATE, INT_START_DATE, FREE_INT_DAY, UPDATE_DATE, "
        "PLATFORM_CODE) VALUES (" + ",".join(["%s"] * len(plan_rows[0])) + ")",
        plan_rows,
    )
    print(f"dwd_ev_repay_plan +{n_plan}")

    # ---------- 还款明细 ----------
    detail_rows = []
    detail_seen: set = set()
    # 明细的外键 (LOAN_NO, TERM_NO) 必须引用 plan 已存在的组合,否则 FK 约束失败
    plan_pairs = list(plan_seen)
    for i in range(n_detail):
        if not plan_pairs:
            break
        loan_no, term_no = rng.choice(plan_pairs)
        key = (loan_no, term_no)
        if key in detail_seen:  # 主键 (LOAN_NO, TERM_NO),跳过重复
            continue
        detail_seen.add(key)
        ln = next((l for l in loans if l["no"] == loan_no), None)
        if ln is None:
            continue
        status = rng.choices(["00", "00", "01", "03", "04"], weights=[40, 25, 10, 15, 10])[0]
        princ = ln["amt"] / ln["terms"]
        int_amt = princ * ln["rate"]
        ovd = princ * 0.2 if status in ("03", "04") else 0.0
        detail_rows.append((
            loan_no, ln["prd"], ln["terms"], term_no, status, princ, int_amt, ovd, 0.0, 0.0,
            ln["start"] + dt.timedelta(days=term_no * 30), None, dt.date(2026, 8, 3), rng.choice(PLATFORMS),
        ))
    cur.executemany(
        "INSERT INTO dwd_ev_repay_detail (LOAN_NO, PRD_CODE, TOTAL_TERMS, TERM_NO, REPAY_STATUS, PRINC, `INT`, "
        "OVD, FEE, COMPOUND_PAID, REPAY_DATE, SETTLE_DATE, UPDATE_DATE, PLATFORM_CODE) "
        "VALUES (" + ",".join(["%s"] * len(detail_rows[0])) + ")",
        detail_rows,
    )
    print(f"dwd_ev_repay_detail +{n_detail}")

    # ---------- 代偿(逾期借据) ----------
    overdue_loans = [ln for ln in loans if ln["status"] == "逾期"] or loans
    claim_rows = []
    claim_seen: set = set()
    for i in range(n_claim):
        ln = rng.choice(overdue_loans)
        dc_term = rng.randint(1, 6)
        dc_dt = gen_date(rng, "2026-01-01", "2026-07-31")
        key = (ln["no"], dc_term, dc_dt.isoformat())  # 主键 (LOAN_NO, DC_TERM, DC_DT)
        if key in claim_seen:
            continue
        claim_seen.add(key)
        dc = ln["amt"] * rng.uniform(0.1, 0.4)
        claim_rows.append((
            dc_dt, dc_dt,
            ln["prd"], f"产品{ln['prd'][-1]}", ln["no"], dc, dc * 0.08, dc * 0.05, dc * 1.13,
            dc_term, rng.choice(["保险公司代偿", "担保公司代偿", "资产公司代偿"]),
            dt.date(2026, 8, 3), rng.choice(PLATFORMS),
        ))
    cur.executemany(
        "INSERT INTO dwd_sr_claim_detail (DATA_DT, DC_DT, PRD_CODE, PRD_NAME, LOAN_NO, DC_BAL, DC_INT, DC_FINC, "
        "DC_ALL_BAL, DC_TERM, DC_TYPE, UPDATE_DATE, PLATFORM_CODE) "
        "VALUES (" + ",".join(["%s"] * len(claim_rows[0])) + ")",
        claim_rows,
    )
    print(f"dwd_sr_claim_detail +{n_claim}")

    conn.commit()
    conn.close()
    print(f"\n共插入约 {n_cust + n_app + n_loan + n_plan + n_detail + n_claim} 条")


if __name__ == "__main__":
    main()
