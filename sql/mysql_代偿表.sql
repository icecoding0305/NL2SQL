-- 代偿表 DWD_SR_CLAIM_DETAIL(Hive DDL 转 MySQL)
-- 在 nl2sql 库手动执行:
--   mysql -h mysql5.sqlpub.com -P 3310 -u fbrisk -p nl2sql < sql/mysql_代偿表.sql
CREATE TABLE IF NOT EXISTS `dwd_sr_claim_detail` (
  `DATA_DT` DATE COMMENT '数据日期',
  `DC_DT` DATE NOT NULL COMMENT '代偿日',
  `PRD_CODE` VARCHAR(100) COMMENT '产品编码',
  `PRD_NAME` VARCHAR(100) COMMENT '产品名称',
  `LOAN_NO` VARCHAR(100) NOT NULL COMMENT '借据编号',
  `DC_BAL` DECIMAL(24,6) COMMENT '代偿本金',
  `DC_INT` DECIMAL(24,6) COMMENT '代偿利息',
  `DC_FINC` DECIMAL(24,6) COMMENT '代偿罚息',
  `DC_ALL_BAL` DECIMAL(24,6) COMMENT '代偿总额',
  `DC_TERM` INT NOT NULL COMMENT '代偿期次',
  `DC_TYPE` VARCHAR(100) COMMENT '代偿类型',
  `UPDATE_DATE` DATE COMMENT '数据更新日期',
  `PLATFORM_CODE` VARCHAR(100) COMMENT '平台代码',
  PRIMARY KEY (`LOAN_NO`, `DC_TERM`, `DC_DT`),
  CONSTRAINT `fk_claim_loan` FOREIGN KEY (`LOAN_NO`) REFERENCES `dwd_ar_loan_info` (`LOAN_NO`)
    ON DELETE RESTRICT ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='代偿记录明细表';
