import datetime
import logging
import numpy as np
import pandas as pd
from typing import Any, Dict, List

from .config import settings, ERROR_TEXT, ERROR_KPK_LIMIT_TEXT

_LOGGER = logging.getLogger(__name__)
_TABULATE_FALLBACK_WARNED = False


def _limit_support_note() -> str:
    """Возвращает унифицированную подсказку по эскалации вопросов расчета лимита."""
    return ERROR_KPK_LIMIT_TEXT.strip()


def _format_money(val: Any) -> Any:
    """Вспомогательная функция для форматирования одиночных числовых значений с пробелами."""
    if isinstance(val, (int, float)):
        return f"{val:,.2f}".replace(",", " ")
    return val


def _format_numeric_cols(df: pd.DataFrame, decimals: int = 2) -> pd.DataFrame:
    """Округляет числовые колонки и добавляет пробелы между тысячами для удобства чтения."""
    num_cols = df.select_dtypes(include="number").columns
    for col in num_cols:
        df[col] = df[col].apply(
            lambda x: f"{x:,.{decimals}f}".replace(",", " ") if pd.notnull(x) else x
        )
    return df


def _render_table(df: pd.DataFrame) -> str:
    """
    Возвращает markdown-таблицу, а если tabulate недоступен - текстовую таблицу в code block.
    dcc.Markdown в ui_dash.py корректно отрисует оба варианта.
    """
    global _TABULATE_FALLBACK_WARNED

    try:
        return df.to_markdown(index=False)
    except ImportError:
        if not _TABULATE_FALLBACK_WARNED:
            _LOGGER.warning(
                "[_render_table] tabulate недоступен, используем fallback через DataFrame.to_string()"
            )
            _TABULATE_FALLBACK_WARNED = True
        plain_table = df.fillna("").to_string(index=False)
        return f"```\n{plain_table}\n```"


def _map_readable_name(val: Any) -> Any:
    """
    Пытается перевести системный статус/имя в человекочитаемый вид.
    Если не находит в словаре, очищает от URN префиксов.
    """
    if pd.isna(val) or not isinstance(val, str):
        return val
    if val in _HUMAN_READABLE_MAPPING:
        return _HUMAN_READABLE_MAPPING[val]
    # Очистка неизвестных системных URN
    if val.startswith("urn:"):
        return val.split(":")[-1]
    return val


def _get_effective_today() -> pd.Timestamp:
    """Возвращает effective today с учетом override из конфига."""
    today_override = getattr(settings, "kpk_today_override", None)
    if today_override:
        try:
            return pd.Timestamp(datetime.date.fromisoformat(today_override)).normalize()
        except ValueError:
            _LOGGER.warning(
                "[_get_effective_today] Некорректный kpk_today_override=%s, используем системную дату",
                today_override,
            )
    return pd.Timestamp(datetime.date.today()).normalize()


def _parse_table_date(val: Any) -> Any:
    """Нормализует дату из payload/таблицы в Timestamp."""
    if pd.isna(val) or val in {"", "Н/Д", None}:
        return pd.NaT
    parsed = pd.to_datetime(val, errors="coerce", dayfirst=True)
    if pd.isna(parsed):
        return pd.NaT
    return pd.Timestamp(parsed).normalize()


def _resolve_current_deal_status(row: pd.Series) -> str:
    """Заполняет текущий статус сделки, если он не пришел из calc-логов."""
    calc_log_note = _map_readable_name(row.get("last_log_status"))
    if (
            isinstance(calc_log_note, str)
            and calc_log_note.strip()
            and calc_log_note.strip().lower() != "в процессе расчета"
    ):
        return calc_log_note

    today = _get_effective_today()
    value_dt = _parse_table_date(row.get("value_dt"))
    if pd.notna(value_dt) and value_dt >= today:
        return "Отложенная по дате валютирования"

    maturity_dt = _parse_table_date(row.get("maturity_dt"))
    if pd.notna(maturity_dt) and maturity_dt >= today:
        return "Действующая сделка"

    return "Завершена"


def _resolve_last_action_date(row: pd.Series) -> str:
    """Нормализует отображаемую дату последнего действия по сделке."""
    fallback_last_action = _get_effective_today().strftime("%Y-%m-%d") + " 01:00"
    current_status = row.get("last_log_status")
    raw_status = row.get("_raw_last_log_status")

    normalized_raw = None
    if isinstance(raw_status, str) and raw_status.strip():
        normalized_raw = raw_status.strip()
    elif isinstance(current_status, str) and current_status.strip():
        normalized_raw = _normalize_status_key(current_status.strip())

    # Для активных сделок дата в таблице должна отражать текущий расчетный день,
    # даже если последний техлог `calculated` был записан раньше.
    if normalized_raw not in {"ClientDeclined", "BreachDealClosed", "EarlyTerminated", "BankDeclined"}:
        return fallback_last_action

    last_action = row.get("last_log_date")
    if pd.isna(last_action) or last_action in {"", "Н/Д", None}:
        return fallback_last_action
    return last_action


def _authors_table(authors: List[Dict]) -> str:
    """Таблица перераспределений по авторам."""
    try:
        df = pd.DataFrame(authors)
        cols = ["author_nm", "transaction_limit_amt"]
        col_names = ["ФИО", "Влияние на лимит"]
        if "report_dt" in df.columns:
            dates = pd.to_datetime(df["report_dt"], errors="coerce").dt.date.dropna().unique().tolist()
            if len(dates) > 1:
                df["report_dt"] = pd.to_datetime(df["report_dt"], errors="coerce").dt.strftime("%Y-%m-%d")
                cols = ["report_dt"] + cols
                col_names = ["Дата"] + col_names
        if "is_ca_compensation" in df.columns and df["is_ca_compensation"].any():
            df["ca_label"] = df.apply(
                lambda r: f"компенсация от ЦА" if r.get("is_ca_compensation") else "",
                axis=1,
            )
            cols.append("ca_label")
            col_names.append("Примечание")
        df = df[cols]
        df.columns = col_names
        _format_numeric_cols(df)
        return _render_table(df)
    except Exception as e:
        _LOGGER.warning(
            "[_authors_table] Ошибка формирования таблицы авторов. Кол-во authors=%d: %s",
            len(authors), e
        )
        return ""


def _redistribution_heading(authors: List[Dict]) -> str:
    if not authors:
        return "#### Перераспределения по КПК:"
    total = 0.0
    for row in authors:
        try:
            total += float(row.get("transaction_limit_amt", 0) or 0)
        except (TypeError, ValueError):
            continue
    if total < 0:
        return "#### Перераспределения с КПК:"
    if total > 0:
        return "#### Перераспределения на КПК:"
    return "#### Перераспределения по КПК:"


_DEAL_STATUS_LABELS = {
    "km_cotirovka": "Котировка менеджера",
    "auto_cotirovka": "Автокотировка",
    "group_deal": "Групповая сделка",
    "incorrect": "Требует ручной проверки",
}

_HUMAN_READABLE_MAPPING = {
    "urn:sbrfsystems:99-pprb:upp": "ППРБ",
    "urn:sbrfsystems:99-ufs-depositsweb": "Автокотировка",
    "urn:sbrfsystems:99-ufs-sct": "групповая сделка",
    "urn:sbrfsystems:99-ufs-sr": "ЕФС",
    "BreachDealClosed": "Несоблюдение условий",
    "calculated": "В процессе расчета",
    "Draft": "Черновик",
    "ClientDeclined": "Отклонена клиентом",
    "BankDeclined": "Отклонена банком",
    "EarlyTerminated": "Закрыта досрочно"
}

_STATUS_LIMIT_EXPLANATIONS = {
    "ClientDeclined": (
        "отказ от посланной менеджером сделки; "
        "лимит возвращается через 1 рабочий день после отказа клиента"
    ),
    "BreachDealClosed": (
        "соглашение на сделку есть, но деньги не предоставлены в дату валютирования; "
        "лимит возвращается на утро 5-го рабочего дня после даты валютирования"
    ),
    "EarlyTerminated": (
        "закрытие уже заключенного договора досрочно; "
        "по текущей логике проекта сделка продолжает влиять на лимит"
    ),
    "BankDeclined": (
        "сделка отклонена банком; лимит возвращается сразу"
    ),
}


def _normalize_status_key(status: Any) -> Any:
    if pd.isna(status) or not isinstance(status, str):
        return status
    for raw, human in _HUMAN_READABLE_MAPPING.items():
        if status == raw or status == human:
            return raw
    return status


def _status_limit_explanation(status: Any) -> str | None:
    normalized = _normalize_status_key(status)
    explanation = _STATUS_LIMIT_EXPLANATIONS.get(normalized)
    if not explanation:
        return None
    title = _map_readable_name(normalized)
    return f"{title}: {explanation}."

_DEALS_COLS = {
    "internal_order_cd": "id сделки",
    "client_name": "Клиент",
    "source_system_cd": "Система котирования",
    "product_cd": "Продукт",
    "delta_limit_amt": "Влияние на лимит",
    "inn": "ИНН",
    "value_dt": "Дата валютирования",
    "deal_dt": "Дата договора",
    "first_upload_dt": "Дата начала влияния на лимит",
    "maturity_dt": "Дата окончания",
    # "calc_log_note": "Текущий статус",
}


def _deals_table(top_deals: List[Dict]) -> str:
    """Таблица топ-отрицательных сделок."""
    try:
        max_rows = settings.max_deals_table_rows
        df = pd.DataFrame(top_deals[:max_rows])
        df = _apply_md_display_dates(df)

        if "deal_status" in df.columns:
            df["deal_status_label"] = df["deal_status"].map(_DEAL_STATUS_LABELS).fillna(df["deal_status"])

        if "source_system_cd" in df.columns:
            df["source_system_cd"] = df["source_system_cd"].apply(_map_readable_name)
        # df["calc_log_note"] = df.apply(_resolve_current_deal_status, axis=1)

        available = [c for c in _DEALS_COLS if c in df.columns]
        df = df[available]
        df.rename(columns={k: v for k, v in _DEALS_COLS.items() if k in available}, inplace=True)
        _format_numeric_cols(df)
        result = _render_table(df)
        extra = len(top_deals) - max_rows
        if extra > 0:
            result += f"\n\n*...и еще {extra} сделок (не показаны).*"
        return result
    except Exception as e:
        _LOGGER.warning(
            "[_deals_table] Ошибка формирования таблицы сделок. Кол-во deals=%d: %s",
            len(top_deals), e
        )
        return ""


def _deals_analysis_block(deals: Dict, md_lines: List[str]) -> None:
    """Добавляет блок анализа сделок в md_lines (in-place)."""
    try:
        impact_neg = deals.get('deals_impact_correct_neg', 0)
        impact_pos = deals.get('deals_impact_correct_pos', 0)
        top_deals = deals.get("top_negative_deals", [])

        # Если нет никакого влияния и нет сделок, просто выходим
        if impact_neg == 0 and impact_pos == 0 and not top_deals:
            return

        md_lines.append("\n#### Влияние сделок на лимит КПК:")

        if impact_neg != 0 or impact_pos != 0:
            if impact_neg != 0:
                md_lines.append(f"- Отрицательное влияние: **{_format_money(impact_neg)}**")
            if impact_pos != 0:
                md_lines.append(f"- Положительное влияние: **{_format_money(impact_pos)}**")
            md_lines.append("")

        if not top_deals:
            return

        md_lines.append("**Детализация сделок с отрицательным влиянием на лимит КПК:**")
        deals_tbl = _deals_table(top_deals)
        if deals_tbl:
            md_lines.append(deals_tbl)

        md_lines.append("\n**Детали изменения лимита:**")
        for d in top_deals:
            reasons = []
            status = d.get("deal_status", "")
            source_system = str(d.get("source_system_cd", "")).strip().lower()
            mapped_source_system = str(_map_readable_name(d.get("source_system_cd")) or "").strip().lower()
            is_efs_deal = source_system == _EFS_SOURCE or mapped_source_system == _EFS_DISPLAY_NAME
            delta_limit_amt = pd.to_numeric(d.get("delta_limit_amt"), errors="coerce")
            status_label = _DEAL_STATUS_LABELS.get(status)
            if status_label:
                reasons.append(status_label.lower())
            if d.get("prev_division"):
                reasons.append(f"переведена из КПК {d.get('prev_division')}")
            if d.get("v_dt_weekday") == 5:
                reasons.append("дата валютирования выпала на субботу")
            if d.get("days_diff", 0) > 1:
                reasons.append(f"начала влияние на утро {d.get('days_diff')}-го дня после валютирования")
            if d.get("value_dt_after_deal"):
                if pd.notna(delta_limit_amt) and delta_limit_amt < 0 and is_efs_deal:
                    reasons.append(
                        "влияние отрицательных сделок из ЕФС. Наш бизнес начинается сразу в момент заключения сделки"
                    )
            if d.get("value_dt_after_upload"):
                reasons.append("Изменение ЕТС за период с момента расчета до момента заключения изменило влияние на лимит")
            calc_note = d.get("calc_log_note")
            if calc_note:
                reasons.append(calc_note)
            reason_str = ", ".join(reasons) if reasons else "новая сделка в системе"
            md_lines.append(f"* id сделки {d.get('internal_order_cd')}: {reason_str.capitalize()}.")
    except Exception as e:
        _LOGGER.warning("kpk_deals_analysis_block_failed. error=%s", e)


_INCORRECT_DEALS_COLS = {
    "internal_order_cd": "id сделки",
    "client_name": "Клиент",
    "source_system_cd": "Система котирования",
    "product_cd": "Продукт",
    "delta_limit_amt": "Влияние на лимит",
    "inn": "ИНН",
    "value_dt": "Дата валютирования",
    "deal_dt": "Дата договора",
    "first_upload_dt": "Дата начала влияния на лимит",
}

_EFS_SOURCE = "urn:sbrfsystems:99-ufs-sr"
_EFS_DISPLAY_NAME = "ефс"


def _apply_md_display_dates(df: pd.DataFrame) -> pd.DataFrame:
    """Корректирует только отображаемые даты в markdown-таблицах."""
    if df.empty:
        return df
    if not {"source_system_cd", "first_upload_dt", "deal_dt"}.issubset(df.columns):
        return df

    df = df.copy()
    raw_source = df["source_system_cd"].astype(str).str.strip().str.lower()
    mapped_source = df["source_system_cd"].apply(_map_readable_name).astype(str).str.strip().str.lower()
    efs_mask = (raw_source == _EFS_SOURCE) | (mapped_source == _EFS_DISPLAY_NAME)
    if "delta_limit_amt" in df.columns:
        delta = pd.to_numeric(df["delta_limit_amt"], errors="coerce")
        efs_mask = efs_mask & (delta < 0)
    df.loc[efs_mask, "first_upload_dt"] = df.loc[efs_mask, "deal_dt"]
    return df


def _incorrect_deals_block(deals: Dict, md_lines: List[str]) -> None:
    """Блок сделок, требующих ручной проверки."""
    try:
        incorrect = deals.get("incorrect_deals", [])
        if not incorrect:
            return

        md_lines.append("\n#### Сделки, требующие ручной проверки:")
        count = deals.get("incorrect_deals_count", len(incorrect))
        impact = deals.get("deals_impact_incorrect", 0)
        md_lines.append(f"- Количество: **{count}**")
        md_lines.append(f"- Суммарное влияние: **{_format_money(impact)}**\n")

        max_rows = settings.max_deals_table_rows
        df = pd.DataFrame(incorrect[:max_rows])
        df = _apply_md_display_dates(df)

        if "source_system_cd" in df.columns:
            df["source_system_cd"] = df["source_system_cd"].apply(_map_readable_name)

        available = [c for c in _INCORRECT_DEALS_COLS if c in df.columns]
        df = df[available].rename(columns={k: v for k, v in _INCORRECT_DEALS_COLS.items() if k in available})
        _format_numeric_cols(df)
        md_lines.append(_render_table(df))
        extra = len(incorrect) - max_rows
        if extra > 0:
            md_lines.append(f"\n*...и еще {extra} сделок, требующих ручной проверки (не показаны).*")
        md_lines.append(
            "\nПо данным сделкам требуется дополнительная проверка. "
            f"{_limit_support_note()}"
        )
    except Exception as e:
        _LOGGER.warning("kpk_incorrect_deals_block_failed. error=%s", e)


_DISCOUNT_COLS = {
    "internal_order_cd": "id сделки",
    "client_name": "Клиент",
    "product_cd": "Продукт",
    "inn": "ИНН",
    "original_delta_limit_amt": "Влияние на лимит при котировании",
    "delta_limit_amt_prev": "Предыдущее влияние",
    "delta_limit_amt_t": "Влияние на дату запроса",
    "discount_change": "Изменение",
}

_TOP_UP_COLS = {
    "internal_order_cd": "id сделки",
    "client_name": "Клиент",
    "product_cd": "Продукт",
    "inn": "ИНН",
    "delta_limit_amt_prev": "Предыдущее влияние",
    "delta_limit_amt": "Влияние на дату запроса",
    "delta_limit_change": "Разница",
    "value_dt": "Дата валютирования",
    "deal_dt": "Дата договора",
}


def _non_compliance_block(non_compliance_deals: List[Dict], md_lines: List[str]) -> None:
    """Блок сделок с несоответствием условий (BreachDealClosed)."""
    if not non_compliance_deals:
        return
    try:
        md_lines.append("\n#### Сделки с несоответствием условий договора:")

        max_rows = settings.max_deals_table_rows
        df = pd.DataFrame(non_compliance_deals[:max_rows])
        cols = {
            "internal_order_cd": "id сделки",
            "client_name": "Клиент",
            "value_dt": "Дата валютирования",
            "delta_limit_amt": "Влияние на лимит КПК",
            "inn_num": "ИНН",
            "return_date": "Дата компенсации лимита",
        }
        available = [c for c in cols if c in df.columns]
        df = df[available].rename(columns={k: v for k, v in cols.items() if k in available})
        _format_numeric_cols(df)
        md_lines.append(_render_table(df))
        extra = len(non_compliance_deals) - max_rows
        if extra > 0:
            md_lines.append(f"\n*...и еще {extra} сделок с несоответствием (не показаны).*")

        for deal in non_compliance_deals[:max_rows]:
            note = deal.get("note")
            if note:
                md_lines.append(f"* id сделки {deal.get('internal_order_cd')}: {note}.")
    except Exception as e:
        _LOGGER.warning("kpk_non_compliance_block_failed. error=%s", e)


def _format_limit_reversal(orig_amt: Any) -> tuple[str, str]:
    """Возвращает подпись и сумму для возврата/изъятия лимита по исчезнувшей сделке."""
    try:
        numeric_amt = float(orig_amt)
    except (TypeError, ValueError):
        return "Изменение лимита", "Н/Д"

    if numeric_amt < 0:
        return "Возврат в лимит", _format_money(abs(numeric_amt))
    if numeric_amt > 0:
        return "Изъятие из лимита", _format_money(abs(numeric_amt))
    return "Изменение лимита", _format_money(0)


def _disappeared_deals_block(disappeared: List[Dict], md_lines: List[str]) -> None:
    """Блок отмененных/отозванных сделок."""
    if not disappeared:
        return
    try:
        md_lines.append("\n#### Отмененные/отозванные сделки (статусы из витрины расчетов):")

        _LOG_COLS = {
            "status_cd": "Статус",
            "calculation_dttm": "Дата расчета",
            "inn_num": "ИНН",
            "value_dt": "Дата валютирования",
        }

        max_rows = settings.max_deals_table_rows
        if len(disappeared) > max_rows:
            md_lines.append(f"*Показаны первые {max_rows} из {len(disappeared)} сделок.*\n")
        for entry in disappeared[:max_rows]:
            order_cd = entry.get("internal_order_cd", "Н/Д")
            orig_amt = entry.get("original_delta_limit_amt")
            limit_effect_label, amt_str = _format_limit_reversal(orig_amt)
            md_lines.append(
                f"\n**id сделки {order_cd}** | "
                f"Продукт: {entry.get('product_cd', 'Н/Д')} | "
                f"Дата вал.: {entry.get('value_dt', 'Н/Д')} | "
                f"{limit_effect_label}: {amt_str}"
            )
            logs = entry.get("log_entries", [])
            if logs:
                log_df = pd.DataFrame(logs)
                if "status_cd" in log_df.columns:
                    log_df["status_cd"] = log_df["status_cd"].apply(_map_readable_name)

                available = [c for c in _LOG_COLS if c in log_df.columns]
                log_df = log_df[available].rename(columns={k: v for k, v in _LOG_COLS.items() if k in available})
                _format_numeric_cols(log_df)
                md_lines.append(_render_table(log_df))
            explained_statuses = []
            for log in logs:
                explanation = _status_limit_explanation(log.get("status_cd"))
                if explanation and explanation not in explained_statuses:
                    explained_statuses.append(explanation)
            for explanation in explained_statuses:
                md_lines.append(f"* id сделки {order_cd}: {explanation}")
    except Exception as e:
        _LOGGER.warning("kpk_disappeared_deals_block_failed. error=%s", e)


def _discounting_block(discounting: Dict, md_lines: List[str]) -> None:
    """Добавляет блок анализа дисконтирования в md_lines (in-place)."""
    try:
        impact_total = np.round(discounting.get('discount_impact_total') or 0, 2)
        deals = discounting.get("significant_deals", [])

        # Если данных нет, ничего не выводим
        if impact_total == 0 and not deals:
            return

        md_lines.append("\n#### Влияние дисконтирования:")
        if impact_total != 0:
            md_lines.append(f"- Суммарное влияние дисконтирования: **{_format_money(impact_total)}**\n")

        if deals:
            md_lines.append("**Сделки со значимым влиянием дисконтирования:**")
            df = pd.DataFrame(deals)
            df = _apply_md_display_dates(df)
            available = [c for c in _DISCOUNT_COLS if c in df.columns]
            df = df[available]
            df.rename(columns={k: v for k, v in _DISCOUNT_COLS.items() if k in available}, inplace=True)
            _format_numeric_cols(df)
            md_lines.append(_render_table(df))
    except Exception as e:
        _LOGGER.warning("kpk_discounting_block_failed. error=%s", e)


def _top_up_option_block(top_up: Dict, md_lines: List[str]) -> None:
    """Блок сделок с активацией опции пополнения."""
    deals = top_up.get("top_up_deals", [])
    if not deals:
        return
    try:
        md_lines.append("\n#### Активация опции пополнения:")

        impact_total = np.round(top_up.get('top_up_total_impact') or 0, 2)
        if impact_total != 0:
            md_lines.append(f"- Суммарное дополнительное влияние: **{_format_money(impact_total)}**\n")

        df = pd.DataFrame(deals)
        df = _apply_md_display_dates(df)
        available = [c for c in _TOP_UP_COLS if c in df.columns]
        df = df[available].rename(columns={k: v for k, v in _TOP_UP_COLS.items() if k in available})
        _format_numeric_cols(df)
        md_lines.append(_render_table(df))

        md_lines.append(
            """\n**Пояснение:** У перечисленных сделок между анализируемыми датами в учетных системах Банка появилась опция пополнения. 
            В случае наличия в сделке опции пополнения отрицательное влияние сделки на лимит удваивается 
            (поскольку банк принимает на себя дополнительный процентный риск по возможным будущим пополнениям).
            В таблице показана разница влияния на лимит между текущей и предыдущей датой анализа.
            В случае, если Вы и/или Клиент не инициировали включение в условия действующей сделки опции пополнения, необходимо обратиться в Центр сопровождения корпоративного бизнеса для уточнения причин корректировки условий сделки."""
        )
    except Exception as e:
        _LOGGER.warning("kpk_top_up_option_block_failed. error=%s", e)


# ─── public API ───────────────────────────────────────────────────────────────

def generate_kpk_report_markdown(data: Dict[str, Any]) -> str:
    """Преобразует JSON ответ инструмента в готовую строку Markdown."""
    if not data or data.get("mode") != "single":
        return ERROR_TEXT

    try:
        return _generate_kpk_report_markdown_impl(data)
    except Exception as e:
        _LOGGER.error("generate_kpk_report_failed. error=%s", e)
        return ERROR_TEXT


def _generate_kpk_report_markdown_impl(data: Dict[str, Any]) -> str:
    md_lines = []
    md_lines.append("#### Анализ лимита подразделения (КПК)")

    prev_dt = data.get("prev_report_dt")
    report_dt = data.get("report_dt")
    as_of_morning_dt = data.get("as_of_morning_dt")
    data_report_dt = data.get("data_report_dt") or report_dt
    if as_of_morning_dt:
        md_lines.append(f"**Анализ на утро:** {as_of_morning_dt}")
    if prev_dt:
        label = "Период данных витрины" if as_of_morning_dt else "Период анализа"
        md_lines.append(f"**{label}:** {prev_dt} -> {data_report_dt}")
    else:
        label = "Дата данных витрины" if as_of_morning_dt else "Дата анализа"
        md_lines.append(f"**{label}:** {data_report_dt}")

    prev_lim = _format_money(round(data.get('prev_limit_amt') or 0, 2))
    curr_lim = _format_money(round(data.get('limit_amt') or 0, 2))
    delta_lim = _format_money(round(data.get('delta_limit_amt') or 0, 2))

    md_lines.append(
        f"**Лимит:** {prev_lim} -> {curr_lim} "
        f"(Разница: {delta_lim})"
    )

    # Перераспределения
    authors = data.get("redistribution_by_author", [])
    if authors:
        tbl = _authors_table(authors)
        if tbl:
            md_lines.append(f"\n{_redistribution_heading(authors)}")
            md_lines.append(tbl)

    # Сделки
    deals = data.get("deals_analysis", {})
    if deals and deals.get("status") == "analyzed":
        _deals_analysis_block(deals, md_lines)
        _incorrect_deals_block(deals, md_lines)

    # Дисконтирование
    discounting = data.get("discounting_analysis", {})
    if discounting and discounting.get("status") == "analyzed":
        _discounting_block(discounting, md_lines)

    # Сделки с нарушением условий
    non_compliance = data.get("deals_analysis", {}).get("non_compliance_deals", [])
    if non_compliance:
        _non_compliance_block(non_compliance, md_lines)

    # Пропавшие сделки (отмененные)
    disappeared = data.get("disappeared_deals_analysis", [])
    if disappeared:
        _disappeared_deals_block(disappeared, md_lines)

    # Активация опции пополнения
    top_up = data.get("top_up_option_analysis", {})
    if top_up and top_up.get("status") == "analyzed":
        _top_up_option_block(top_up, md_lines)

    return "\n".join(md_lines)


def generate_all_kpk_report_markdown(data: Dict[str, Any]) -> str:
    """Преобразует JSON мульти-отчета в готовую строку Markdown."""
    if not data or data.get("mode") != "all":
        return ERROR_TEXT

    if data.get("negative_count", 0) == 0:
        as_of_morning_dt = data.get("as_of_morning_dt")
        if as_of_morning_dt:
            return (
                f"На утро {as_of_morning_dt} нет подразделений с отрицательным лимитом или отрицательным приростом."
            )
        return "На выбранную дату нет подразделений с отрицательным лимитом или отрицательным приростом."

    try:
        return _generate_all_kpk_report_markdown_impl(data)
    except Exception as e:
        _LOGGER.error("generate_all_kpk_report_failed. error=%s", e)
        return ERROR_TEXT


def _generate_all_kpk_report_markdown_impl(data: Dict[str, Any]) -> str:
    md_lines = []
    all_rows = data.get("rows", [])
    max_kpk = settings.max_all_kpk_rows
    for row in all_rows[:max_kpk]:
        md_lines.append(f"---\n### КПК: {row.get('division_cd')}")
        if row.get("anomaly_type") == "anomalous_positive":
            md_lines.append("** Значительное увеличение лимита **")

        prev_lim = _format_money(np.round(row.get('prev_limit_amt') or 0, 2))
        curr_lim = _format_money(np.round(row.get('limit_amt') or 0, 2))
        delta_lim = _format_money(np.round(row.get('delta_limit_amt') or 0, 2))

        md_lines.append(
            f"**Лимит:** {prev_lim} → {curr_lim} "
            f"(Разница: {delta_lim})"
        )

        authors = row.get("redistribution_by_author", [])
        if authors:
            tbl = _authors_table(authors)
            if tbl:
                md_lines.append(f"\n{_redistribution_heading(authors)}")
                md_lines.append(tbl)

        deals = row.get("deals_analysis", {})
        if deals and deals.get("status") == "analyzed":
            _deals_analysis_block(deals, md_lines)
            _incorrect_deals_block(deals, md_lines)

        discounting = row.get("discounting_analysis", {})
        if discounting and discounting.get("status") == "analyzed":
            _discounting_block(discounting, md_lines)

        non_compliance = row.get("deals_analysis", {}).get("non_compliance_deals", [])
        if non_compliance:
            _non_compliance_block(non_compliance, md_lines)

        disappeared = row.get("disappeared_deals_analysis", [])
        if disappeared:
            _disappeared_deals_block(disappeared, md_lines)

        top_up = row.get("top_up_option_analysis", {})
        if top_up and top_up.get("status") == "analyzed":
            _top_up_option_block(top_up, md_lines)

        md_lines.append("") # Пустая строка между КПК

    extra_kpk = len(all_rows) - max_kpk
    if extra_kpk > 0:
        md_lines.append(f"\n*...и еще {extra_kpk} КПК (не показаны).*")

    return "\n".join(md_lines)


def generate_find_deal_markdown(data: Dict[str, Any]) -> str:
    """Форматирует результат поиска сделки по сумме влияния."""
    if not data:
        return ERROR_TEXT

    deals = data.get("deals", [])
    if not deals:
        return (
            "Сделки с указанным влиянием на лимит не найдены.\n\n"
            f"{_limit_support_note()}"
        )


    try:
        _FIND_COLS = {
            "internal_order_cd": "id сделки",
            "client_name": "Клиент",
            "product_cd": "Продукт",
            "source_system_cd": "Система котирования",
            "delta_limit_amt": "Влияние на лимит КПК",
            "inn": "ИНН",
            "value_dt": "Дата валютирования",
            "author_nm": "Автор",
        }

        df = pd.DataFrame(deals)
        df = _apply_md_display_dates(df)
        if "source_system_cd" in df.columns:
            df["source_system_cd"] = df["source_system_cd"].apply(_map_readable_name)

        available = [c for c in _FIND_COLS if c in df.columns]
        df = df[available]
        df.rename(columns={k: v for k, v in _FIND_COLS.items() if k in available}, inplace=True)
        _format_numeric_cols(df)

        md_lines = ["### Найденные сделки:", _render_table(df)]
        return "\n".join(md_lines)
    except Exception as e:
        _LOGGER.error("generate_find_deal_markdown_failed. error=%s", e)
        return ERROR_TEXT


def generate_investigate_deal_markdown(data: Dict[str, Any]) -> str:
    """Форматирует результат анализа сделки (investigate_deal)."""
    if not data:
        return ERROR_TEXT

    inn = data.get("inn_num", "Н/Д")
    client_name = str(data.get("client_name") or "").strip()
    verdict = data.get("verdict", "not_found")

    try:
        md_lines = [
            f"#### Поиск сделки клиента {client_name} (ИНН: {inn})"
            if client_name
            else f"#### Поиск сделки (ИНН: {inn})"
        ]

        log_findings = data.get("log_findings", [])
        if log_findings:
            md_lines.append("\n##### Статусы в расчетах:")
            _LOG_COLS = {
                "internal_order_cd": "id сделки",
                "status_cd": "Статус",
                "calculation_dttm": "Дата расчета",
                "impact_end_note": "Прекращение влияния",
            }
            df = pd.DataFrame(log_findings)

            if "status_cd" in df.columns:
                df["status_cd"] = df["status_cd"].apply(_map_readable_name)

            available = [c for c in _LOG_COLS if c in df.columns]
            df = df[available].rename(columns={k: v for k, v in _LOG_COLS.items() if k in available})
            _format_numeric_cols(df)
            md_lines.append(_render_table(df))
            md_lines.append("\n##### Пояснение по статусам:")
            seen_explanations = []
            for finding in log_findings:
                explanation = _status_limit_explanation(finding.get("status_cd"))
                if explanation and explanation not in seen_explanations:
                    seen_explanations.append(explanation)
            for explanation in seen_explanations:
                md_lines.append(f"* {explanation}")

        deal_matches = data.get("deal_matches", [])
        if deal_matches:
            match_titles = {
                "found_in_deals": "\n#### Сделка найдена в расчете лимита:",
                "stopped_by_status": "\n#### Последняя доступная запись по сделке в витрине:",
                "pending_impact": "\n#### Найденная запись по сделке:",
                "not_found": "\n#### Поиск в витрине сделок:",
            }
            md_lines.append(match_titles.get(verdict, "\n#### Поиск в витрине сделок:"))
            _MATCH_COLS = {
                "internal_order_cd": "id сделки",
                "client_name": "Клиент",
                "division_cd": "код КПК",
                "product_cd": "Продукт",
                "source_system_cd": "Система котирования",
                "delta_limit_amt": "Влияние на лимит КПК",
                "upload_dt": "Дата последней записи в витрине",
                "deal_status_explanation": "Тип/Статус",
            }
            df = pd.DataFrame(deal_matches)
            df = _apply_md_display_dates(df)

            if "source_system_cd" in df.columns:
                df["source_system_cd"] = df["source_system_cd"].apply(_map_readable_name)
            if "deal_status_explanation" in df.columns:
                df["deal_status_explanation"] = df["deal_status_explanation"].apply(_map_readable_name)

            available = [c for c in _MATCH_COLS if c in df.columns]
            df = df[available].rename(columns={k: v for k, v in _MATCH_COLS.items() if k in available})
            _format_numeric_cols(df)
            md_lines.append(_render_table(df))
        elif verdict == "stopped_by_status":
            md_lines.append("\n#### Актуальная запись в витрине сделок:")
            md_lines.append(
                "*На текущую дату запись по указанным параметрам не найдена в витрине; "
                "причина отсутствия влияния определена по логам расчета.*"
            )
        elif verdict == "not_found":
            md_lines.append("\n#### Поиск в витрине сделок:")
            md_lines.append("*Расчет по указанным параметрам не найден в витрине сделок.*")

        pending = data.get("pending_explanation")
        if pending:
            for eng, rus in _HUMAN_READABLE_MAPPING.items():
                pending = pending.replace(f"(статус: {eng})", f"(статус: {rus})")
            md_lines.append(f"\n#### Причина отсутствия влияния:\n{pending}")

        return "\n".join(md_lines)
    except Exception as e:
        _LOGGER.error("generate_investigate_deal_failed. error=%s", e)
        return ERROR_TEXT


_CLIENT_HISTORY_COLS = {
    "internal_order_cd": "id сделки",
    "client_name": "Клиент",
    "product_cd": "Продукт",
    "source_system_cd": "Система котирования",
    "delta_limit_amt": "Влияние на лимит КПК",
    "value_dt": "Дата валютирования",
    "deal_dt": "Дата договора",
    "upload_dt": "Дата отражения в системе",
    "last_log_status": "Итоговый статус",
    "last_log_date": "Дата последнего действия по сделке",
}


def generate_client_history_markdown(data: Dict[str, Any]) -> str:
    """Форматирует историю сделок клиента по КПК."""
    if not data:
        return ERROR_TEXT

    inn = data.get("inn_num", "Н/Д")
    div = data.get("division_cd", "Н/Д")
    client_name = str(data.get("client_name") or "").strip()
    deals = data.get("deals", [])

    try:
        date = data.get("date", "Н/Д")
        date_from = data.get("date_from")
        date_to = data.get("date_to")
        days = data.get("lookback_days", 7)
        period_line = (
            f"**Период:** {date_from} → {date_to}"
            if date_from and date_to
            else f"**Период:** последние {days} дней (до {date})"
        )

        md_lines = [
            (
                f"### История сделок клиента {client_name} (ИНН: {inn}) по КПК {div}"
                if client_name
                else f"### История сделок клиента (ИНН: {inn}) по КПК {div}"
            ),
            period_line,
        ]

        pos_impact = data.get('total_positive', 0)
        neg_impact = data.get('total_negative', 0)

        if pos_impact != 0:
            md_lines.append(f"**Суммарное положительное влияние:** {_format_money(pos_impact)}")
        if neg_impact != 0:
            md_lines.append(f"**Суммарное отрицательное влияние:** {_format_money(neg_impact)}")
        md_lines.append("")

        if not deals:
            md_lines.append("*Сделок за указанный период не найдено.*")
            return "\n".join(md_lines)

        df = pd.DataFrame(deals)
        df = _apply_md_display_dates(df)
        if "source_system_cd" in df.columns:
            df["source_system_cd"] = df["source_system_cd"].apply(_map_readable_name)
        if "last_log_status" in df.columns:
            df["_raw_last_log_status"] = df["last_log_status"]
            df["last_log_status"] = df["last_log_status"].apply(_map_readable_name)
            df["last_log_status"] = df.apply(_resolve_current_deal_status, axis=1)
        if "last_log_date" in df.columns:
            df["last_log_date"] = df.apply(_resolve_last_action_date, axis=1)

        available = [c for c in _CLIENT_HISTORY_COLS if c in df.columns]
        df = df[available].rename(columns={k: v for k, v in _CLIENT_HISTORY_COLS.items() if k in available})
        _format_numeric_cols(df)
        md_lines.append(_render_table(df))

        deals_with_logs = [
            d for d in deals
            if d.get("last_log_status") and _status_limit_explanation(d.get("last_log_status"))
        ]
        if deals_with_logs:
            md_lines.append("\n**Пояснение по статусам из данных по расчетам:**")
            for d in deals_with_logs:
                status_raw = d.get("last_log_status")
                order = d.get("internal_order_cd", "Н/Д")
                explanation = _status_limit_explanation(status_raw)
                if explanation:
                    md_lines.append(f"* id сделки {order}: {explanation}")

        return "\n".join(md_lines)
    except Exception as e:
        _LOGGER.error("generate_client_history_failed. error=%s", e)
        return ERROR_TEXT
