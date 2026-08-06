-- 还款计划明细 DWD_EV_REPAY_DETAIL(Hive DDL 转 MySQL)
-- 注意:`INT` 是 MySQL 保留字,列名必须用反引号包裹。
-- 在 nl2sql 库手动执行:
--   mysql -h mysql5.sqlpub.com -P 3310 -u fbrisk -p nl2sql < sql/mysql_还款-还款计划明细.sql
CREATE TABLE IF NOT EXISTS `dwd_ev_repay_detail` (
  `LOAN_NO` VARCHAR(100) NOT NULL COMMENT '借据编号',
  `PRD_CODE` VARCHAR(100) COMMENT '产品编号',
  `TOTAL_TERMS` INT COMMENT '总期数',
  `TERM_NO` INT NOT NULL COMMENT '当前期数',
  `REPAY_STATUS` VARCHAR(100) COMMENT '还款状态(00:正常还款(NS),01:当期提前还款(ER),02:全部提前结清(PO),03:逾期,04:逾期还款,07:豁免(WV),08:当期退款(PAR),09:整笔退款(FUR))',
  `PRINC` DECIMAL(24,6) COMMENT '实还本金',
  `INT` DECIMAL(24,6) COMMENT '实还利息',
  `OVD` DECIMAL(24,6) COMMENT '实还罚息',
  `FEE` DECIMAL(24,6) COMMENT '实还费用',
  `COMPOUND_PAID` DECIMAL(24,6) COMMENT '实还复利',
  `REPAY_DATE` DATE COMMENT '实还日期',
  `SETTLE_DATE` DATE COMMENT '结清日期',
  `UPDATE_DATE` DATE COMMENT '最新更新日期',
  `PLATFORM_CODE` VARCHAR(100) COMMENT '平台代码',
  PRIMARY KEY (`LOAN_NO`, `TERM_NO`),
  CONSTRAINT `fk_detail_loan` FOREIGN KEY (`LOAN_NO`) REFERENCES `dwd_ar_loan_info` (`LOAN_NO`)
    ON DELETE RESTRICT ON UPDATE CASCADE,
  CONSTRAINT `fk_detail_plan` FOREIGN KEY (`LOAN_NO`, `TERM_NO`) REFERENCES `dwd_ev_repay_plan` (`LOAN_NO`, `TERM_NO`)
    ON DELETE RESTRICT ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='事件-还款-还款计划明细';
