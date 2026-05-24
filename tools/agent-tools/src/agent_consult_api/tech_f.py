import math
import numpy as np
import pandas as pd
from decimal import Decimal


def is_nan(x):
    if isinstance(x, float) or isinstance(x, np.floating):
        return math.isnan(float(x))
    if isinstance(x, Decimal):
        return x.is_nan()
    return isinstance(x, float) and x != x


def is_none_or_nan(x):
    return x is None or is_nan(x)


def _almost_equal(a: float, b: float, rel=1e-4, abs_=1e-2):  # 1 б.п. + копейки
    return abs(a-b) <= max(rel*max(abs(a),abs(b)), abs_)


def _decimal_to_float(x):
    if isinstance(x, Decimal): return np.round(float(x), 4)
    if isinstance(x, dict): return {k: _decimal_to_float(v) for k,v in x.items()}
    if isinstance(x, list): return [_decimal_to_float(v) for v in x]
    return x


def _row_from_prefetched(prefetched_row: dict | None) -> dict | None:
    """
    Позволяет переиспользовать уже найденную сделку из снэпшота.
    Требуем базовые поля — если их нет, вернём None и пойдём обычным путём.
    """
    if not prefetched_row: return None
    need = {"deal_dt","end_dt","value_dt","ccy_cd","product_cd","deal_amt","interest_rate"}
    return prefetched_row if need.issubset(set(prefetched_row.keys())) else None


def normalize_inn(inn):
    inn = str(inn)
    if inn == 'nan':
        return None
    if len(inn) <10:
        inn = inn.zfill(10)
    elif len(inn) == 11:
        inn = inn.zfill(12)
    return inn


def get_optionality_type(
        df: pd.DataFrame=None,
        withdrawal_col_name: str='withdrawal_option',
        top_up_col_name: str='top_up_option_flg'
) -> str:

    """Определение типа опциона: OTZ | POP | OTZ_POP | '' для маппинга СРЛ"""

    none_conditions = (df[withdrawal_col_name] == 0) & \
                      (df[top_up_col_name] == 0) & \
                      (df['product_cd'] == 'DEPO')

    df.loc[none_conditions, 'optionality_type'] = ''

    pop_conditions = (df[withdrawal_col_name] == 0) & \
                     (df[top_up_col_name] == 1) & \
                     (df['product_cd'] == 'DEPO')

    df.loc[pop_conditions, 'optionality_type'] = 'POP'

    otz_conditions = ((df['product_cd'] == 'NSO') |
                      (df[withdrawal_col_name] == 1) & \
                      (df[top_up_col_name] == 0) & \
                      (df['product_cd'] == 'DEPO'))

    df.loc[otz_conditions, 'optionality_type'] = 'OTZ'

    otz_pop_conditions = (df[withdrawal_col_name] == 1) & \
                         (df[top_up_col_name] == 1) & \
                         (df['product_cd'] == 'DEPO')

    df.loc[otz_pop_conditions, 'optionality_type'] = 'OTZ_POP'

    return df


def get_term_label(term, type_ = ''):
    """
    Функция определения бакета срочности
    INPUT: term: int/ float - срок
           type_: str - тип, в зависимости от которого выбираются градации срочности
    RETURN: бакет срочности: str
    """
    if type_ == 'eva':
        term_key = ['1-3D', '4-6D', '7D', '2W', '3W', '1M', '2M', '3M', '4M', '5M', '6M', '9M', '1Y', '1.5Y', '2Y', '3Y']
        term_val = [3, 6, 7, 14, 21, 31, 61, 92, 122, 153, 183, 274, 366, 549, 731, float('inf')]

    elif type_ == 'cost_liq_risk':
        term_key = ['1D', '3D', '4D', '6D', '7D', '2W', '3W', '1M', '2M', '3M', '1Y', '1.5Y', '2Y', '3Y', '3+Y']
        term_val = [2, 3, 4, 6, 7, 14, 21, 31, 61, 92, 366, 549, 731, 1096, float('inf')]

    else:
        raise Exception("Unknown term state")

    index_term = 0
    if term > max(term_val):
        index_term = -1
    else:
        while term > term_val[index_term]:
            index_term += 1
    return term_key[index_term]
