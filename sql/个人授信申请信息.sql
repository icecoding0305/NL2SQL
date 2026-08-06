create table IF NOT EXISTS db_pris_riskmanagement.DWD_EV_INDV_CRD_APP(
    APP_NO VARCHAR(50) comment '授信申请编号',
    CRED_LINE_TYPE VARCHAR(50) comment '授信业务类型',
    APP_NODE VARCHAR(10) comment '审批环节',
    APPLY_DATE date comment '授信申请时间',--2025-2-19添加
    PRD_CODE VARCHAR(50) comment '产品编码',
    NAME VARCHAR(50) comment '客户姓名',
    SEX VARCHAR(10) comment '性别',
    ID_TYPE VARCHAR(10) comment '证件类型',
    CERT_CODE VARCHAR(50) comment '证件号码',
    CODE_END date comment '身份证有效期止',
    CODE_START date comment '身份证有效期起',
    CERT_ADDRESS VARCHAR(400) comment '身份证地址',
    CERT_SIGNING_ORG VARCHAR(50) comment '身份证签发机关',
    BIRTHDAY date comment '出生日期',
    NATION VARCHAR(50) comment '国籍',
    ETHNIC VARCHAR(10) comment '民族',
    PHONE_NO VARCHAR(50) comment '客户手机号',
    DIPLOMA VARCHAR(11) comment '学历',
    CAREER VARCHAR(10) comment '职业',
    EMAIL VARCHAR(80) comment '邮箱',
    YEAR_INCOME VARCHAR(20) comment '收入',
    MARR_STATUS VARCHAR(10) comment '婚姻状况',
    LIVING_ADDRESS_PROV VARCHAR(50) comment '居住省',
    LIVING_ADDRESS_CITY VARCHAR(50) comment '居住市',
    LIVING_ADDRESS_DISTR VARCHAR(50) comment '居住区',
    LIVING_ADDRESS VARCHAR(400) comment '居住住址详细',
    COMPA_ADDRESS_PROV VARCHAR(50) comment '工作省',
    COMPA_ADDRESS_CITY VARCHAR(50) comment '工作市',
    COMPA_ADDRESS_DISTR VARCHAR(50) comment '工作区',
    COMPA_ADDRESS VARCHAR(400) comment '工作住址详细',
    COMPA_NAME VARCHAR(100) comment '工作单位名称',
    COMPA_PHONE VARCHAR(50) comment '单位电话',
    FIRST_CONTACT_NAME VARCHAR(50) comment '第一联系人姓名',
    FIRST_CONTACT_PHONE VARCHAR(50) comment '第一联系人手机',
    FIRST_CONTACT_RELATION VARCHAR(10) comment '第一联系人关系',
    SECOND_CONTACT_NAME VARCHAR(50) comment '第二联系人姓名',
    SECOND_CONTACT_PHONE VARCHAR(50) comment '第二联系人手机',
    SECOND_CONTACT_RELATION VARCHAR(10) comment '第二联系人关系',
    DEBIT_ACCOUNT_NAME VARCHAR(50) comment '借款人收款户名',
    DEBIT_OPEN_ACCOUNT_BANK VARCHAR(100) comment '收款人银行卡开户行',
    DEBIT_ACCOUNT_NO VARCHAR(100) comment '收款人银行卡卡号',
    DEBIT_OPEN_ACCOUNT_BANK_CODE VARCHAR(100) comment '借款人收款开户行编码',
    APP_CRED_AMT NUMERIC comment '申请授信额度',
    APPRV_CRED_AMT NUMERIC comment '审批授信额度',
    APPRV_STATE VARCHAR(10) comment '授信状态',
    LOAN_TERM VARCHAR(20) comment '贷款期限',
    LOAN_PURPOSE VARCHAR(10) comment '贷款用途',
    CUST_RATE NUMERIC comment '对客利率',
    ACCOUNT_MANAGER VARCHAR(50) comment '客户经理',
    AGENCY VARCHAR(200) comment '经办机构',
    CRED_DATE_START TIMESTAMP comment '授信起始日期',
    CRED_DATE_END TIMESTAMP comment '授信到期日期',
    LAST_UPDATE_DATE TIMESTAMP comment '最后更新日期',
    LAST_UPDATE_TIME TIMESTAMP comment '最后更新时间'
)
PARTITIONED BY (PLATFORM_CODE varchar(100) comment '平台代码')
CLUSTERED by (APP_NO) into 21 buckets
stored as orc_transaction;

/*
-- 添加单值分区
ALTER TABLE db_pris_riskmanagement.DWD_EV_INDV_CRD_APP ADD PARTITION (PLATFORM_CODE='OLPCOMMON'); --olp公共表
ALTER TABLE db_pris_riskmanagement.DWD_EV_INDV_CRD_APP ADD PARTITION (PLATFORM_CODE='XXD'); --新信贷
ALTER TABLE db_pris_riskmanagement.DWD_EV_INDV_CRD_APP ADD PARTITION (PLATFORM_CODE='XW'); --新网
ALTER TABLE db_pris_riskmanagement.DWD_EV_INDV_CRD_APP ADD PARTITION (PLATFORM_CODE='ZJ'); --字节
ALTER TABLE db_pris_riskmanagement.DWD_EV_INDV_CRD_APP ADD PARTITION (PLATFORM_CODE='ZAD'); --众安
ALTER TABLE db_pris_riskmanagement.DWD_EV_INDV_CRD_APP ADD PARTITION (PLATFORM_CODE='WL'); --蔚来汽车
*/

comment ON TABLE db_pris_riskmanagement.DWD_EV_INDV_CRD_APP is '事件-申请-个人授信申请信息';