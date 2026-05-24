import pandas as pd


BASE_MIN_DATE = "2026-03-01"
EVA_BASE_DATE = str(pd.to_datetime(BASE_MIN_DATE) - pd.Timedelta(days=15)).split(' ')[0]


OPT_DEPO = """
    SELECT
    start_dt, ccy_cd, min_amt, max_amt, CAST(term_day_cnt AS INT) as term_day_cnt, product_type_cd, SUM((component_val / 100)) as option_price
    FROM custom_fin_palm_rates.cmn_option_decomposition_depo_leg_fct
    WHERE discount_flg = 0
    AND start_dt = DATE '{0}'
    GROUP BY start_dt, ccy_cd, min_amt, max_amt, term_day_cnt, product_type_cd
    ORDER BY start_dt, ccy_cd, term_day_cnt, min_amt, max_amt, product_type_cd
"""


OPT_NSO = """
    SELECT
    start_dt, ccy_cd, min_amt, max_amt, term_day_cnt, SUM((component_val / 100)) as option_price
    FROM custom_fin_palm_rates.cmn_option_decomposition_nso_leg_fct
    WHERE start_dt = DATE '{0}'
    GROUP BY start_dt, ccy_cd, min_amt, max_amt, term_day_cnt
    ORDER BY start_dt, ccy_cd, term_day_cnt, min_amt, max_amt
"""


CRL = """
    SELECT
    CAST(start_dt as DATE) as start_dt, product_cd, product_subtype_cd, product_type_cd, term_cd, term_day_cnt, ccy_cd, (crl_val / 100) AS crl_val
    FROM custom_fin_palm_rates.cmn_crl_fct
    WHERE start_dt = DATE '{0}'
    ORDER BY start_dt
"""


NOR = """
    SELECT
    start_dt, end_dt, ccy_cd, (value_rate / 100) as nor
    FROM dm_ddt.me_rsr_rate_ccy_fct
    WHERE 1=1
    AND deal_type_cd = 'MM'
    AND client_type_cd = 'ALL'
    AND rate_type_cd='NOR'
    AND start_dt >= DATE '2023-06-01'
"""


ETS = """
    SELECT
    start_dt, ccy_cd, term_day_cnt, (ftp_rate / 100) as etc_rate
    FROM custom_fin_palm_rates.cmn_rate_ftp_fct
    WHERE ftp_type_cd = 'FIX'
    AND start_dt = DATE '{0}'
    AND main_curve_flg = 1
    AND term_day_cnt <= 1096
"""


TARGET_EVA = """
    SELECT
    start_dt, end_dt, ccy_cd, product_cd, subproduct_cd, min_amt,
    max_amt, term_cnt, ib_flg, sens_flg, business_block_cd,
    (eva_rate / 100) as eva_rate
    FROM custom_fin_palm_dataops.lb_dep_eva_fct
    WHERE 1 = 1
    AND start_dt = DATE '{0}'
    AND rate_cd = 'Target'
"""


INDIC_EVA = """
    SELECT
    start_dt, end_dt, ccy_cd, product_cd, subproduct_cd, min_amt,
    max_amt, term_cnt, ib_flg, sens_flg, business_block_cd,
    (eva_rate / 100) as eva_rate
    FROM custom_fin_palm_dataops.lb_dep_eva_fct
    WHERE 1 = 1
    AND start_dt = DATE '{0}'
    AND rate_cd = 'Indic'
"""


DEALS = """
    SELECT *
    FROM custom_fin_palm_dataops.lb_deal_depo_fct t
    WHERE 1=1
    AND t.ccy_cd = 'RUB'
    AND t.deal_dt >= DATE '{0}'
    AND t.process_run_id = (
            SELECT MAX(t2.process_run_id)
            FROM custom_fin_palm_dataops.lb_deal_depo_fct t2
            WHERE 1=1
            AND t2.ccy_cd = 'RUB'
            AND t2.deal_dt >= DATE '{0}'
        )
""".format(BASE_MIN_DATE)

DEALS_REPORT = """
    SELECT *
    FROM custom_fin_palm_dataops.lb_deal_depo_fct t
    WHERE 1=1
    AND t.ccy_cd = 'RUB'
    AND t.deal_dt >= DATE '{0}'
    AND t.deal_dt <= DATE '{1}'
    AND t.process_run_id = (
            SELECT MAX(t2.process_run_id)
            FROM custom_fin_palm_dataops.lb_deal_depo_fct t2
            WHERE 1=1
            AND t2.ccy_cd = 'RUB'
            AND t2.deal_dt >= DATE '{0}'
            AND t2.deal_dt <= DATE '{1}'
        )
"""
