from .queries import DEALS


test_sql = {
    "query": """SELECT product_cd FROM custom_fin_palm_dataops.lb_deal_depo_fct LIMIT 1""",
    "schema": "custom_fin_palm_dataops",
    "catalog": "con_hadoop_foton"
}


def choose_query(sel, is_deal: bool=True) -> str:

    if is_deal:

        deals_query_with_id = """
                        SELECT *
                        FROM custom_fin_palm_dataops.lb_deal_depo_fct t
                        WHERE 1=1
                        AND t.internal_order_cd = '{0}'
                        AND t.process_run_id = (
                                SELECT MAX(t2.process_run_id)
                                FROM custom_fin_palm_dataops.lb_deal_depo_fct t2
                                WHERE 1=1
                                AND t2.internal_order_cd = '{0}'
                            )
                    """

        if sel.deal_id:
            return deals_query_with_id.format(sel.deal_id)
        else:
            return DEALS

    else:
        deals_query_with_id = """
            SELECT *
            FROM custom_fin_palm_dataops.lb_cons_calculation_fct
            WHERE 1=1
            AND pass_through_calc_id = '{0}'
        """

        if sel.deal_id:
            return deals_query_with_id.format(sel.deal_id)
        else:
            return """SELECT * FROM custom_fin_palm_dataops.lb_cons_calculation_fct WHERE deal_dt >= '2026-01-12'"""
