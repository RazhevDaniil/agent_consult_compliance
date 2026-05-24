from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from io import BytesIO
import os
from typing import Any, Iterable
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

import numpy as np
import pandas as pd


EXCEL_MIME_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

_AUTO_SOURCES = {
    "urn:sbrfsystems:99-ufs-depositsweb",
    "urn:sbrfsystems:99-ufs-sct",
}
_KM_SOURCE = "urn:sbrfsystems:99-ufs-sr"


def build_incorrect_deals_report_filename(report_dt: str | None) -> str:
    suffix = _fmt_date(report_dt, "%Y%m%d", default="unknown_date")
    return f"new_deals_report_{suffix}.xlsx"


def build_incorrect_deals_report_xlsx(payload: dict[str, Any] | None) -> bytes:
    rows = build_incorrect_deals_report_rows(payload)
    report_dt = ((payload or {}).get("report_dt") if isinstance(payload, dict) else None)
    limit_caption = _fmt_date(report_dt, "%d.%m.%y", default="")
    headers = [
        "Дата отчета",
        "КПК",
        f"Лимит на {limit_caption}" if limit_caption else "Лимит",
        "Причина",
        "Детали",
        "Действия ЦК",
        "Сумма",
    ]

    return _build_simple_xlsx(
        sheet_name="NewDeals",
        headers=headers,
        rows=rows,
        column_widths=[26, 10, 19, 34, 129, 28, 14],
    )


def build_incorrect_deals_report_rows(payload: dict[str, Any] | None) -> list[list[str]]:
    if not isinstance(payload, dict):
        return []

    report_dt = payload.get("report_dt")
    report_dt_label = _fmt_date(report_dt, "%d.%m.%y")
    raw_rows = payload.get("rows") or []
    report_rows: list[list[str]] = []

    for raw_row in raw_rows:
        if not isinstance(raw_row, dict):
            continue

        limit_value = _safe_float(raw_row.get("limit_amt"))

        deals_analysis = raw_row.get("deals_analysis") or {}
        deals = deals_analysis.get("all_new_deals") or deals_analysis.get("incorrect_deals") or []
        top_up_analysis = raw_row.get("top_up_option_analysis") or {}
        top_up_deals = top_up_analysis.get("top_up_deals") or []
        incorrect_original_total = _sum_incorrect_original_delta(deals_analysis)
        action, amount = _build_compensation_action(limit_value, incorrect_original_total)
        if not action:
            continue

        annotated = [_annotate_deal(deal) for deal in deals if isinstance(deal, dict)]
        annotated.extend(_annotate_top_up_deal(deal) for deal in top_up_deals if isinstance(deal, dict))
        annotated = [deal for deal in annotated if deal]
        if not annotated:
            continue

        reason = _aggregate_reason(annotated)
        details = _build_details_text(annotated)
        limit_amt = _format_money(limit_value, digits=0)

        report_rows.append([
            report_dt_label,
            str(raw_row.get("division_cd") or ""),
            limit_amt,
            reason,
            details,
            action,
            amount,
        ])

    report_rows.sort(key=lambda row: (0 if row[5] else 1, row[1]))
    return report_rows


def _sum_incorrect_original_delta(deals_analysis: dict[str, Any]) -> float:
    incorrect_deals = deals_analysis.get("incorrect_deals") or []
    if not incorrect_deals:
        incorrect_deals = [
            deal
            for deal in (deals_analysis.get("all_new_deals") or [])
            if isinstance(deal, dict) and str(deal.get("deal_status") or "").strip().lower() == "incorrect"
        ]
    return sum(
        _safe_float(deal.get("original_delta_limit_amt"))
        for deal in incorrect_deals
        if isinstance(deal, dict)
    )


def _build_compensation_action(limit_value: float, incorrect_original_total: float) -> tuple[str, str]:
    if incorrect_original_total != 0:
        limit_after_incorrect = limit_value - incorrect_original_total
        if limit_after_incorrect < 0:
            return (
                "Компенсировать некорректные сделки и довести лимит до нуля",
                _format_money(abs(limit_value), digits=0),
            )
        amount = _format_money(abs(incorrect_original_total), digits=0)
        return "Компенсировать некорректные сделки", amount
    if limit_value < 0:
        return "Запрос компенсации до нуля", ""
    return "", ""


def _annotate_deal(deal: dict[str, Any]) -> dict[str, Any]:
    source = str(deal.get("source_system_cd") or "").strip().lower()
    product = str(deal.get("product_cd") or "").strip().upper()
    value_dt = _to_timestamp(deal.get("value_dt"))
    upload_dt = _to_timestamp(deal.get("first_upload_dt") or deal.get("upload_dt"))
    deal_dt = _to_timestamp(deal.get("deal_dt"))
    deal_status = str(deal.get("deal_status") or "").strip().lower()
    delta_amt = _safe_float(deal.get("delta_limit_amt"))
    workdays_diff = _get_workdays_diff(value_dt, upload_dt)
    expected_diff = _expected_workdays_diff(source, product)
    reason_code, reason_label = _infer_reason_from_status(
        deal_status=deal_status,
        source=source,
        product=product,
        value_dt=value_dt,
        upload_dt=upload_dt,
        expected_diff=expected_diff,
        workdays_diff=workdays_diff,
    )

    return {
        "inn": str(deal.get("inn") or "").strip(),
        "delta_amt": delta_amt,
        "product": product,
        "source": source,
        "value_dt": value_dt,
        "upload_dt": upload_dt,
        "deal_dt": deal_dt,
        "deal_status": deal_status,
        "reason_code": reason_code,
        "reason_label": reason_label,
        "workdays_diff": workdays_diff,
        "expected_diff": expected_diff,
        "internal_order_cd": str(deal.get("internal_order_cd") or "").strip(),
        "prev_division": str(deal.get("prev_division") or "").strip(),
    }


def _aggregate_reason(deals: list[dict[str, Any]]) -> str:
    reason_counts = Counter(deal["reason_label"] for deal in deals)
    if len(reason_counts) == 1:
        return deals[0]["reason_label"]
    dominant_reason = max(
        reason_counts.items(),
        key=lambda item: (
            item[1],
            sum(abs(deal["delta_amt"]) for deal in deals if deal["reason_label"] == item[0]),
            item[0],
        ),
    )[0]
    return f"{dominant_reason} и другие отклонения"


def _build_details_text(deals: list[dict[str, Any]]) -> str:
    deals = sorted(
        deals,
        key=lambda item: (abs(item["delta_amt"]), item["inn"], item["internal_order_cd"]),
        reverse=True,
    )
    total_impact = sum(deal["delta_amt"] for deal in deals)
    reason_counts = Counter(deal["reason_label"] for deal in deals)
    reason_label, reason_count = reason_counts.most_common(1)[0]
    unique_inns = [inn for inn in dict.fromkeys(deal["inn"] for deal in deals if deal["inn"])]

    if len(unique_inns) == 1:
        client_text = f"клиенту ИНН {unique_inns[0]}"
    elif unique_inns:
        shown = ", ".join(unique_inns[:3])
        suffix = " и другим клиентам" if len(unique_inns) > 3 else ""
        client_text = f"клиентам ИНН {shown}{suffix}"
    else:
        client_text = "сделкам без ИНН"

    if len(reason_counts) == 1:
        intro = f"Найдено {len(deals)} событий по {client_text}: {reason_label.lower()}."
    else:
        intro = (
            f"Найдено {len(deals)} событий по {client_text}. "
            f"Основная причина: {reason_label.lower()} ({reason_count} шт.)."
        )

    impact_line = f"Суммарное влияние: {_format_money(total_impact, digits=0)} руб."
    detail_lines = []
    for deal in deals[:5]:
        detail_lines.append(_deal_line(deal))

    extra = len(deals) - len(detail_lines)
    if extra > 0:
        detail_lines.append(f"Еще сделок: {extra}.")

    return "\n".join([intro, impact_line, *detail_lines])


def _deal_line(deal: dict[str, Any]) -> str:
    inn = deal["inn"] or "н/д"
    value_dt = _fmt_dt_short(deal["value_dt"])
    upload_dt = _fmt_dt_short(deal["upload_dt"])
    deal_dt = _fmt_dt_short(deal["deal_dt"])
    impact = _format_money(deal["delta_amt"], digits=0)
    order_cd = deal.get("internal_order_cd") or "н/д"
    prev_division = str(deal.get("prev_division") or "").strip()
    transfer_note = f"; переведена из КПК {prev_division}" if prev_division else ""
    source = deal.get("source") or "н/д"
    source_note = f"source_system_cd {source}, "

    if deal["reason_code"] == "km_cotirovka":
        timing = "отразилась в дату валютирования" if deal["product"] == "NSO" else (
            "отразилась на следующий рабочий день после даты валютирования"
        )
        return (
            f"Сделка {order_cd}, ИНН {inn}: {source_note}котировка менеджера, value_dt {value_dt}, "
            f"первое отражение в витрине {upload_dt}{transfer_note}; {timing}, влияние {impact} руб."
        )
    if deal["reason_code"] == "future_value_dt":
        return (
            f"Сделка {order_cd}, ИНН {inn}: {source_note}дата валютирования {value_dt} позже первого "
            f"отражения в витрине {upload_dt}{transfer_note}; влияние {impact} руб."
        )
    if deal["reason_code"] == "after_operday":
        return (
            f"Сделка {order_cd}, ИНН {inn}: {source_note}сделка от {deal_dt} заключена после опер. дня "
            f"и отразилась в витрине {upload_dt}{transfer_note}, влияние {impact} руб."
        )
    if deal["reason_code"] == "group_deal":
        return (
            f"Сделка {order_cd}, ИНН {inn}: {source_note}групповая сделка, value_dt {value_dt}, "
            f"первое отражение в витрине {upload_dt}{transfer_note}; "
            f"отразилась на утро 5-го рабочего дня после даты валютирования, "
            f"влияние {impact} руб."
        )
    if deal["reason_code"] == "auto_cotirovanie":
        return (
            f"Сделка {order_cd}, ИНН {inn}: {source_note}автокотировка, value_dt {value_dt}, "
            f"первое отражение в витрине {upload_dt}{transfer_note}; "
            f"отразилась на утро 5-го рабочего дня после даты валютирования, "
            f"влияние {impact} руб."
        )
    if deal["reason_code"] == "top_up_activation":
        prev_impact = _format_money(deal.get("delta_amt_prev"), digits=0)
        current_impact = _format_money(deal.get("delta_amt_current"), digits=0)
        return (
            f"Сделка {order_cd}, ИНН {inn}: {source_note}активирована опция пополнения, value_dt {value_dt}; "
            f"влияние сделки изменилось с {prev_impact} до {current_impact} руб., "
            f"дополнительное влияние {impact} руб."
        )
    if deal["reason_code"] == "late_reflection":
        return (
            f"Сделка {order_cd}, ИНН {inn}: {source_note}сделка отразилась позже ожидаемого срока, "
            f"value_dt {value_dt}, первое отражение в витрине {upload_dt}{transfer_note}, "
            f"влияние {impact} руб."
        )
    if deal["reason_code"] == "early_reflection":
        return (
            f"Сделка {order_cd}, ИНН {inn}: {source_note}сделка отразилась раньше ожидаемого срока, "
            f"value_dt {value_dt}, первое отражение в витрине {upload_dt}{transfer_note}, "
            f"влияние {impact} руб."
        )
    if deal["reason_code"] == "incorrect":
        return (
            f"Сделка {order_cd}, ИНН {inn}: {source_note}сделка требует ручной проверки, "
            f"value_dt {value_dt}, первое отражение в витрине {upload_dt}{transfer_note}, "
            f"влияние {impact} руб."
        )
    if deal["reason_code"] in {"auto_cotirovanie", "group_deal"}:
        return (
            f"Сделка {order_cd}, ИНН {inn}: {source_note}value_dt {value_dt}, "
            f"первое отражение в витрине {upload_dt}{transfer_note}, "
            f"влияние {impact} руб."
        )
    if deal["reason_code"] == "missing_dates":
        return f"Сделка {order_cd}, ИНН {inn}: {source_note}не хватает дат для проверки, влияние {impact} руб."
    return (
        f"Сделка {order_cd}, ИНН {inn}: {source_note}value_dt {value_dt}, "
        f"первое отражение в витрине {upload_dt}{transfer_note}, "
        f"влияние {impact} руб."
    )


def _infer_reason_from_status(
        *,
        deal_status: str,
        source: str,
        product: str,
        value_dt: pd.Timestamp | None,
        upload_dt: pd.Timestamp | None,
        expected_diff: int | None,
        workdays_diff: int,
) -> tuple[str, str]:
    if deal_status == "km_cotirovka":
        return "km_cotirovka", "Котировка менеджера"
    if deal_status == "auto_cotirovka":
        return "auto_cotirovanie", "Автокотировка"
    if deal_status == "group_deal":
        return "group_deal", "Групповая сделка"
    return "incorrect", "Некорректная сделка"


def _infer_reason(
        *,
        source: str,
        product: str,
        value_dt: pd.Timestamp | None,
        upload_dt: pd.Timestamp | None,
        expected_diff: int | None,
        workdays_diff: int,
) -> tuple[str, str]:
    if value_dt is None or upload_dt is None:
        return "missing_dates", "Некорректные даты"
    if value_dt.normalize() > upload_dt.normalize():
        return "future_value_dt", "Дата валютирования позже отражения"
    if source == _KM_SOURCE and expected_diff is not None and workdays_diff > expected_diff:
        return "manual_review_km_delay", "Некорректная сделка"
    if source == _KM_SOURCE:
        return "after_operday", "Сделка заключена после опер. дня"
    if source == "urn:sbrfsystems:99-ufs-sct":
        return "group_deal", "Групповая сделка"
    if source in _AUTO_SOURCES:
        return "auto_cotirovanie", "Автокотирование"
    if expected_diff is not None and workdays_diff > expected_diff:
        return "late_reflection", "Позднее отражение сделки"
    if expected_diff is not None and workdays_diff < expected_diff:
        return "early_reflection", "Раннее отражение сделки"
    return "incorrect", "Некорректная сделка"


def _annotate_top_up_deal(deal: dict[str, Any]) -> dict[str, Any]:
    delta_change = _safe_float(deal.get("delta_limit_change"))
    if delta_change == 0:
        return {}

    return {
        "inn": str(deal.get("inn") or "").strip(),
        "delta_amt": delta_change,
        "delta_amt_prev": _safe_float(deal.get("delta_limit_amt_prev")),
        "delta_amt_current": _safe_float(deal.get("delta_limit_amt")),
        "product": str(deal.get("product_cd") or "").strip().upper(),
        "source": "top_up_option",
        "value_dt": _to_timestamp(deal.get("value_dt")),
        "upload_dt": None,
        "deal_dt": _to_timestamp(deal.get("deal_dt")),
        "deal_status": "top_up_activation",
        "reason_code": "top_up_activation",
        "reason_label": "Активация опции пополнения",
        "workdays_diff": 0,
        "expected_diff": None,
        "internal_order_cd": str(deal.get("internal_order_cd") or "").strip(),
    }


def _expected_workdays_diff(source: str, product: str) -> int | None:
    if source == _KM_SOURCE:
        if product == "DEPO":
            return 1
        if product == "NSO":
            return 0
    if source in _AUTO_SOURCES:
        return 5
    return None


def _get_workdays_diff(value_dt: pd.Timestamp | None, upload_dt: pd.Timestamp | None) -> int:
    if value_dt is None or upload_dt is None:
        return -999

    v_wd = value_dt.weekday()
    u_wd = upload_dt.weekday()
    days_diff = (upload_dt.date() - value_dt.date()).days
    if days_diff > 0:
        if v_wd == 5 and u_wd == 0:
            return 0
        if v_wd in [0, 1, 2] and u_wd == 0:
            return 5
        if v_wd == 3 and u_wd == 1:
            return 5
        if v_wd == 4 and u_wd == 2:
            return 5

    try:
        holidays = []
        raw = os.getenv("KPK_HOLIDAYS", "2026-05-11")
        for token in raw.split(","):
            ts = pd.to_datetime(token.strip(), errors="coerce")
            if pd.notna(ts):
                holidays.append(ts.date().isoformat())
        return int(np.busday_count(value_dt.date(), upload_dt.date(), holidays=holidays))
    except Exception:
        return -999


def _safe_float(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, str):
        value = value.replace(" ", "").replace(",", ".").strip()
        if value.lower() in {"", "none", "nan", "null", "n/a"}:
            return 0.0
    try:
        val = float(value)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if not np.isfinite(val) else val


def _to_timestamp(value: Any) -> pd.Timestamp | None:
    if value in (None, "", "Н/Д"):
        return None
    ts = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(ts) else ts.normalize()


def _fmt_dt_short(value: pd.Timestamp | None) -> str:
    return "" if value is None else value.strftime("%d.%m.%y")


def _fmt_date(value: Any, fmt: str, default: str = "") -> str:
    ts = _to_timestamp(value)
    return default if ts is None else ts.strftime(fmt)


def _format_money(value: Any, digits: int = 0) -> str:
    amount = _safe_float(value)
    rounded = round(amount, digits)
    abs_amount = abs(rounded)
    if digits <= 0:
        body = f"{int(round(abs_amount)):,}".replace(",", " ")
    else:
        body = f"{abs_amount:,.{digits}f}".replace(",", " ").replace(".", ",")
    sign = "- " if rounded < 0 else ""
    return f"{sign}{body}"


def _build_simple_xlsx(
        *,
        sheet_name: str,
        headers: list[str],
        rows: Iterable[list[str]],
        column_widths: list[int],
) -> bytes:
    rows = list(rows)
    created = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    data = BytesIO()

    with ZipFile(data, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _content_types_xml())
        archive.writestr("_rels/.rels", _root_rels_xml())
        archive.writestr("docProps/app.xml", _app_props_xml(sheet_name))
        archive.writestr("docProps/core.xml", _core_props_xml(created))
        archive.writestr("xl/workbook.xml", _workbook_xml(sheet_name))
        archive.writestr("xl/_rels/workbook.xml.rels", _workbook_rels_xml())
        archive.writestr("xl/styles.xml", _styles_xml())
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            _worksheet_xml(headers=headers, rows=rows, column_widths=column_widths),
        )

    return data.getvalue()


def _worksheet_xml(*, headers: list[str], rows: list[list[str]], column_widths: list[int]) -> str:
    all_rows = [headers, *rows]
    max_row = len(all_rows)
    max_col = len(headers)
    dimension = f"A1:{_column_name(max_col)}{max_row}"

    cols_xml = "".join(
        f'<col min="{idx}" max="{idx}" width="{width}" customWidth="1"/>'
        for idx, width in enumerate(column_widths, start=1)
    )

    sheet_rows = []
    for row_idx, row in enumerate(all_rows, start=1):
        style = 1 if row_idx == 1 else 2
        cells = []
        for col_idx, value in enumerate(row, start=1):
            cell_ref = f"{_column_name(col_idx)}{row_idx}"
            cells.append(_inline_string_cell(cell_ref, value, style))
        sheet_rows.append(f'<row r="{row_idx}">{"".join(cells)}</row>')

    auto_filter_ref = f"A1:{_column_name(max_col)}{max_row}"

    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetPr><outlinePr summaryBelow="1" summaryRight="1"/><pageSetUpPr/></sheetPr>'
        f'<dimension ref="{dimension}"/>'
        '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        '<selection pane="bottomLeft" activeCell="A2" sqref="A2"/></sheetView></sheetViews>'
        '<sheetFormatPr baseColWidth="8" defaultRowHeight="15"/>'
        f"<cols>{cols_xml}</cols>"
        f"<sheetData>{''.join(sheet_rows)}</sheetData>"
        f'<autoFilter ref="{auto_filter_ref}"/>'
        '<pageMargins left="0.75" right="0.75" top="1" bottom="1" header="0.5" footer="0.5"/>'
        "</worksheet>"
    )


def _inline_string_cell(cell_ref: str, value: Any, style: int) -> str:
    text = "" if value is None else str(value)
    space_attr = ' xml:space="preserve"' if ("\n" in text or text.startswith(" ") or text.endswith(" ")) else ""
    return (
        f'<c r="{cell_ref}" s="{style}" t="inlineStr">'
        f"<is><t{space_attr}>{escape(text)}</t></is>"
        "</c>"
    )


def _column_name(index: int) -> str:
    letters = []
    while index > 0:
        index, rem = divmod(index - 1, 26)
        letters.append(chr(65 + rem))
    return "".join(reversed(letters))


def _content_types_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/docProps/app.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
        '<Override PartName="/docProps/core.xml" '
        'ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        "</Types>"
    )


def _root_rels_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/>'
        '<Relationship Id="rId2" '
        'Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" '
        'Target="docProps/core.xml"/>'
        '<Relationship Id="rId3" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" '
        'Target="docProps/app.xml"/>'
        "</Relationships>"
    )


def _app_props_xml(sheet_name: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
        'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
        '<Application>Codex</Application>'
        '<HeadingPairs><vt:vector size="2" baseType="variant">'
        '<vt:variant><vt:lpstr>Worksheets</vt:lpstr></vt:variant>'
        '<vt:variant><vt:i4>1</vt:i4></vt:variant>'
        '</vt:vector></HeadingPairs>'
        '<TitlesOfParts><vt:vector size="1" baseType="lpstr">'
        f"<vt:lpstr>{escape(sheet_name)}</vt:lpstr>"
        "</vt:vector></TitlesOfParts>"
        "</Properties>"
    )


def _core_props_xml(created: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:dcmitype="http://purl.org/dc/dcmitype/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        "<dc:creator>Codex</dc:creator>"
        "<cp:lastModifiedBy>Codex</cp:lastModifiedBy>"
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{created}</dcterms:created>'
        f'<dcterms:modified xsi:type="dcterms:W3CDTF">{created}</dcterms:modified>'
        "</cp:coreProperties>"
    )


def _workbook_xml(sheet_name: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<workbookPr/><bookViews><workbookView activeTab="0"/></bookViews>'
        f'<sheets><sheet name="{escape(sheet_name)}" sheetId="1" r:id="rId1"/></sheets>'
        '<calcPr calcId="124519" fullCalcOnLoad="1"/>'
        "</workbook>"
    )


def _workbook_rels_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/>'
        "</Relationships>"
    )


def _styles_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="2">'
        '<font><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><sz val="11"/><name val="Calibri"/></font>'
        '</fonts>'
        '<fills count="2">'
        '<fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill>'
        '</fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="3">'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1" '
        'applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1">'
        '<alignment vertical="top" wrapText="1"/></xf>'
        '</cellXfs>'
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        "</styleSheet>"
    )
