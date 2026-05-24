# ---------------------------------------------------------------------------
# EVA / ETC / Option Price / CRL
# ---------------------------------------------------------------------------
def get_term_label(term: int, is_eva: bool = False) -> str:
    if is_eva:
        term_key = [
            "1D", "2D", "3D", "4D", "5D", "6D",
            "1W", "2W", "3W",
            "1M", "2M", "3M", "4M", "5M", "6M", "9M",
            "1Y", "1.5Y", "2Y", "3Y",
        ]
        term_val = [1, 2, 3, 4, 5, 6, 7, 14, 21, 31, 61, 92, 122, 153, 183, 274, 366, 549, 731, 1096]
    else:
        term_key = ["1D", "3D", "4D", "1W", "2W", "3W", "1M", "2M", "3M", "1Y", "1.5Y", "2Y", "3Y"]
        term_val = [1, 3, 4, 7, 14, 21, 31, 61, 92, 366, 549, 731, 1096]

    idx = 0
    while term > term_val[idx]:
        idx += 1
    return term_key[idx]


def get_term_label_crl(term: int) -> str:
    term_val = [1, 3, 4, 6, 7, 14, 21, 31, 61, 92, 366, 549, 731, 1096]
    term_key = ["1D", "3D", "4D", "6D", "1W", "2W", "3W", "1M", "2M", "3M", "1Y", "1.5Y", "2Y", "3Y"]
    idx = 0
    while term > term_val[idx]:
        idx += 1
    return term_key[idx]


# ---------------------------------------------------------------------------
# Лимитные ставки (инвалюта / RUB)
# ---------------------------------------------------------------------------

def get_term_label_lim_rates(term: int) -> str:
    """Бакеты для foreign_limit_rates: '1D', '3D', '4D', ..."""
    term_val = [1, 3, 4, 6, 7, 14, 21, 31, 61, 92, 366, 549, 731, 1096]
    term_key = ["1D", "3D", "4D", "6D", "1W", "2W", "3W", "1M", "2M", "3M", "1Y", "1.5Y", "2Y", "3Y"]
    idx = 0
    while term > term_val[idx]:
        idx += 1
    return term_key[idx]


def get_term_label_limit_rates(term: int) -> str:
    """Бакеты для limit_rates_rub: '1-3D', '4-6D', ..."""
    term_val = [3, 6, 7, 14, 21, 31, 61, 92, 122, 153, 183, 274, 366, 549, 731, 1096]
    term_key = ['1-3D', '4-6D', '1W', '2W', '3W', '1M', '2M', '3M', '4M', '5M', '6M', '9M', '1Y', '1.5Y', '2Y', '3Y']
    idx = 0
    while term > term_val[idx]:
        idx += 1
    return term_key[idx]


# ---------------------------------------------------------------------------
# КС + спреды (плавающая ставка)
# ---------------------------------------------------------------------------
def get_term_label_cbr_spreads(term: int) -> str:
    term_val = [61, 92, 122, 153, 183, 274, 366]
    term_key = ["2M", "3M", "4M", "5M", "6M", "9M", "1Y"]
    idx = 0
    while term > term_val[idx]:
        idx += 1
    return term_key[idx]


def get_vol_labels_cbr_spreads(amount: float) -> str:

    vol = amount/1e6

    if vol < 500:
        return None
    if vol >= 500 and vol < 1_000:
        return "from_half"
    elif vol >= 1_000 and vol < 3_000:
        return "from_one"
    elif vol >= 3_000 and vol < 10_000:
        return "from_three"
    elif vol >= 10_000:
        return "from_ten"


# ---------------------------------------------------------------------------
# Исторические спреды клиентов (AVG_CLIENTS_SPREAD)
# ---------------------------------------------------------------------------
def get_term_label_clients(term: int) -> str:
    """Бакеты срочности для history_spreads: '1-3D', '4-6D', ..."""
    buckets = [
        (3, "1-3D"), (6, "4-6D"), (7, "1W"), (14, "2W"), (21, "3W"), (31, "1M"),
        (61, "2M"), (92, "3M"), (122, "4M"), (153, "5M"), (183, "6M"), (274, "9M"),
        (366, "1Y"), (549, "1.5Y"), (731, "2Y"),
    ]

    for upper_bound, bucket in buckets:
        if term <= upper_bound:
            return bucket

    return "3Y"


def get_vol_label_clients(amount, type_ = '', neighbour = False):
    """Бакеты объёма для history_spreads: '0-30', '30-100', ..."""
    vol_split = [0, 10, 30, 100, 500, 3000, 1000000]
    vol_ = 0
    while vol_split[vol_] <= (amount / 1000000):
        vol_ += 1
    return '{0}-{1}'.format(vol_split[vol_ - 1], vol_split[vol_])


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------
def normalize_inn(inn):
    inn = str(inn)
    if inn == 'nan':
        return None
    if len(inn) <10:
        inn = inn.zfill(10)
    elif len(inn) == 11:
        inn = inn.zfill(12)
    return inn
