create table if not exists db_pris_riskmanagement.DWD_EV_REPAY_PLAN (
    LOAN_NO VARCHAR(100) COMMENT '借据编号',
    PRD_CODE VARCHAR(100) comment '产品编号',
    LOAN_AMT DECIMAL(24,6) comment '放款金额',
    TRANS_DATE DATE comment '资产转让日',
    TRANS_AMT DECIMAL(24,6) comment '资产转让放款金额',
    TOTAL_TERMS INTEGER comment '总期数',
    TERM_NO INTEGER comment '当前期数',
    RPY_AMT DECIMAL(24,6) comment '应还总金额',
    RPY_PRINC DECIMAL(24,6) comment '应还本金',
    RPY_INT DECIMAL(24,6) comment '应还利息',
    RPY_OVD DECIMAL(24,6) comment '应还罚息',
    START_DATE DATE comment '本期开始日期',
    END_DATE DATE comment '本期结束日期',
    INT_START_DATE DATE comment '起息日',
    FREE_INT_DAY INTEGER comment '宽限期',
    UPDATE_DATE DATE comment '最新更新日期'
)
PARTITIONED BY (PLATFORM_CODE varchar(100) comment '平台代码')
CLUSTERED by (LOAN_NO) into 7 buckets
stored as holodesk;

/*
-- 添加单值分区
ALTER TABLE db_pris_riskmanagement.DWD_EV_REPAY_PLAN ADD IF NOT EXISTS PARTITION (PLATFORM_CODE='360'); --360分润
ALTER TABLE db_pris_riskmanagement.DWD_EV_REPAY_PLAN ADD IF NOT EXISTS PARTITION (PLATFORM_CODE='DD'); --大地
ALTER TABLE db_pris_riskmanagement.DWD_EV_REPAY_PLAN ADD IF NOT EXISTS PARTITION (PLATFORM_CODE='FBED'); --富邦E贷
ALTER TABLE db_pris_riskmanagement.DWD_EV_REPAY_PLAN ADD IF NOT EXISTS PARTITION (PLATFORM_CODE='MS'); --马上消金
ALTER TABLE db_pris_riskmanagement.DWD_EV_REPAY_PLAN ADD IF NOT EXISTS PARTITION (PLATFORM_CODE='WC'); --微车
ALTER TABLE db_pris_riskmanagement.DWD_EV_REPAY_PLAN ADD IF NOT EXISTS PARTITION (PLATFORM_CODE='WDHX'); -- 网贷核心
ALTER TABLE db_pris_riskmanagement.DWD_EV_REPAY_PLAN ADD IF NOT EXISTS PARTITION (PLATFORM_CODE='WDHXLHD'); -- 网贷核心联合贷款
ALTER TABLE db_pris_riskmanagement.DWD_EV_REPAY_PLAN ADD IF NOT EXISTS PARTITION (PLATFORM_CODE='WLD'); -- 微粒贷
ALTER TABLE db_pris_riskmanagement.DWD_EV_REPAY_PLAN ADD IF NOT EXISTS PARTITION (PLATFORM_CODE='XW'); -- 新核心按揭
ALTER TABLE db_pris_riskmanagement.DWD_EV_REPAY_PLAN ADD IF NOT EXISTS PARTITION (PLATFORM_CODE='XXD'); -- 新信贷
ALTER TABLE db_pris_riskmanagement.DWD_EV_REPAY_PLAN ADD IF NOT EXISTS PARTITION (PLATFORM_CODE='ZB'); --中保360
ALTER TABLE db_pris_riskmanagement.DWD_EV_REPAY_PLAN ADD IF NOT EXISTS PARTITION (PLATFORM_CODE='ZY'); -- 自营产品
ALTER TABLE db_pris_riskmanagement.DWD_EV_REPAY_PLAN ADD IF NOT EXISTS PARTITION (PLATFORM_CODE='WL'); -- 蔚来
ALTER TABLE db_pris_riskmanagement.DWD_EV_REPAY_PLAN ADD IF NOT EXISTS PARTITION (PLATFORM_CODE='XW2'); --新网2
*/

COMMENT ON TABLE db_pris_riskmanagement.DWD_EV_REPAY_PLAN is '事件-还款-还款计划';