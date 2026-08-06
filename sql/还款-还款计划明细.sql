create table if not exists db_pris_riskmanagement.DWD_EV_REPAY_DETAIL (
    LOAN_NO VARCHAR(100) comment '借据编号',
    PRD_CODE VARCHAR(100) comment '产品编号',
    TOTAL_TERMS INTEGER comment '总期数',
    TERM_NO INTEGER comment '当前期数',
    REPAY_STATUS VARCHAR(100) comment '还款状态(00:正常还款(NS),01:当期提前还款(ER),02:全部提前结清(PO),03:逾期,04:逾期还款,07:豁免(WV),08:当期退款(PAR),09:整笔退款(FUR))',
    PRINC DECIMAL(24,6) comment '实还本金',
    INT DECIMAL(24,6) comment '实还利息',
    OVD DECIMAL(24,6) comment '实还罚息',
    FEE DECIMAL(24,6) comment '实还费用',
    COMPOUND_PAID DECIMAL(24,6) comment '实还复利',
    REPAY_DATE DATE comment '实还日期',
    SETTLE_DATE DATE comment '结清日期',
    UPDATE_DATE DATE comment '最新更新日期'
)
PARTITIONED BY (PLATFORM_CODE VARCHAR(100) comment '平台代码')
CLUSTERED by (LOAN_NO) into 7 buckets
stored as holodesk;

/*
-- 添加单值分区
ALTER TABLE db_pris_riskmanagement.DWD_EV_REPAY_DETAIL ADD PARTITION (PLATFORM_CODE='XW'); --新网
ALTER TABLE db_pris_riskmanagement.DWD_EV_REPAY_DETAIL ADD PARTITION (PLATFORM_CODE='ZB'); --中保360
ALTER TABLE db_pris_riskmanagement.DWD_EV_REPAY_DETAIL ADD PARTITION (PLATFORM_CODE='360'); --360分润
ALTER TABLE db_pris_riskmanagement.DWD_EV_REPAY_DETAIL ADD PARTITION (PLATFORM_CODE='FBED'); --富邦E贷
ALTER TABLE db_pris_riskmanagement.DWD_EV_REPAY_DETAIL ADD PARTITION (PLATFORM_CODE='MS'); --马上消金
ALTER TABLE db_pris_riskmanagement.DWD_EV_REPAY_DETAIL ADD PARTITION (PLATFORM_CODE='DD'); --大地
ALTER TABLE db_pris_riskmanagement.DWD_EV_REPAY_DETAIL ADD PARTITION (PLATFORM_CODE='WDHX'); --网贷核心
ALTER TABLE db_pris_riskmanagement.DWD_EV_REPAY_DETAIL ADD PARTITION (PLATFORM_CODE='WDHXLHD'); --网贷核心联合贷
ALTER TABLE db_pris_riskmanagement.DWD_EV_REPAY_DETAIL ADD PARTITION (PLATFORM_CODE='WC'); --微车
ALTER TABLE db_pris_riskmanagement.DWD_EV_REPAY_DETAIL ADD PARTITION (PLATFORM_CODE='XHX'); --新核心按揭
ALTER TABLE db_pris_riskmanagement.DWD_EV_REPAY_DETAIL ADD PARTITION (PLATFORM_CODE='WLD'); --微粒贷
ALTER TABLE db_pris_riskmanagement.DWD_EV_REPAY_DETAIL ADD PARTITION (PLATFORM_CODE='ZY'); -- 自营产品
ALTER TABLE db_pris_riskmanagement.DWD_EV_REPAY_DETAIL ADD PARTITION (PLATFORM_CODE='WL'); -- 蔚来
ALTER TABLE db_pris_riskmanagement.DWD_EV_REPAY_DETAIL ADD PARTITION (PLATFORM_CODE='WS'); --网商

ALTER TABLE db_pris_riskmanagement.DWD_EV_REPAY_DETAIL ADD PARTITION (PLATFORM_CODE='XW2'); --新网2
*/

COMMENT ON TABLE db_pris_riskmanagement.DWD_EV_REPAY_DETAIL is '事件-还款-还款计划明细';