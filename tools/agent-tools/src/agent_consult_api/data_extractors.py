import logging

import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from typing import Dict, Iterable, Optional, Sequence, Union

from ..trino.query_utils import choose_query
from ..trino.trino_client import run_trino, TrinoClientError
from ..trino.queries import OPT_DEPO, OPT_NSO, CRL, NOR, ETS, TARGET_EVA, INDIC_EVA, DEALS_REPORT

from .tech_f import normalize_inn, get_optionality_type, get_term_label, is_none_or_nan

from ..utils.logger_config import configure_logging


configure_logging()
log = logging.getLogger("---DataExtractor---")


LOAD_KWARGS = {
    'nor': {
        'catalog':'con_pg_dm',
        'schema':'dm_ddt'
    },
    'palm': {
        'catalog':'con_hadoop_foton',
        'schema':'custom_fin_palm_rates'
    },
    'depo': {
        'catalog':'con_hadoop_foton',
        'schema':'custom_fin_palm_dataops'
    },
}


def get_near_monday(dt: str) -> str:
    d = datetime.strptime(dt, "%Y-%m-%d").date()
    return (d - timedelta(days=d.weekday())).isoformat()


class DealExtractor:

    def __init__(
            self,
            deal_params,
    ):
        self.p = deal_params

        log.info('DealExtractor init!')

    def download_data(self, data_type: dict=None, query: str=None) -> pd.DataFrame:
        params = LOAD_KWARGS[data_type]
        try:
            df = run_trino(query, catalog=params['catalog'], schema=params['schema'])
            if df is None or len(df) < 1:
                log.warning(f"data {data_type} is empty")
                return None
            log.info(f"data {data_type} has been loaded successfully! shape: {df.shape}")
            return df
        except Exception as e:
            log.error(f"Error loading data {data_type}: {e}")
            return None


    def prepare_data(self, df: pd.DataFrame) -> pd.DataFrame:
        """обработка данных (приведение полей к определенному типа, создание новых полей и пр)"""

        log.info("--- Start prepare deals data ---")

        df['value_dt'] = pd.to_datetime(df['value_dt'])
        df['deal_dt'] = pd.to_datetime(df['deal_dt'])
        df['maturity_dt'] = pd.to_datetime(df['maturity_dt'])

        df.loc[df['product_cd']=='NSO', 'value_dt'] = df['value_dt'] - pd.Timedelta(days=1) # корректировка даты валютирования для НСО

        df['term'] = (df['maturity_dt'] - df['value_dt']).apply(lambda x: x.days)

        df.loc[df['product_cd']=='NSO', 'value_dt'] = df['value_dt'] + pd.Timedelta(days=1) # обратная корректировка

        df['tem_bucket_eva'] = df['term'].apply(lambda x: get_term_label(x, 'eva'))
        df['tem_bucket_crl'] = df['term'].apply(lambda x: get_term_label(x, 'cost_liq_risk'))

        df['ccy_cd'] = df['ccy_cd'].astype(str)
        df['product_cd'] = df['product_cd'].astype(str)

        df['single_deal_amt'] = df['single_deal_amt'].astype(float)
        df['original_delta_limit_amt'] = df['original_delta_limit_amt'].astype(float)
        df['delta_limit_amt'] = df['delta_limit_amt'].astype(float)

        df['interest_rate'] = df['interest_rate'].astype(float) / 100
        df['option_price_rate'] = df['option_price_rate'].fillna(0).astype(float) / 100
        df['fund_rate'] = df['fund_rate'].astype(float) / 100
        df['max_rate'] = df['max_rate'].astype(float) / 100
        df['eva_rate'] = df['eva_rate'].astype(float) / 100
        df['eva_target_rate'] = df['eva_target_rate'].astype(float) / 100

        df['top_up_option_flg'] = df['top_up_option_flg'].fillna(0).astype(int)
        df['withdrawal_option_full_flg'] = df['withdrawal_option_full_flg'].fillna(0).astype(int)
        df['withdrawal_option_part_flg'] = df['withdrawal_option_part_flg'].fillna(0).astype(int)
        df['sens_flg'] = df['sens_flg'].fillna(0).astype(int)
        df['msb_flg'] = df['msb_flg'].fillna(0).astype(int)

        df['inn_num'] = df['inn_num'].apply(normalize_inn)
        df['ib_flg'] = df['inn_num'].apply(lambda x: 1 if len(x) > 11 else 0)

        df.loc[(df['ib_flg']==1) & (df['msb_flg']!=1), 'msb_flg'] = 1

        df['withdrawal_option'] = df['withdrawal_option_full_flg'] + df['withdrawal_option_part_flg']

        df = get_optionality_type(df)

        df = df.drop('deal_amt', axis=1)\
            .rename(
            columns={
                'maturity_dt':'end_dt',
                'single_deal_amt':'deal_amt',
                'msb_flg':'msp_flg',
                'eva_target_rate':'target_eva_rate'
            }
        )

        df['optionality'] = df['optionality_type']
        df.loc[df['optionality_type']=='OTZ_POP', 'optionality'] = 'OTZ'

        log.info("--- End prepare deals data ---")
        log.info(f"data shape is {df.shape}, data columns are {df.columns}")

        return df

    def filter_deal(self, df: pd.DataFrame) -> dict:
        try:

            log.info(f"deal_id is {self.p.deal_id}, dtype is {type(self.p.deal_id)}")
            
            if 'internal_order_cd' not in df.columns:
                log.info('no internal_order_cd at dataset columns')
                return None

            if self.p.deal_id:
                log.info(f"filter by deal_id {self.p.deal_id}")
                cand = df[df['internal_order_cd'] == str(self.p.deal_id)].copy()
                if cand.empty:
                    log.info('no data after filter by deal_id')
                    return None
                cand = cand.sort_values(['deal_dt']).tail(1)

                cand['otz_type'] = None
                cand.loc[cand['product_cd']=='NSO', 'otz_type'] = 'OTZ_0_1'
                cand.loc[cand['product_cd']=='DEPO', 'otz_type'] = 'OTZ_NO'
    
                cand.drop(['inn_num'], axis=1, inplace=True)
    
                log.info(f"candidate data shape is {cand.shape}")
                log.info(f"filtered deals data {cand}")
                return cand.to_dict('records')[0]
                
            else:
                log.info('no deal_id in internal params')
                return None

        except Exception as e:
            log.error('no deals', e)
            return None


    def extract_deal(self):
        """единая точка входа и выхода для получения записи нужной сделки"""
        query = choose_query(self.p)
        # log.info(f"query to deals DB: {query}")
        deal_df = self.download_data(data_type='depo', query=query)
        if deal_df is None or len(deal_df) < 1:
            log.info('no deals found')
            return None
        deal_df = self.prepare_data(deal_df)
        extracted_deal = self.filter_deal(deal_df)
        log.info("--- End of extracting deals ---")
        return extracted_deal


class CalcExtractor:

    def __init__(
            self,
            deal_params,
    ):

        self.p = deal_params


    def download_data(self, data_type: dict=None, query: str=None) -> pd.DataFrame:
        params = LOAD_KWARGS[data_type]
        try:
            df = run_trino(query, catalog=params['catalog'], schema=params['schema'])
            if df is None or len(df) < 1:
                log.warning(f"data {data_type} is empty")
                return None
            log.info(f"data {data_type} has been loaded successfully! shape: {df.shape}")
            return df
        except Exception as e:
            log.error(f"Error loading data {data_type}: {e}")
            return None


    def prepare_data(self, df: pd.DataFrame) -> pd.DataFrame:

        log.info("--- Start prepare calcs data ---")

        df['calculation_dttm'] = pd.to_datetime(df['calculation_dttm'])
        df['deal_dt'] = pd.to_datetime(df['deal_dt'])
        df['value_dt'] = pd.to_datetime(df['value_dt'])
        df['end_dt'] = pd.to_datetime(df['end_dt'])

        df['pass_through_calc_id'] = df['pass_through_calc_id'].fillna(0)
        df['pass_through_calc_id'] = df['pass_through_calc_id'].astype(str)

        df.loc[df['product_cd']=='NSO', 'value_dt'] = df['value_dt'] - pd.Timedelta(days=1) # корректировка даты валютирования для НСО

        df['term'] = (df['end_dt'] - df['value_dt']).apply(lambda x: x.days)

        df.loc[df['product_cd']=='NSO', 'value_dt'] = df['value_dt'] + pd.Timedelta(days=1) # обратная корректировка

        df['tem_bucket_eva'] = df['term'].apply(lambda x: get_term_label(x, 'eva'))
        df['tem_bucket_crl'] = df['term'].apply(lambda x: get_term_label(x, 'cost_liq_risk'))

        df['deal_term_cnt'] = df['deal_term_cnt'].astype(int)
        df['has_top_up_option_flg'] = df['has_top_up_option_flg'].fillna(0).astype(int)
        df['has_withdrawal_option_flg'] = df['has_withdrawal_option_flg'].fillna(0).astype(int)
        df['sens_flg'] = df['sens_flg'].fillna(0).astype(int)
        df['msp_flg'] = df['msp_flg'].fillna(0).astype(int)
        df['resident_flg'] = df['resident_flg'].fillna(0).astype(int)

        df['inn_num'] = df['inn_num'].apply(normalize_inn)
        df['ib_flg'] = df['inn_num'].apply(lambda x: 1 if len(x) > 11 else 0)

        df['deal_amt'] = df['deal_amt'].astype(float)
        df['delta_limit_amt'] = df['delta_limit_amt'].astype(float)
        df['interest_rate'] = df['interest_rate'].astype(float)
        df['limit_rate'] = df['limit_rate'].astype(float)
        df['public_rate'] = df['public_rate'].astype(float)
        df['fund_rate'] = df['fund_rate'].astype(float)
        df['ets_rate'] = df['ets_rate'].astype(float)
        df['option_price_rate'] = df['option_price_rate'].fillna(0).astype(float)
        df['max_rate'] = df['max_rate'].astype(float)
        df['eva_rate'] = df['eva_rate'].astype(float)
        df['indicative_eva_rate'] = df['indicative_eva_rate'].astype(float)
        df['target_eva_rate'] = df['target_eva_rate'].astype(float)

        df = get_optionality_type(
            df,
            withdrawal_col_name='has_withdrawal_option_flg',
            top_up_col_name='has_top_up_option_flg'
        )

        df['optionality'] = df['optionality_type']
        df.loc[df['optionality_type']=='OTZ_POP', 'optionality'] = 'OTZ'

        log.info("--- End prepare calcs data ---")
        log.info(f"data shape is {df.shape}, data columns are {df.columns}")

        return df


    def filter_calc(self, df: pd.DataFrame) -> dict:

        try:
            q = pd.Series(True, index=df.index)

            assert 'pass_through_calc_id' in df.columns, 'pass_through_calc_id в колонках нету'
            if self.p.deal_id:
                q &= (df['pass_through_calc_id'] == str(self.p.deal_id))

            cand = df[q].copy()
            if cand.empty:
                return None

            if cand['status_cd'].nunique() > 1 and ('DealDone' in list(cand['status_cd'].unique()) or 'Closed' in list(cand['status_cd'].unique())):
                if 'DealDone' in list(cand['status_cd'].unique()):
                    cand = cand[cand['status_cd']=='DealDone']
                else:
                    cand = cand[cand['status_cd']=='Closed']
            if len(cand) > 1:
                row = cand.sort_values(by='calculation_dttz').tail(1)

                row['otz_type'] = None #"OTZ_NO" if self.p.product == 'DEPO' else 'OTZ_0_1'
                row.loc[cand['product_cd']=='NSO', 'otz_type'] = 'OTZ_0_1'
                row.loc[cand['product_cd']=='DEPO', 'otz_type'] = 'OTZ_NO'

                # row['otz_type'] = "OTZ_NO" if self.p.product == 'DEPO'else 'OTZ_0_1'

                row = row.to_dict(orient='records') # пока не придумал другого
            else:
                cand['otz_type'] = None #"OTZ_NO" if self.p.product == 'DEPO' else 'OTZ_0_1'
                cand.loc[cand['product_cd']=='NSO', 'otz_type'] = 'OTZ_0_1'
                cand.loc[cand['product_cd']=='DEPO', 'otz_type'] = 'OTZ_NO'

                row = cand.to_dict(orient='records')

            return row[0]

        except Exception as e:
            log.info('no deals', e)
            return None


    def extract_calc(self):
        query = choose_query(self.p, is_deal=False)
        calc_df = self.download_data(data_type='depo', query=query)
        if calc_df is None or len(calc_df) < 1:
            log.info('no deals found')
            return None
        calc_df = self.prepare_data(calc_df)
        extracted_calc = self.filter_calc(calc_df)
        return extracted_calc


class IndicatorsExtractor:

    def __init__(self, params):

        self.p = params

        self.dt = str(pd.to_datetime(params['deal_dt']) - pd.Timedelta(days=7)).split(' ')[0]
        self.monday_dt = get_near_monday(self.dt)

        self.crl_product_subtype_cd = 'Structure'
        self.crl_product = "Depo" if self.p['product_cd'] == "DEPO" else "NSO"
        self.crl_option_type = (self.p['optionality_type'] if self.p['product_cd'] == "DEPO" else "")

        self.eva_product = "D" if self.p['product_cd'] == "DEPO" else "NSO"
        self.eva_business_block_cd = "ALL" if self.p['ib_flg'] == 1 else self.p['business_block_cd']


    def download_data(self, data_type: dict=None, query: str=None) -> pd.DataFrame:
        params = LOAD_KWARGS[data_type]
        try:
            df = run_trino(query, catalog=params['catalog'], schema=params['schema'])
            if df is None or len(df) < 1:
                log.warning(f"Data {data_type} is empty (0 rows returned).")
                return None
            log.info(f"Data {data_type} loaded successfully! Shape: {df.shape}")
            return df

        except TrinoClientError as e:
            # Специфическая ошибка Trino (например, нет таблицы или партиции)
            # Логируем как WARNING или ERROR, но не крашим приложение
            if "HIVE_FILE_NOT_FOUND" in str(e):
                log.error(f"CRITICAL DATA ISSUE for {data_type}: Table/Partition missing in Hadoop. Details: {e}")
            else:
                log.error(f"Trino query error for {data_type}: {e}")
            return None # Возвращаем None, чтобы сработала логика дефолтных значений (0.0)

        except Exception as e:
            # Непредвиденная ошибка (баг в коде, память и т.д.)
            log.exception(f"Unexpected error loading data {data_type}: {e}")
            return None


    def crl(self) -> float:
        log.info("--- Start process CRL data ---")
        df = self.download_data(data_type='palm', query=CRL.format(self.dt))
        if df is None or len(df) < 1:
            log.info("--- No CRL data found. Return default value 0.0 ---")
            return 0.0

        log.info(f"--- Shape of downloaded CRL data is {df.shape} ---")

        df['start_dt'] = pd.to_datetime(df['start_dt'])
        last_dt = df['start_dt'].max()

        if last_dt < pd.to_datetime(self.p['deal_dt']):

            df_filtered_one = df[
                (df['start_dt']==last_dt) &
                (df['ccy_cd']==self.p['ccy_cd']) &
                (df['product_subtype_cd']==self.crl_product_subtype_cd) &
                (df['product_cd']==self.crl_product) &
                (df['product_type_cd']==self.crl_option_type) &
                (df['term_cd']==self.p['tem_bucket_crl'])
                ]

            crl = np.round(float(df_filtered_one['crl_val'].unique()[0]), 6)
            log.info(f"--- Filtered CRL data is {crl} ---")
            if not crl:
                log.info("--- No CRL filtered data found. Return default value 0.0 ---")
                return 0.0
            return crl

        else:
            df_filtered = df[
                (df['start_dt'] >= pd.to_datetime('2023-06-01')) &
                (df['ccy_cd']==self.p['ccy_cd']) &
                (df['product_subtype_cd']==self.crl_product_subtype_cd) &
                (df['product_cd']==self.crl_product) &
                (df['product_type_cd']==self.crl_option_type) &
                (df['term_cd']==self.p['tem_bucket_crl'])
                ]

            df_filtered = df_filtered.copy()
            df_filtered['end_dt'] = df_filtered['start_dt'].shift(-1) - pd.Timedelta(days=1)
            df_filtered.loc[ df_filtered['end_dt'].isna(), 'end_dt'] = pd.Timestamp.today().normalize()
            df_filtered_one = df_filtered[
                (df_filtered['start_dt']<=pd.to_datetime(self.p['deal_dt'])) &
                (df_filtered['end_dt']>pd.to_datetime(self.p['deal_dt']))
                ]
            if len(df_filtered_one) < 1:
                df_filtered_one = df_filtered[df_filtered['start_dt'] == df_filtered['start_dt'].max()]

            crl = np.round(float(df_filtered_one['crl_val'].unique()[0]), 6)
            log.info(f"--- Filtered CRL data is {crl} ---")

            if not crl:
                log.info("--- No CRL filtered data found. Return default value 0.0 ---")
                return 0.0

            log.info("--- End of processing CRL data ---")
            return crl


    def nor(self) -> float:
        log.info("--- Start process NOR data ---")
        df_nor = self.download_data(data_type='nor', query=NOR)
        if df_nor is None or len(df_nor) < 1:
            log.info("--- NOR not found, use default value 4.5% ---")
            return 0.045

        log.info(f"--- Shape of downloaded NOR data is {df_nor.shape} ---")

        df_nor = df_nor[df_nor['ccy_cd']==self.p['ccy_cd']]

        if len(df_nor) < 2:
            log.info(f"--- Filtered NOR data is {df_nor} ---")
            return np.round(float(df_nor['nor'].unique()[0]), 6)

        else:
            df_nor['start_dt'] = pd.to_datetime(df_nor['start_dt'])
            df_nor['end_dt'] = pd.to_datetime(df_nor['end_dt'])

            df_nor = df_nor[
                (df_nor['start_dt']<=pd.to_datetime(self.p['deal_dt'])) &
                (df_nor['end_dt']>pd.to_datetime(self.p['deal_dt']))
                ]

            log.info(f"--- Filtered NOR data is {df_nor} ---")

            nor = np.round(float(df_nor['nor'].unique()[0]), 6)

        if not nor:
            log.info("--- NOR not found, use default value 4.5% ---")
            return 0.045

        log.info("--- End of processing NOR data ---")
        return nor


    def ets(self) -> Dict[float, float]:

        log.info("--- Start process ETS data ---")

        ets = self.download_data(data_type='palm', query=ETS.format(self.dt))

        if ets is None or len(ets) < 1:
            log.info("--- ETS not found, use default value 0.0 ---")
            return None

        log.info(f"--- Shape of downloaded ETS data is {ets.shape} ---")

        ets['start_dt'] = pd.to_datetime(ets['start_dt'])

        def add_dates(df: pd.DataFrame) -> pd.DataFrame:

            log.info("--- Start add dates to fullfill ETS data ---")

            date_etc_list = []
            try:
                for ccy_term, etc_ccy_term in df.groupby(['ccy_cd', 'term_day_cnt']):
                    etc_ccy_term = etc_ccy_term.sort_values(by='start_dt')
                    date = pd.DataFrame({'date': pd.date_range(etc_ccy_term['start_dt'].min(), etc_ccy_term['start_dt'].max())})
                    date_etc = date.merge(etc_ccy_term, left_on = 'date', right_on = 'start_dt', how = 'left')
                    date_etc = date_etc.ffill()
                    date_etc_list.append(date_etc)
                date_etc_full = pd.concat(date_etc_list)

                date_etc_full['start_dt'] = pd.to_datetime(date_etc_full['start_dt'])
                date_etc_full['term_day_cnt'] = date_etc_full['term_day_cnt'].astype(int)
                date_etc_full['etc_rate'] = date_etc_full['etc_rate'].astype(float)

                return date_etc_full[['date', 'ccy_cd', 'term_day_cnt', 'etc_rate']].sort_values(by = 'date')
            except Exception as e:
                log.error(f"--- Error due adding dates to ETS data: {e} ---")
                return df


        def interpolate_etc(df_etc: pd.DataFrame) -> pd.DataFrame:
            """
            Линейная интерполяция значений ЕТС на дполнительные сроки
            INPUT: pd.DataFrame с ЕТС на базовые срочности на каждый день за указанный период
            RETURN: pd.DataFrame с ЕТС, интерполированным на каждый срок в интервале: [1, 1096],
                    на каждый день за указанный период
            """
            interpolated_etc_list = []
            try:
                for date, df_date in df_etc.groupby('date'):
                    df_date = df_date.sort_values('term_day_cnt')
                    base_terms = list(df_date['term_day_cnt'].unique())
                    etc_values = df_date['etc_rate']
                    all_terms = [i for i in range(1, max(base_terms) + 1)]
                    interpolated_etc = [np.interp(i, base_terms, etc_values) for i in range(1, max(base_terms) + 1)]

                    df_date_new = pd.DataFrame()
                    df_date_new['term_day_cnt'] = all_terms
                    df_date_new['etc_rate'] = interpolated_etc
                    df_date_new['date'] = date
                    df_date_new['ccy_cd'] = df_date['ccy_cd'].unique()[0]
                    interpolated_etc_list.append(df_date_new)

                return pd.concat(interpolated_etc_list).sort_values(by = 'date')

            except Exception as e:
                log.error(f"--- Error due interpolating ETS data: {e} ---")
                return df_etc


        ets = add_dates(ets)

        try:
            log.info("--- Continue processing ETS ---")
            ets_ccy_list = []
            for ccy, ets_ccy in ets.groupby('ccy_cd'):
                ets_ccy_inter = interpolate_etc(ets_ccy)
                ets_ccy_list.append(ets_ccy_inter)

            log.info("--- Interpolation ETS data has been successfully done ---")

            ets_inter = pd.concat(ets_ccy_list).sort_values(by=['date', 'term_day_cnt', 'ccy_cd']).reset_index(drop=True)

            interp_etc_rates_u_month = ets_inter[ets_inter['term_day_cnt'] < 31].copy()
            interp_etc_rates_u_month.loc[:, 'ets_zc'] = interp_etc_rates_u_month['etc_rate']


            interp_etc_rates_ov_month = ets_inter[ets_inter['term_day_cnt'] >= 31].copy()
            interp_etc_rates_ov_month.loc[:, 'ets_zc'] = (
                                                                 (
                                                                         (1 + interp_etc_rates_ov_month['etc_rate']/12) ** (12 * interp_etc_rates_ov_month['term_day_cnt']/365)
                                                                 ) - 1
                                                         ) * 365 / interp_etc_rates_ov_month['term_day_cnt']


            interp_etc_rates = pd.concat([interp_etc_rates_u_month, interp_etc_rates_ov_month])

            interp_etc_rates['date'] = pd.to_datetime(interp_etc_rates['date'], format="%Y-%m-%d")
            interp_etc_rates = interp_etc_rates.sort_values(by = 'date').reset_index(drop = True)

            interp_etc_rates['term_day_cnt'] = interp_etc_rates['term_day_cnt'].astype(int)

            interp_etc_rates_filtered_rub = interp_etc_rates[
                (interp_etc_rates['date']==pd.to_datetime(self.p['deal_dt'])) &
                (interp_etc_rates['ccy_cd']=='RUB') &
                (interp_etc_rates['term_day_cnt']==int(self.p['term']))
                ]

            if len(interp_etc_rates_filtered_rub) < 1:
                log.info("--- NO ETS filtered data found. Return default value 0.0 ---")
                ets_zc_rub = 0.0

            else:
                ets_zc_rub = np.round(float(interp_etc_rates_filtered_rub['ets_zc'].unique()[0]), 6)

            if 'ets_rate' in self.p.keys() and not is_none_or_nan(self.p['ets_rate']):
                ets_zc = np.round(float(self.p['ets_rate']), 6)

            else:

                interp_etc_rates['term_day_cnt'] = interp_etc_rates['term_day_cnt'].astype(int)

                interp_etc_rates_filtered = interp_etc_rates[
                    (interp_etc_rates['date']==pd.to_datetime(self.p['deal_dt'])) &
                    (interp_etc_rates['ccy_cd']==self.p['ccy_cd']) &
                    (interp_etc_rates['term_day_cnt']==int(self.p['term']))
                    ]

                if len(interp_etc_rates_filtered) < 1:
                    log.info("--- NO ETS filtered data found. Return default value 0.0 ---")
                    ets_zc = 0.0

                else:
                    log.info(f"--- Filtered ETS data is {interp_etc_rates_filtered} ---")
                    ets_zc = np.round(float(interp_etc_rates_filtered['ets_zc'].unique()[0]), 6)


            ets_dict = {
                'ets_zc': ets_zc,
                'ets_zc_rub': ets_zc_rub
            }

            log.info("--- End of processing ETS data ---")

            return ets_dict

        except Exception as e:
            log.exception(f"--- Error due processing ETS: {e} ---")
            return {
                'ets_zc': 0.0,
                'ets_zc_rub': 0.0
            }


    def option_price(self) -> float:

        log.info("--- Start process OPTION PRICE data ---")

        if self.p['optionality'] == '':
            log.info("--- End of processing OPRION PRICE data because of no optionality. Return default value 0.0 ---")
            return 0.0

        if 'option_price_rate' in self.p.keys() and not is_none_or_nan(self.p['option_price_rate']):
            log.info(f"--- Return OPTION PRICE value from initial deal. Value if {float(self.p['option_price_rate'])} ---")
            return np.round(float(self.p['option_price_rate']), 6)


        def add_dates(df: pd.DataFrame, group_list) -> pd.DataFrame:
            log.info("--- Start add dates to fullfill OPTION PRICE data ---")
            date_option_price_list = []
            for ccy_term, df_gr in df.groupby(group_list):
                df_gr = df_gr.sort_values(by='start_dt')
                date = pd.DataFrame({'date': pd.date_range(df_gr['start_dt'].min(), df_gr['start_dt'].max())})
                date_df = date.merge(df_gr, left_on = 'date', right_on = 'start_dt', how = 'left')
                date_df = date_df.ffill()
                date_option_price_list.append(date_df)
                
            date_option_price_full = pd.concat(date_option_price_list)

            date_option_price_full = date_option_price_full.drop('start_dt', axis=1)
            date_option_price_full['start_dt'] = pd.to_datetime(date_option_price_full['date'])
            date_option_price_full = date_option_price_full.drop('date', axis=1)

            date_option_price_full['min_amt'] = date_option_price_full['min_amt'].astype(float)
            date_option_price_full['max_amt'] = date_option_price_full['max_amt'].astype(float)
            date_option_price_full['term_day_cnt'] = date_option_price_full['term_day_cnt'].astype(int)
            date_option_price_full['option_price'] = date_option_price_full['option_price'].astype(float)

            log.info("--- End of adding dates to OPTION PRICE data ---")

            return date_option_price_full.sort_values(by='start_dt')

        if self.p['product_cd'] == 'DEPO':
            df = self.download_data(data_type='palm', query=OPT_DEPO.format(self.dt))
            if df is None or len(df) < 1:
                log.info("--- OPTION PRICE DEPO not found, use default value 0.0 ---")
                return 0.0
            log.info(f"--- Shape of downloaded OPTION PRICE DEPO data is {df.shape}. COLUMNS: {df.columns}---")

            if "_col4" in df.columns:
                log.info(f"--- OPTION PRICE DEPO has column _col4. DATA in this column is {df['_col4'].unique()} ---")
                df = df.rename(columns={'_col4': 'term_day_cnt'})
                log.info(f"--- OPTION PRICE DEPO has been renamed column _col4 to term_day_cnt. DATA in this column is {df['term_day_cnt'].unique()} ---")
            
            try:
                df['start_dt'] = pd.to_datetime(df['start_dt'])
                df = add_dates(df, ['ccy_cd', 'min_amt', 'max_amt', 'term_day_cnt', 'product_type_cd'])
                df['min_amt'] = df['min_amt'].astype(float)
                df['max_amt'] = df['max_amt'].astype(float)
                df['term_day_cnt'] = df['term_day_cnt'].astype(int)
                    
                df_filtered_one = df[
                    (df['start_dt'] == pd.to_datetime(self.p['deal_dt'])) &
                    (df['min_amt'] <= float(self.p['deal_amt'])) &
                    (df['max_amt'] > float(self.p['deal_amt'])) &
                    (df['ccy_cd'] == self.p['ccy_cd']) &
                    (df['term_day_cnt']==int(self.p['term'])) &
                    (df['product_type_cd']==self.p['optionality'])
                    ]

                if len(df_filtered_one) > 0:
                    log.info(f"--- filtered data OPTION PRICE DEPO shape is {df_filtered_one.shape} ---")

                    opt_price = str(df_filtered_one['option_price'].unique()[0])
                    log.info("--- End of processing OPTION PRICE DEPO ---")
                    return np.round(float(opt_price), 6)
                else:
                    log.info(f"--- filtered data OPTION PRICE DEPO shape is zero. Return default value 0.0 ---")
                    return 0.0

            except Exception as e:
                log.error(f"--- OPTION PRICE DEPO not found: {e} . Return default value 0.0---")
                return 0.0

        else:
            df = self.download_data(data_type='palm', query=OPT_NSO.format(self.dt))

            if df is None or len(df) < 1:
                log.info("--- OPTIONAL PRICE NSO not found, use default value 0.0 ---")
                return 0.0

            log.info(f"--- Shape of downloaded OPTION PRICE NSO data is {df.shape} ---")

            if "_col4" in df.columns:
                log.info(f"--- OPTION PRICE NSO has column _col4. DATA in this column is {df['_col4'].unique()} ---")
                df = df.rename(columns={'_col4': 'term_day_cnt'})
                log.info(f"--- OPTION PRICE NSO has been renamed column _col4 to term_day_cnt. DATA in this column is {df['term_day_cnt'].unique()} ---")

            try:
                df['start_dt'] = pd.to_datetime(df['start_dt'])
                df = add_dates(df, ['ccy_cd', 'min_amt', 'max_amt', 'term_day_cnt'])
                df['min_amt'] = df['min_amt'].astype(float)
                df['max_amt'] = df['max_amt'].astype(float)
                df['term_day_cnt'] = df['term_day_cnt'].astype(int)
                
                df_filtered_one = df[
                    (df['start_dt'] == pd.to_datetime(self.p['deal_dt'])) &
                    (df['min_amt'] <= float(self.p['deal_amt'])) &
                    (df['max_amt'] > float(self.p['deal_amt'])) &
                    (df['ccy_cd'] == self.p['ccy_cd']) &
                    (df['term_day_cnt']==int(self.p['term']))
                    ]

                log.info(f"--- filtered data OPTIONAL PRICE NSO shape is {df_filtered_one.shape} ---")

                opt_price = str(df_filtered_one['option_price'].unique()[0])
                log.info("--- End of processing OPTION PRICE NSO ---")
                return np.round(float(opt_price), 6)
            except Exception as e:
                log.error(f"--- OPTION PRICE NSO not found: {e} . Return default value 0.0 ---")
                return 0.0


    def indic_eva(self) -> float:

        log.info("--- Start process INDIC EVA data ---")

        if 'indicative_eva_rate' in self.p.keys() and not is_none_or_nan(self.p['indicative_eva_rate']):
            log.info(f"--- End of processing INDIC EVA data, return {float(self.p['indicative_eva_rate'])} from initial deals data ---")
            return np.round(float(self.p['indicative_eva_rate']), 6)

        else:

            indic_eva = self.download_data(data_type='depo', query=INDIC_EVA.format(self.dt))

            if indic_eva is None or len(indic_eva) < 1:
                log.info('--- INDIC EVA not found, use 0.0 ---')
                return 0.0

            log.info(f"--- Shape of downloaded INDIC EVA data is {indic_eva.shape} ---")

            try:
                log.info("--- Start prepare INDIC EVA data ---")

                indic_eva = indic_eva.drop('end_dt', axis=1)
                indic_eva['start_dt'] = pd.to_datetime(indic_eva['start_dt'])
                indic_eva['min_amt'] = indic_eva['min_amt'].astype(float)
                indic_eva['max_amt'] = indic_eva['max_amt'].astype(float)
                indic_eva['eva_rate'] = indic_eva['eva_rate'].astype(float)
                indic_eva['term_cnt'] = indic_eva['term_cnt'].astype(int)
                indic_eva['sens_flg'] = indic_eva['sens_flg'].astype(int)
                indic_eva['ib_flg'] = indic_eva['ib_flg'].astype(int)


                date_indic_eva_list = []
                col2gr = ['ccy_cd', 'product_cd', 'term_cnt', 'business_block_cd', 'min_amt', 'max_amt', 'sens_flg', 'ib_flg']
                for key, df_col2gr in indic_eva.groupby(col2gr):
                    df_col2gr = df_col2gr.sort_values(by='start_dt')
                    date = pd.DataFrame({'date': pd.date_range(df_col2gr['start_dt'].min(), df_col2gr['start_dt'].max())})
                    date_indic_eva = date.merge(df_col2gr, left_on = 'date', right_on = 'start_dt', how = 'left')
                    date_indic_eva = date_indic_eva.ffill()
                    date_indic_eva_list.append(date_indic_eva)

                date_indic_eva_full = pd.concat(date_indic_eva_list)

                date_indic_eva_full['start_dt'] = pd.to_datetime(date_indic_eva_full['start_dt'])
                date_indic_eva_full['min_amt'] = date_indic_eva_full['min_amt'].astype(float)
                date_indic_eva_full['max_amt'] = date_indic_eva_full['max_amt'].astype(float)
                date_indic_eva_full['eva_rate'] = date_indic_eva_full['eva_rate'].astype(float)
                date_indic_eva_full['term_cnt'] = date_indic_eva_full['term_cnt'].astype(int)
                date_indic_eva_full['sens_flg'] = date_indic_eva_full['sens_flg'].astype(int)
                date_indic_eva_full['ib_flg'] = date_indic_eva_full['ib_flg'].astype(int)
                date_indic_eva_full['tem_cd'] = date_indic_eva_full['term_cnt'].apply(lambda x: get_term_label(x, 'eva'))

                log.info("--- Start filtering INDIC EVA ---")

                filter_indic_eva = date_indic_eva_full[
                    (date_indic_eva_full['date'] == pd.to_datetime(self.p['deal_dt'])) &
                    (date_indic_eva_full['ccy_cd'] == self.p['ccy_cd']) &
                    (date_indic_eva_full['product_cd'] == self.eva_product) &
                    (date_indic_eva_full['business_block_cd'] == self.eva_business_block_cd) &
                    (date_indic_eva_full['min_amt'] <= float(self.p['deal_amt'])) &
                    (date_indic_eva_full['max_amt'] > float(self.p['deal_amt'])) &
                    (date_indic_eva_full['sens_flg'] == int(self.p['sens_flg'])) &
                    (date_indic_eva_full['ib_flg'] == int(self.p['ib_flg'])) &
                    (date_indic_eva_full['tem_cd'] == self.p['tem_bucket_eva'])
                    ]

                log.info(f"--- Shape of filtered INDIC EVA data is {filter_indic_eva.shape} ---")
                log.info("--- END of processing INDIC EVA ---")
                return np.round(float(filter_indic_eva['eva_rate'].unique()[0]), 6)

            except Exception as e:
                log.error(f"--- Error due procesing INDIC EVA data: {e} . Return default value 0.0 ---")
                return 0.0


    def target_eva(self) -> float:

        log.info("--- Start process TARGET EVA data ---")
        if 'target_eva_rate' in self.p.keys() and not is_none_or_nan(self.p['target_eva_rate']):
            log.info(f"--- End of processing TARGET EVA data, return {float(self.p['target_eva_rate'])} from initial deals data ---")
            return np.round(float(self.p['target_eva_rate']), 6)
        else:
            t_eva = self.download_data(data_type='depo', query=TARGET_EVA.format(self.monday_dt))

            if t_eva is None or len(t_eva) < 1:
                log.info('--- TARGET EVA not found, use 0.0 ---')
                return 0.0

            log.info(f"--- Shape of downloaded TARGET EVA data is {t_eva.shape} ---")

            try:
                t_eva['start_dt'] = pd.to_datetime(t_eva['start_dt'])

                t_eva.loc[t_eva['start_dt'] == t_eva['start_dt'].max(), 'end_dt'] = pd.Timestamp.today().normalize()
                t_eva['end_dt'] = pd.to_datetime(t_eva['end_dt'])

                t_eva['min_amt'] = t_eva['min_amt'].astype(float)
                t_eva['max_amt'] = t_eva['max_amt'].astype(float)
                t_eva['term_cnt'] = t_eva['term_cnt'].astype(int)
                t_eva['ib_flg'] = t_eva['ib_flg'].astype(int)
                t_eva['sens_flg'] = t_eva['sens_flg'].astype(int)
                t_eva['eva_rate'] = t_eva['eva_rate'].astype(float)

                t_eva['tem_cd'] = t_eva['term_cnt'].apply(lambda x: get_term_label(x, 'eva'))

                if self.eva_product == "D":
                    log.info("--- Start of filtering TARGET EVA DEPO ---")
                    filter_target_eva = t_eva[
                        (t_eva['start_dt'] <= pd.to_datetime(self.p['deal_dt'])) &
                        (t_eva['end_dt'] >= pd.to_datetime(self.p['deal_dt'])) &
                        (t_eva['ccy_cd'] == self.p['ccy_cd']) &
                        (t_eva['product_cd'] == self.eva_product) &
                        (t_eva['subproduct_cd'] == self.p['otz_type']) &
                        (t_eva['business_block_cd'] == self.eva_business_block_cd) &
                        (t_eva['min_amt'] <= float(self.p['deal_amt'])) &
                        (t_eva['max_amt'] > float(self.p['deal_amt'])) &
                        (t_eva['sens_flg'] == int(self.p['sens_flg'])) &
                        (t_eva['ib_flg'] == int(self.p['ib_flg'])) &
                        (t_eva['tem_cd'] == self.p['tem_bucket_eva'])
                        ]

                    if len(filter_target_eva) < 1:
                        log.info('--- No TARGET EVA DEPO found. Return default value 0.0 ---')
                        return 0.0

                    log.info(f"--- Shape of filtered TARGET EVA DEPO data is {filter_target_eva.shape} ---")
                    log.info("--- END of processing TARGET EVA DEPO ---")
                    return np.round(float(filter_target_eva['eva_rate'].unique()[0]), 6)

                else:
                    log.info("--- Start of filtering TARGET EVA NSO ---")
                    filter_target_eva = t_eva[
                        (t_eva['start_dt'] <= pd.to_datetime(self.p['deal_dt'])) &
                        (t_eva['end_dt'] >= pd.to_datetime(self.p['deal_dt'])) &
                        (t_eva['ccy_cd'] == self.p['ccy_cd']) &
                        (t_eva['product_cd'] == self.eva_product) &
                        (t_eva['business_block_cd'] == self.eva_business_block_cd) &
                        (t_eva['min_amt'] <= float(self.p['deal_amt'])) &
                        (t_eva['max_amt'] > float(self.p['deal_amt'])) &
                        (t_eva['sens_flg'] == int(self.p['sens_flg'])) &
                        (t_eva['ib_flg'] == int(self.p['ib_flg'])) &
                        (t_eva['tem_cd'] == self.p['tem_bucket_eva'])
                        ]

                    if len(filter_target_eva) < 1:
                        log.info('--- No TARGET EVA NSO found. Use default value 0.0 ---')
                        return 0.0

                    log.info(f"--- Shape of filtered TARGET EVA NSO data is {filter_target_eva.shape} ---")
                    log.info("--- END of processing TARGET EVA NSO ---")
                    return np.round(float(filter_target_eva['eva_rate'].unique()[0]), 6)

            except Exception as e:
                log.error(f"--- Error due processing TARGET EVA data: {e} . Use default value 0.0 ---")
                return 0.0


def make_deals_report_query(
        query: str,
        period_start: str,
        period_end: str,
        inns: Union[None, str, Sequence[str]] = None
) -> str:
    """
    period_start/period_end: 'YYYY-MM-DD'
    inns: None | '770...' | ['770...','540...'] | ('770...','540...')
    """
    inn_filter = ""

    if inns:
        if isinstance(inns, str):
            inn_list = [inns]
        else:
            inn_list = list(inns)

        cleaned = sorted(set(normalize_inn(x) for x in inn_list))
        in_sql = "(" + ",".join(f"'{x}'" for x in cleaned) + ")"
        inn_filter = f"AND inn_num IN {in_sql}"

    return query.format(period_start, period_end, inn_filter)


class ReportExtractor:
    def __init__(
            self,
            inns=None,
            period_start=None,
            period_end=None,

    ):

        self.inns = inns
        self.period_start = period_start
        self.period_end = period_end

    def download_data(self, data_type: dict=None, query: str=None) -> pd.DataFrame:
        params = LOAD_KWARGS[data_type]
        add_query = """SELECT * FROM custom_fin_palm_dataops.lb_deal_depo_fct WHERE ccy_cd = 'RUB'"""
        try:
            df = run_trino(query, catalog=params['catalog'], schema=params['schema'])
            if len(df) < 1:
                log.info(f"--- Empty dataset ---")
                try:
                    df = run_trino(add_query, catalog=params['catalog'], schema=params['schema'])
                    if len(df) < 1:
                        log.info(f"--- Empty dataset. AGAIN. ---")
                        return None
                except Exception as e:
                    log.error(f"Error loading data {data_type}: {e}")
                    return None
            log.info(f"data {data_type} has been loaded successfully! shape: {df.shape}")
            return df
        except Exception as e:
            log.error(f"Error loading data {data_type}: {e}")
            return None


    def filter_data(self, df: pd.DataFrame) -> pd.DataFrame:
        log.info("--- Start filtering data ---")
        mask = df.internal_order_cd != ''
        df['value_dt'] = pd.to_datetime(df['value_dt'])
        df['maturity_dt'] = pd.to_datetime(df['maturity_dt'])
        df['deal_dt'] = pd.to_datetime(df['deal_dt'])

        df.loc[:, 'term']  = df['maturity_dt'] - df['deal_dt']
        df['maturity_dt'] = df['maturity_dt'].dt.strftime('%Y-%m-%d')
        df['single_deal_amt'] = df['single_deal_amt'].astype(float)
        df['interest_rate'] = df['interest_rate'].astype(float)
        df['original_delta_limit_amt'] = df['original_delta_limit_amt'].astype(float)
        df['delta_limit_amt'] = df['delta_limit_amt'].astype(float)
        df['marginal_income_amt'] = df['marginal_income_amt'].astype(float)
        df['eva_rate'] = df['eva_rate'].astype(float)

        df_f = df[mask]

        log.info(f"--- End of filtering data. Shape of filtered data is {df_f.shape} ---")

        return df_f


    def calc_stats(self, df: pd.DataFrame) -> dict:
        log.info("--- Start making report data ---")
        report = dict()

        report['n_deals'] = df.internal_order_cd.nunique()
        count_by_product = df.groupby('product_cd')['internal_order_cd'].nunique().to_dict()
        report['count_by_product'] = ", ".join(f'{k}: {v}' for k,v in count_by_product.items())
        report['weighted_mean_term'] = (df.term.dt.days * df.single_deal_amt).sum() / (df.single_deal_amt.sum() + 1e-4)
        report['weighted_mean_interest_rate'] = (df.interest_rate * df.single_deal_amt).sum() / (df.single_deal_amt.sum() + 1e-4)
        report['weighted_mean_sum_limit'] = (df.original_delta_limit_amt * df.single_deal_amt).sum() / (df.single_deal_amt.sum() + 1e-4)
        report['weighted_amt'] = (df.single_deal_amt * df.term.dt.days).sum() / (df.term.dt.days.sum() + 1e-4)
        report['maturity_dt_min'] = df.maturity_dt.min()
        report['maturity_dt_max'] = df.maturity_dt.max()
        report['single_deal_amt_min'] = df.single_deal_amt.min()
        report['single_deal_amt_max'] = df.single_deal_amt.max()
        report['interest_rate_min'] = df.interest_rate.min()
        report['interest_rate_max'] = df.interest_rate.max()
        report['delta_limit_amt_min'] = df.delta_limit_amt.min()
        report['delta_limit_amt_max'] = df.delta_limit_amt.max()
        report['original_delta_limit_amt_min'] = df.original_delta_limit_amt.min()
        report['original_delta_limit_amt_max'] = df.original_delta_limit_amt.max()
        report['marginal_income_amt_min'] = df.marginal_income_amt.min()
        report['marginal_income_amt_max'] = df.marginal_income_amt.max()
        report['eva_rate_min'] = df.eva_rate.min()
        report['eva_rate_max'] = df.eva_rate.max()
        log.info("--- End making report data ---")
        return report

    def extract_report(self):

        if self.inns:
            if isinstance(self.inns, str):
                inns = [self.inns]
            else:
                inns = self.inns
        else:
            log.info('--- No inns specified ---')
            return None

        query = make_deals_report_query(DEALS_REPORT, self.period_start, self.period_end, inns)
        deals_df = self.download_data(data_type="depo", query=query)
        if deals_df is None or len(deals_df) == 0:
            log.info('--- No deals found ---')
            return None
        deals_df = self.filter_data(deals_df)
        report = self.calc_stats(deals_df)
        return report
