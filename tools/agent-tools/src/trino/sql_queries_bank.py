NOR = """
        SELECT t.start_dt, t.end_dt, t.ccy_cd, t.value_rate as nor
        FROM dm_ddt.me_rsr_rate_ccy_fct t
        WHERE 1=1
            AND t.deal_type_cd = 'MM'
            AND t.client_type_cd = 'ALL'
            AND t.rate_type_cd='NOR'
            AND t.individual_ccy_cd = 'RUB'
            AND CAST(t.start_dt AS DATE) = (
                SELECT CAST(MAX(t2.start_dt) AS DATE)
                FROM dm_ddt.me_rsr_rate_ccy_fct t2
                WHERE 1=1
                    AND t2.deal_type_cd = 'MM'
                    AND t2.client_type_cd = 'ALL'
                    AND t2.rate_type_cd='NOR'
                    AND t2.individual_ccy_cd = 'RUB'
            )
        """


ETC = """
        SELECT t.start_dt, t.term_day_cnt, t.ftp_rate
        FROM custom_fin_palm_rates.cmn_rate_ftp_fct t
        WHERE t.ccy_cd = 'RUB'
            AND t.ftp_type_cd = 'FIX'
            AND t.main_curve_flg = 1
            AND t.term_day_cnt <= 1096
            AND CAST(t.start_dt AS DATE) = (
                SELECT CAST(MAX(t2.start_dt) AS DATE)
                FROM custom_fin_palm_rates.cmn_rate_ftp_fct t2
                WHERE 1=1
                    AND t2.ccy_cd = 'RUB'
                    AND t2.ftp_type_cd = 'FIX'
                    AND t2.main_curve_flg = 1
                    AND t2.term_day_cnt <= 1096
            )
        ORDER BY t.term_day_cnt ASC
        """


OPTION_PRICE_NSO = """
                    SELECT t.min_amt, t.max_amt, CAST(t.term_day_cnt AS INT) AS term_day_cnt,
                            SUM(t.component_val) as option_price
                    FROM custom_fin_palm_rates.cmn_option_decomposition_nso_leg_fct t
                    WHERE t.ccy_cd = 'RUB'
                        AND t.term_day_cnt <= 1096
                        AND CAST(t.start_dt AS DATE) = (
                            SELECT CAST(MAX(t2.start_dt) AS DATE)
                            FROM custom_fin_palm_rates.cmn_option_decomposition_nso_leg_fct t2
                            WHERE t2.ccy_cd = 'RUB'
                                AND t2.term_day_cnt <= 1096
                        )
                    GROUP BY t.min_amt, t.max_amt, t.term_day_cnt
                    ORDER BY t.term_day_cnt, t.min_amt, t.max_amt
                    """


OPTION_PRICE_DEPO = """
                    SELECT t.min_amt, t.max_amt, CAST(t.term_day_cnt AS INT) AS term_day_cnt,
                        t.product_type_cd, SUM(t.component_val) as option_price
                    FROM custom_fin_palm_rates.cmn_option_decomposition_depo_leg_fct t
                    WHERE t.discount_flg = 0
                        AND t.product_type_cd IN ('POP', 'OTZ')
                        AND t.ccy_cd = 'RUB'
                        AND t.term_day_cnt <= 1096
                        AND CAST(t.start_dt AS DATE) = (
                            SELECT CAST(MAX(t2.start_dt) AS DATE)
                            FROM custom_fin_palm_rates.cmn_option_decomposition_depo_leg_fct t2
                            WHERE t2.discount_flg = 0
                                AND t2.product_type_cd IN ('POP', 'OTZ')
                                AND t2.ccy_cd = 'RUB'
                                AND t2.term_day_cnt <= 1096
                        )
                    GROUP BY t.min_amt, t.max_amt, t.term_day_cnt, t.product_type_cd
                    ORDER BY t.term_day_cnt, t.min_amt, t.max_amt, t.product_type_cd
                    """


COST_LIQUIDITY_RISK = """
                        SELECT t.product_cd, t.product_type_cd, t.term_cd, t.term_day_cnt, t.crl_val
                        FROM custom_fin_palm_rates.cmn_crl_fct t
                        WHERE t.ccy_cd = 'RUB'
                            AND t.product_cd IN ('NSO', 'Depo')
                            AND t.product_subtype_cd = 'Structure'
                            AND t.term_day_cnt <= 1096
                            AND CAST(t.start_dt AS DATE) = (
                                SELECT CAST(MAX(t2.start_dt) AS DATE)
                                FROM custom_fin_palm_rates.cmn_crl_fct t2
                                WHERE t2.ccy_cd = 'RUB'
                                    AND t2.product_cd IN ('NSO', 'Depo')
                                    AND t2.product_subtype_cd = 'Structure'
                                    AND t2.term_day_cnt <= 1096
                            )
                        ORDER BY t.product_cd, t.product_type_cd, t.term_day_cnt
                        """


EVA_PAHOM = """
            SELECT t.product_cd, t.subproduct_cd, t.term_cnt, t.min_amt, t.max_amt, t.eva_rate
            FROM custom_fin_palm_dataops.lb_dep_eva_fct t
            WHERE t.ccy_cd = 'RUB'
                AND t.rate_cd = 'Pahom'
                AND t.business_block_cd = 'ALL'
                AND t.min_amt >= 500000000
                AND (
                    (t.product_cd = 'D' AND t.subproduct_cd = 'OTZ_NO') OR
                    (t.product_cd = 'NSO' AND t.subproduct_cd = 'OTZ_0_1')
                )
                AND CAST(t.start_dt AS DATE) = (
                    SELECT CAST(MAX(t2.start_dt) AS DATE)
                    FROM custom_fin_palm_dataops.lb_dep_eva_fct t2
                    WHERE t2.ccy_cd = 'RUB'
                        AND t2.rate_cd = 'Pahom'
                        AND t2.business_block_cd = 'ALL'
                        AND t2.min_amt >= 500000000
                        AND (
                            (t2.product_cd = 'D' AND t2.subproduct_cd = 'OTZ_NO') OR
                            (t2.product_cd = 'NSO' AND t2.subproduct_cd = 'OTZ_0_1')
                        )
                )
            ORDER BY t.term_cnt ASC
            """


EVA_INDIC = """
            SELECT t.business_block_cd, t.product_cd, t.subproduct_cd, t.term_cnt, t.min_amt,
                t.max_amt, t.ib_flg, t.sens_flg, t.eva_rate AS IndicEVA
            FROM custom_fin_palm_dataops.lb_dep_eva_fct t
            WHERE t.ccy_cd = 'RUB'
                AND t.rate_cd = 'Indic'
                AND (
                    (t.product_cd = 'D' AND t.subproduct_cd = 'OTZ_NO') OR
                    (t.product_cd = 'NSO' AND t.subproduct_cd = 'OTZ_0_1')
                )
                AND CAST(t.start_dt AS DATE) = (
                    SELECT CAST(MAX(t2.start_dt) AS DATE)
                    FROM custom_fin_palm_dataops.lb_dep_eva_fct t2
                    WHERE t2.ccy_cd = 'RUB'
                        AND t2.rate_cd = 'Indic'
                        AND (
                            (t2.product_cd = 'D' AND t2.subproduct_cd = 'OTZ_NO') OR
                            (t2.product_cd = 'NSO' AND t2.subproduct_cd = 'OTZ_0_1')
                        )
                )
            ORDER BY t.term_cnt ASC
            """

EVA_TARGET = """
            SELECT t.business_block_cd, t.product_cd, t.subproduct_cd, t.term_cnt, t.min_amt,
                t.max_amt, t.ib_flg, t.sens_flg, t.eva_rate AS TargetEVA
            FROM custom_fin_palm_dataops.lb_dep_eva_fct t
            WHERE t.ccy_cd = 'RUB'
                AND t.rate_cd = 'Target'
                AND (
                    (t.product_cd = 'D' AND t.subproduct_cd = 'OTZ_NO') OR
                    (t.product_cd = 'NSO' AND t.subproduct_cd = 'OTZ_0_1')
                )
                AND CAST(t.start_dt AS DATE) = (
                    SELECT CAST(MAX(t2.start_dt) AS DATE)
                    FROM custom_fin_palm_dataops.lb_dep_eva_fct t2
                    WHERE t2.ccy_cd = 'RUB'
                        AND t2.rate_cd = 'Target'
                        AND (
                            (t2.product_cd = 'D' AND t2.subproduct_cd = 'OTZ_NO') OR
                            (t2.product_cd = 'NSO' AND t2.subproduct_cd = 'OTZ_0_1')
                        )
                )
            ORDER BY t.term_cnt ASC
            """


SENS_FLG_BY_INN = """
                    SELECT DISTINCT t.sens_flg
                    FROM custom_fin_palm_dataops.lb_deal_depo_fct t
                    WHERE 1=1
                        AND t.inn_num = '{0}'
                        AND CAST(t.deal_dt AS DATE) = (
                            SELECT CAST(MAX(t2.deal_dt) AS DATE)
                            FROM custom_fin_palm_dataops.lb_deal_depo_fct t2
                            WHERE 1=1
                                AND t2.inn_num = '{0}'
                        )
                    """


KEY_RATE_SPREADS = """
    SELECT term, 
        from_half/100 as from_half, from_one/100 as from_one, from_three/100 as from_three, from_ten/100 as from_ten,
        monthly/100 as monthly, quarterly/100 as quarterly
    FROM pss.treasury_float_rates_spread
        
"""


KEY_RATE = """
            SELECT t.start_dt, t.cbr_rate_cd, t.cbr_rate
            FROM dm_csp.me_cbr_rate_palm_csp_fct t
            WHERE 1=1
                AND t.ccy_cd = 'RUB'
                AND t.cbr_rate_cd = 'CBRATE'
                AND CAST(t.start_dt AS DATE) = (
                    SELECT CAST(MAX(t2.start_dt) AS DATE)
                    FROM dm_csp.me_cbr_rate_palm_csp_fct t2
                    WHERE 1=1
                        AND t2.ccy_cd = 'RUB'
                        AND t2.cbr_rate_cd = 'CBRATE'
                )
            """


KEY_WNO_RATE = """
            SELECT t.start_dt, t.cbr_rate_cd
            FROM dm_csp.me_cbr_rate_palm_csp_fct t
            WHERE 1=1
                AND t.ccy_cd = 'RUB'
                AND t.cbr_rate_cd = 'CBRATE'
                AND CAST(t.start_dt AS DATE) = (
                    SELECT CAST(MAX(t2.start_dt) AS DATE)
                    FROM dm_csp.me_cbr_rate_palm_csp_fct t2
                    WHERE 1=1
                        AND t2.ccy_cd = 'RUB'
                        AND t2.cbr_rate_cd = 'CBRATE'
                )
            """


LIMIT_RATE = """
                        SELECT t.start_dt, t.ccy_cd, t.product_type_cd, t.product_cd,
                                t.min_amt, t.max_amt, t.term_cd, t.term_day_cnt, t.max_rate
                        FROM custom_fin_palm_rates.cmn_rate_max_depo_leg_fct t
                        WHERE t.ccy_cd = 'RUB'
                            AND t.product_type_cd IN ('NSO', 'Dep')
                            AND t.term_day_cnt <= 1096
                            AND CAST(t.start_dt AS DATE) = (
                                SELECT CAST(MAX(t2.start_dt) AS DATE)
                                FROM custom_fin_palm_rates.cmn_rate_max_depo_leg_fct t2
                                WHERE t2.ccy_cd = 'RUB'
                                    AND t2.product_type_cd IN ('NSO', 'Dep')
                                    AND t2.term_day_cnt <= 1096
                            )
                        """

LIMIT_RATE_IB = """
                        SELECT t.start_dt, t.ccy_cd, t.product_type_cd, t.product_cd,
                                t.min_amt, t.max_amt, t.term_cd, t.term_day_cnt, t.max_rate
                        FROM custom_fin_palm_rates.cmn_rate_max_depo_ib_fct t
                        WHERE t.ccy_cd = 'RUB'
                            AND t.product_type_cd IN ('NSO', 'Dep')
                            AND t.term_day_cnt <= 1096
                            AND CAST(t.start_dt AS DATE) = (
                                SELECT CAST(MAX(t2.start_dt) AS DATE)
                                FROM custom_fin_palm_rates.cmn_rate_max_depo_leg_fct t2
                                WHERE t2.ccy_cd = 'RUB'
                                    AND t2.product_type_cd IN ('NSO', 'Dep')
                                    AND t2.term_day_cnt <= 1096
                            )
                        """


AVG_CLIENTS_SPREAD = """
WITH labeled AS (
    SELECT
        d.inn_num,
        d.deal_dt,
        d.single_deal_amt,
        d.interest_rate,
        d.fund_rate,
        (d.fund_rate - d.interest_rate) AS spread,

        CASE
            WHEN date_diff('day', d.value_dt, d.maturity_dt) <= 3   THEN '1-3D'
            WHEN date_diff('day', d.value_dt, d.maturity_dt) <= 6   THEN '4-6D'
            WHEN date_diff('day', d.value_dt, d.maturity_dt) <= 7   THEN '1W'
            WHEN date_diff('day', d.value_dt, d.maturity_dt) <= 14  THEN '2W'
            WHEN date_diff('day', d.value_dt, d.maturity_dt) <= 21  THEN '3W'
            WHEN date_diff('day', d.value_dt, d.maturity_dt) <= 31  THEN '1M'
            WHEN date_diff('day', d.value_dt, d.maturity_dt) <= 61  THEN '2M'
            WHEN date_diff('day', d.value_dt, d.maturity_dt) <= 92  THEN '3M'
            WHEN date_diff('day', d.value_dt, d.maturity_dt) <= 122 THEN '4M'
            WHEN date_diff('day', d.value_dt, d.maturity_dt) <= 153 THEN '5M'
            WHEN date_diff('day', d.value_dt, d.maturity_dt) <= 183 THEN '6M'
            WHEN date_diff('day', d.value_dt, d.maturity_dt) <= 274 THEN '9M'
            WHEN date_diff('day', d.value_dt, d.maturity_dt) <= 366 THEN '1Y'
            WHEN date_diff('day', d.value_dt, d.maturity_dt) <= 549 THEN '1.5Y'
            WHEN date_diff('day', d.value_dt, d.maturity_dt) <= 731 THEN '2Y'
            ELSE '3Y'
        END AS term_bucket,

        CASE
            WHEN d.single_deal_amt / 1000000 <= 30 THEN '0-30'
            WHEN d.single_deal_amt / 1000000 <= 100 THEN '30-100'
            WHEN d.single_deal_amt / 1000000 <= 500 THEN '100-500'
            WHEN d.single_deal_amt / 1000000 <= 3000 THEN '500-3000'
            ELSE '3000-1000000'
        END AS vol_bucket,

        CASE
            WHEN d.single_deal_amt / 1000000 <= 30 THEN 1
            WHEN d.single_deal_amt / 1000000 <= 100 THEN 2
            WHEN d.single_deal_amt / 1000000 <= 500 THEN 3
            WHEN d.single_deal_amt / 1000000 <= 3000 THEN 4
            ELSE 5
        END AS vol_ord

    FROM custom_fin_palm_dataops.lb_deal_depo_fct d
    WHERE d.ccy_cd = 'RUB'
      AND d.deal_dt > DATE '2026-01-01'
      AND d.interest_rate > 0.02
      AND d.interest_rate IS NOT NULL
      AND d.fund_rate IS NOT NULL
      AND d.process_run_id = (
            SELECT MAX(t2.process_run_id)
            FROM custom_fin_palm_dataops.lb_deal_depo_fct t2
            WHERE t2.ccy_cd = 'RUB'
              AND t2.deal_dt > DATE '2025-12-31'
              AND t2.interest_rate > 0.02
              AND t2.interest_rate IS NOT NULL
              AND t2.fund_rate IS NOT NULL
        )
),

/* Ограничиваем каждый source-bucket первыми {0} сделками по свежести,
   чтобы не раздувать join */
source_ranked AS (
    SELECT
        l.*,
        ROW_NUMBER() OVER (
            PARTITION BY l.inn_num, l.term_bucket, l.vol_bucket
            ORDER BY l.deal_dt DESC
        ) AS rn_in_source_bucket
    FROM labeled l
),

source_limited AS (
    SELECT *
    FROM source_ranked
    WHERE rn_in_source_bucket <= {0}
),

/* Целевые бакеты, для которых строим метрику */
targets AS (
    SELECT DISTINCT
        inn_num,
        term_bucket,
        vol_bucket,
        vol_ord
    FROM labeled
),

/* Для каждого target-bucket подтягиваем сделки из своего и соседних volume buckets
   в рамках того же клиента и той же срочности */
candidates AS (
    SELECT
        t.inn_num,
        t.term_bucket,
        t.vol_bucket AS target_vol_bucket,
        t.vol_ord    AS target_vol_ord,

        s.deal_dt,
        s.single_deal_amt,
        s.spread,
        s.vol_bucket AS source_vol_bucket,
        s.vol_ord    AS source_vol_ord,

        ABS(s.vol_ord - t.vol_ord) AS vol_distance
    FROM targets t
    JOIN source_limited s
      ON s.inn_num = t.inn_num
     AND s.term_bucket = t.term_bucket
),

/* Ранжируем кандидатов:
   1) сначала свой bucket,
   2) потом ближайшие по vol_distance,
   3) внутри одинаковой удаленности — самые свежие */
filled AS (
    SELECT
        *,
        ROW_NUMBER() OVER (
            PARTITION BY inn_num,


term_bucket, target_vol_bucket
            ORDER BY
                vol_distance ASC,
                CASE WHEN source_vol_ord = target_vol_ord THEN 0 ELSE 1 END ASC,
                deal_dt DESC,
                source_vol_ord ASC,
                single_deal_amt DESC
        ) AS rn_fill
    FROM candidates
)

SELECT
    inn_num,
    term_bucket,
    target_vol_bucket AS vol_bucket,
    SUM(spread * single_deal_amt) / SUM(single_deal_amt) AS wmean_spread,
    COUNT(*) AS n_deals,
    AVG(single_deal_amt) AS avg_volume,

    /* диагностические поля */
    SUM(CASE WHEN source_vol_bucket = target_vol_bucket THEN 1 ELSE 0 END) AS n_from_own_bucket,
    SUM(CASE WHEN source_vol_bucket <> target_vol_bucket THEN 1 ELSE 0 END) AS n_from_nearest_buckets

FROM filled
WHERE rn_fill <= {0}
GROUP BY inn_num, term_bucket, target_vol_bucket
ORDER BY inn_num, term_bucket, target_vol_bucket
"""