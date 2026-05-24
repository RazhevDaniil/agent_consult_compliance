import os
import asyncio
import logging
import uuid
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, List
from pydantic import BaseModel, Field
from fastapi.responses import JSONResponse, Response
from contextlib import asynccontextmanager, suppress
from fastapi import FastAPI, HTTPException, Query, Body, Header, Depends, Request

from .trino.trino_client import run_trino
from .auth.auth_middleware import REQUIRED_ROLES, get_roles_from_security
from .utils.logger_config import configure_logging
from .models.models import (
    UnifiedRequest, ReportRequest, PipelineResponse, GetRateRequest,
    GetRateResponse, KpkRequest, InvestigateDealRequest, ClientHistoryRequest
)

from .agent_consult_api.calc_params_parser import DealSelector
from .agent_consult_api.engines import PricingEngine, LimitEngine
from .agent_consult_api.tech_f import _row_from_prefetched, _decimal_to_float
from .agent_consult_api.data_extractors import DealExtractor, CalcExtractor, IndicatorsExtractor, ReportExtractor
from .agent_consult_api.incorrect_deals_report import (
    EXCEL_MIME_TYPE,
    build_incorrect_deals_report_filename,
    build_incorrect_deals_report_xlsx,
)

from .agent_consult_api.kpk_limit_analysis import (
    analyze_single_kpk, analyze_negative_report, find_deal_by_amount,
    investigate_deal, analyze_client_history
)
from .agent_consult_api.kpk_data_loaders import (
    load_for_limit_change, load_for_negative_report,
    load_for_find_deal, load_for_investigate_deal, load_for_client_history
)

from .agent_treasury_api.data_processes.db import engine
from .agent_treasury_api.rate_calculator import RateCalculator
from .agent_treasury_api.data_processes.data_loader import DataLoader
from .agent_treasury_api.data_processes.data_processor import DataProcessor
from .agent_treasury_api.data_processes.hist_rate_getter import HistRateGetter
from .agent_treasury_api.data_processes.model_rate_getter import ModelRateGetter
from .agent_treasury_api.client_processes.client_service import ClientDataService


PREFIX_TAIL = os.getenv('PREFIX_TAIL', '')
TRACE_HEADER_NAME = "x-trace-id"

DATA_PROCESSOR_TZ = ZoneInfo(os.getenv("DATA_PROCESSOR_TZ", "Europe/Moscow"))
DATA_PROCESSOR_HOUR = int(os.getenv("DATA_PROCESSOR_HOUR", "4"))
DATA_PROCESSOR_MINUTE = int(os.getenv("DATA_PROCESSOR_MINUTE", "00"))
DATA_PROCESSOR_LOCK = asyncio.Lock()

configure_logging()
log = logging.getLogger("agent-tools-service")


def _resolve_trace_id(value: str | None) -> tuple[str, bool, bool]:
    if not value:
        return str(uuid.uuid4()), True, False
    try:
        parsed = uuid.UUID(value.strip())
    except (AttributeError, ValueError):
        return str(uuid.uuid4()), False, True
    if parsed.version != 4:
        return str(uuid.uuid4()), False, True
    return str(parsed), False, False


def _enrich_deals_with_client_names(data: dict | None) -> None:
    if not isinstance(data, dict):
        return

    cache: dict[str, str] = {}

    def _attach_name(node) -> None:
        if isinstance(node, dict):
            inn = str(node.get("inn") or node.get("inn_num") or "").strip()
            if inn:
                if inn not in cache:
                    cache[inn] = ClientDataService(inn).get_client_info().name
                if cache[inn]:
                    node["client_name"] = cache[inn]

            for value in node.values():
                _attach_name(value)
        elif isinstance(node, list):
            for item in node:
                _attach_name(item)

    _attach_name(data)


def _safe_float(value, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, str):
        cleaned = value.replace(" ", "").replace(",", ".").strip()
        if cleaned.lower() in {"", "none", "nan", "null", "n/a"}:
            return default
        value = cleaned
    try:
        val = float(value)
    except (TypeError, ValueError):
        return default
    return default if not np.isfinite(val) else val


def _fmt_report_number(value, digits: int = 2) -> str:
    return f"{_safe_float(value):.{digits}f}"


def _df_shape(df) -> tuple[int, int] | None:
    if df is None or not hasattr(df, "shape"):
        return None
    return tuple(int(x) for x in df.shape)


def _kpk_result_summary(res) -> dict:
    if not isinstance(res, dict):
        return {"type": type(res).__name__}

    data = res.get("data") if isinstance(res.get("data"), dict) else {}
    deals = data.get("deals_analysis") if isinstance(data.get("deals_analysis"), dict) else {}
    discounting = data.get("discounting_analysis") if isinstance(data.get("discounting_analysis"), dict) else {}
    top_up = data.get("top_up_option_analysis") if isinstance(data.get("top_up_option_analysis"), dict) else {}

    return {
        "status": res.get("status"),
        "error": res.get("error"),
        "data_keys": sorted(data.keys()) if data else [],
        "mode": data.get("mode"),
        "division_cd": data.get("division_cd"),
        "report_dt": data.get("report_dt"),
        "prev_report_dt": data.get("prev_report_dt"),
        "limit_amt": data.get("limit_amt"),
        "prev_limit_amt": data.get("prev_limit_amt"),
        "delta_limit_amt": data.get("delta_limit_amt"),
        "deals_status": deals.get("status"),
        "deals_impact_total": deals.get("deals_impact_total"),
        "discounting_status": discounting.get("status"),
        "top_up_status": top_up.get("status"),
        "disappeared_count": len(data.get("disappeared_deals_analysis") or []),
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.trino_smoke_task = asyncio.create_task(_delayed_trino_smoke_test(0))
    app.state.data_processor_task = asyncio.create_task(_daily_data_processor_loop())

    try:
        yield
    finally:
        for task_name in ("trino_smoke_task", "data_processor_task"):
            task = getattr(app.state, task_name, None)
            if task and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task


app = FastAPI(
    title="Pricing and Limits Pipeline API",
    description="API for running pricing and limits calculations",
    version="1.0.0",
    lifespan=lifespan,
)


log.info("--- AGENT-TOOLS Service has just been initialized ---")


@app.middleware("http")
async def trace_context_middleware(request: Request, call_next):
    trace_id, missing, invalid = _resolve_trace_id(request.headers.get(TRACE_HEADER_NAME))
    request.state.trace_id = trace_id

    if missing or invalid:
        log.warning(
            "trace_id_generated trace_id=%s missing=%s invalid=%s path=%s",
            trace_id,
            missing,
            invalid,
            request.url.path,
        )
    else:
        log.info("request_received trace_id=%s path=%s", trace_id, request.url.path)

    response = await call_next(request)
    response.headers[TRACE_HEADER_NAME] = trace_id
    log.info(
        "request_finished trace_id=%s path=%s status_code=%s",
        trace_id,
        request.url.path,
        response.status_code,
    )
    return response

SECRETS_PROPS = Path("/vault/secrets/secrets.properties")

def require_roles(authorization: Optional[str] = Header(default=None, alias='Authorization')) -> bool:
    if not authorization:
        log.error("--- JWT as authorization in HEADER is missing or empty ---") # , "JWT token is missing ---")
        raise HTTPException(status_code=401, detail="JWT token is missing")
    log.info(f"--- query is authorized with JWT ---")

    roles = get_roles_from_security(authorization)

    if roles:
        log.info(f"--- Roles found in security response: {roles} ---")
        return roles

    if not roles:
        log.info("--- NO ROLES FOUND FOR CURRENT USER IN SECURITY RESPONSE ---")
        # roles = ['PALM_PSS_BUSINESS_SUPPORT_USER_DEPOSITS']
        raise HTTPException(status_code=403, detail="Roles not found in security response")

    if REQUIRED_ROLES and not any(r in roles for r in REQUIRED_ROLES):
        log.info("--- NO ROLES EQUAL TO REQUIRED ONES FOR CURRENT USER IN SECURITY RESPONSE ---")
        # roles = ['PALM_PSS_BUSINESS_SUPPORT_USER_DEPOSITS']
        raise HTTPException(status_code=403, detail="Not allowed for current user roles")


async def _wait_for_file(path: Path, timeout_sec: int = 90, poll_sec: float = 1.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout_sec
    while asyncio.get_running_loop().time() < deadline:
        try:
            if path.is_file() and path.stat().st_size > 0:
                return True
        except FileNotFoundError:
            pass
        await asyncio.sleep(poll_sec)
    return False

test_sql = {
    "query": """SELECT product_cd FROM custom_fin_palm_dataops.lb_deal_depo_fct LIMIT 1""",
    "schema": "custom_fin_palm_dataops",
    "catalog": "con_hadoop_foton"
}

async def _delayed_trino_smoke_test(delay_sec: int = 30) -> None:
    await asyncio.sleep(delay_sec)

    ok = await _wait_for_file(SECRETS_PROPS, timeout_sec=90, poll_sec=1)
    if not ok:
        log.error(f"--- Trino test skipped: secrets file not found: {SECRETS_PROPS} ---")
        return

    loop = asyncio.get_running_loop()
    try:
        resp = await loop.run_in_executor(
            None,
            lambda: run_trino(
                sql=test_sql["query"],
                catalog=test_sql["catalog"],
                schema=test_sql["schema"],
                is_test=True,
            ),
        )
        log.info(f"--- Trino connection passed successfully: {resp} ---")
    except Exception as e:
        log.exception(f"--- Trino connection Failed with an error {e} ---")


def _next_data_processor_run_at(now: datetime | None = None) -> datetime:
    now = now or datetime.now(DATA_PROCESSOR_TZ)
    next_run = now.replace(
        hour=DATA_PROCESSOR_HOUR,
        minute=DATA_PROCESSOR_MINUTE,
        second=0,
        microsecond=0,
    )
    if next_run <= now:
        next_run += timedelta(days=1)
    return next_run


async def _run_data_processor_once(reason: str) -> None:
    if DATA_PROCESSOR_LOCK.locked():
        log.warning(f"--- DataProcessor skipped: previous run is still active. reason={reason} ---")
        return

    async with DATA_PROCESSOR_LOCK:
        log.info(f"--- DataProcessor started. reason={reason} ---")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: DataProcessor(engine=engine))
        log.info(f"--- DataProcessor finished. reason={reason} ---")


async def _daily_data_processor_loop() -> None:
    while True:
        next_run = _next_data_processor_run_at()
        delay_sec = max(0.0, (next_run - datetime.now(DATA_PROCESSOR_TZ)).total_seconds())
        log.info(f"--- Next DataProcessor run scheduled at {next_run.isoformat()} ---")

        await asyncio.sleep(delay_sec)

        try:
            await _run_data_processor_once("daily_schedule")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception(f"--- DataProcessor failed: {e} ---")


@app.get("/health")
async def health_check():
    return {"status": "healthy", "service": "pricing-limits-api"}

@app.get("/ready")
async def readiness_check():
    return {"status": "ready", "service": "pricing-limits-api"}


@app.post(f"{PREFIX_TAIL}/api/get_rate", summary="Получить процентную ставку по условиям сделки", response_model=GetRateResponse)
async def get_rate(request: GetRateRequest):

    # 1. RateCalculator
    calc = RateCalculator(
        engine,
        inn=request.inn,
        ccy=request.ccy,
        product=request.product,
        term=request.term,
        vol=request.vol,
        rate_type=request.rate_type,
        basis=request.basis,
        optionality=request.optionality,
    )
    calc_rate = calc.rate()

    if request.ccy != "RUB":
        if calc_rate is None:
            raise HTTPException(status_code=422, detail="Не удалось рассчитать ставку по заданным параметрам")
        return GetRateResponse(rates=[("calc_rate", calc_rate)])

    fund_rate = calc.fund_rate()

    # 2. HistRateGetter - ставка по историческим сделкам клиента
    hist_getter = HistRateGetter(
        engine,
        inn=request.inn,
        ccy=request.ccy,
        term=request.term,
        vol=request.vol,
        fund_rate=fund_rate,
    )
    hist_rate = hist_getter.rate()

    # 3. ModelRateGetter ставка от котировщика
    limit_rate = DataLoader.lim_rate_rub(
        engine,
        product=request.product,
        optionality=request.optionality,
        volume=request.vol,
        term=request.term,
    )
    max_rate = calc.nul_rate()
    model_getter = ModelRateGetter(
        term=request.term,
        ccy=request.ccy,
        inn=request.inn,
        amount=request.vol,
        product=request.product,
        limit_rate=limit_rate,
        max_rate=max_rate,
        fund_rate=fund_rate,
    )
    model_rate = model_getter.rate()

    # Собираем доступные ставки, сортируем по возрастанию
    rate_sources = [
        ("calc_rate", calc_rate),
        ("hist_rate", hist_rate),
        ("model_rate", model_rate),
    ]
    rates = [(k, v) for k, v in rate_sources if v is not None]
    if not rates:
        raise HTTPException(status_code=422, detail="Не удалось рассчитать ставку по заданным параметрам")

    rates.sort(key=lambda x: x[1])
    return GetRateResponse(rates=rates)


# def _ensure_required_pricing(sel: DealSelector) -> Optional[str]:
#     """
#     Правило:
#       если есть deal_id — достаточно его одного (остальные поля, включая interest_rate, не требуем);
#       если deal_id НЕТ — обязательны ВСЕ поля: inn, deal_dt, currency, product, amount, interest_rate.
#     """
#     if sel.deal_id:
#         return None
#     misses = []
#     required = {
#         "inn": bool(sel.inn),
#         "product": bool(sel.product),
#         "currency": bool(sel.currency),
#         "amount": bool(sel.amount),
#         "deal_dt": bool(sel.deal_dt),
#         "interest_rate": bool(sel.interest_rate),
#     }
#     for k, ok in required.items():
#         if not ok:
#             misses.append(k)
#     return None if not misses else f"Не хватает параметров: {', '.join(misses)}"
#
#
# def _ensure_required_limits(sel: DealSelector) -> Optional[str]:
#     """
#     Правило:
#       если есть deal_id — достаточно его одного (остальные поля, включая interest_rate, не требуем);
#       если deal_id НЕТ — обязательны ВСЕ поля: inn, deal_dt, currency, product, amount, interest_rate.
#     """
#     if sel.deal_id:
#         return None
#     misses = []
#     required = {
#         "inn": bool(sel.inn),
#         "product": bool(sel.product),
#         "currency": bool(sel.currency),
#         "amount": bool(sel.amount),
#         "deal_dt": bool(sel.deal_dt),
#         "interest_rate": bool(sel.interest_rate),
#     }
#     for k, ok in required.items():
#         if not ok:
#             misses.append(k)
#     return None if not misses else f"Не хватает параметров: {', '.join(misses)}"


@app.post(f"{PREFIX_TAIL}/api/pricing/from-selector", response_model=PipelineResponse)
async def run_pricing_pipeline_from_selector(
        request: UnifiedRequest = Body(default_factory=UnifiedRequest),
        _roles: List[str] = Depends(require_roles)
) -> PipelineResponse:
    """
    Run pricing pipeline from DealSelector
    """
    try:
        sel = DealSelector()
        sel.deal_id = request.deal_id
        row = _row_from_prefetched(request.prefetched_row)
        if row is None:
            try:
                log.info("--- Start DealExtractor process ---")
                row = DealExtractor(sel).extract_deal()
                if row is None:
                    log.info("--- Start CalcExtractor process ---")
                    row = CalcExtractor(sel).extract_calc()
            except Exception as e:
                log.error("extract error:", e)
        if not row:
            return PipelineResponse(status="error", error="Сделка/расчет не найден по заданным параметрам.")

        log.info(f"--- DealExtractor processing has been successfully ended, row is {row} ---")

        log.info("--- Start IndicatorsExtractor process ---")
        indicators = IndicatorsExtractor(row)

        log.info("--- Start PricingEngine process ---")
        pe = PricingEngine(row, indicators)

        for_r = pe.for_rate()
        fund_r = pe.funding_rate()
        eva_r = pe.eva_rate()
        be_r = pe.break_even()
        nu_r = pe.non_utilizing()
        mi_r = pe.marginal_income()
        tmi_r = pe.target_marginal_income()

        components = {
            "ets": pe.ets,
            "ets_rub": pe.ets_rub,
            "for_rate": for_r["for_rate"],
            "funding_rate": fund_r["funding_rate"],
            "eva_rate": eva_r["eva_rate"],
            "break_even_rate": be_r["break_even_rate"],
            "non_utilizing_rate": nu_r["non_utilizing_rate"],
            "crl": pe.crl,
            "nor": pe.nor,
            "option_price": pe.option_price,
            "indicative_eva": pe.indic_eva,
            "target_eva": pe.target_eva if hasattr(pe, "target_eva") else None,
            "marginal_income": mi_r["marginal_income"],
            "target_marginal_income": tmi_r["target_marginal_income"],
        }

        explain_map = {
            "for_rate": for_r["explain"],
            "funding_rate": fund_r["explain"],
            "eva_rate": eva_r["explain"],
            "break_even_rate": be_r["explain"],
            "non_utilizing_rate": nu_r["explain"],
            "marginal_income": mi_r["explain"],
            "target_marginal_income": tmi_r["explain"],
            "ets": "ЕТС: трансфертная ставка для срока/валюты.",
            "crl": "СРЛ: стоимость регуляторной ликвидности.",
            "nor": "ФОР/НОР: отчисления (резервы) в % годовых.",
        }

        row_fmt = {**row}
        for f in ("value_dt","deal_dt","end_dt"):
            if f in row_fmt:
                row_fmt[f] = str(row_fmt[f]).split(' ')[0]

        result = {
            "components": _decimal_to_float(components),
            "explain_map": explain_map,
            "inputs_used": row_fmt,
            "found_deal": {"present": True, "source": "deals_or_calcs"},
        }

        return PipelineResponse(status="success", data=result)

    except Exception as e:
        return PipelineResponse(status="error", error=f"Internal server error: {str(e)}")

@app.post(f"{PREFIX_TAIL}/api/limits/from-selector", response_model=PipelineResponse)
async def run_limits_pipeline_from_selector(
        request: UnifiedRequest = Body(default_factory=UnifiedRequest),
        _roles: List[str] = Depends(require_roles)
) -> PipelineResponse:
    """
    Run limits pipeline from DealSelector
    """
    try:
        sel = DealSelector()
        sel.deal_id = request.deal_id

        row = _row_from_prefetched(request.prefetched_row)
        if row is None:
            try:
                log.info("--- Start DealExtractor process ---")
                row = DealExtractor(sel).extract_deal()
                if row is None:
                    log.info("--- Start CalcExtractor process ---")
                    row = CalcExtractor(sel).extract_calc()
            except Exception as e:
                log.error("extract error:", e)
        if not row:
            return PipelineResponse(status="error", error="Сделка/расчет не найден по заданным параметрам.")

        log.info(f"--- DealExtractor processing has been successfully ended, row is {row} ---")

        log.info("--- Start IndicatorsExtractor process ---")
        indicators = IndicatorsExtractor(row)

        log.info("--- Start LimitEngine process ---")
        limits = LimitEngine(row, indicators)

        limit_impact_quote = limits.limit_impact_quote()
        if not isinstance(limit_impact_quote, dict) or "limit_impact" not in limit_impact_quote:
            return PipelineResponse(status="error", error="Влияние на лимит при котировании не может быть рассчитано для заданных параметров.")

        limit_impact_active_deal = limits.limit_impact_active_deal()
        limit_timetable = limits.limit_timetable()

        components = {
            "Влияние на лимит при котировании": limit_impact_quote['limit_impact'],
            "Влияние на лимит действующей сделки": limit_impact_active_deal['deltaLimitAmt']['value'],
            "Влияние на лимит КПК действующей сделки": limit_impact_active_deal['deltaLimitAmtKpk']['value'],
            "Влияние на лимит ЦА действующей сделки": limit_impact_active_deal['deltaLimitAmtCa']['value'],
            "График начисления лимита по сделке": limit_timetable,
            "eva_rate": limits.eva_rate,
            "indicative_eva": limits.indic_eva,
            "target_eva": limits.target_eva,
            "eva_diff": limits.eva_diff,
            "limit_discount_coef": limits.LIMIT_COEF,
            "ca_coef": limits.CA_LIMIT_DISCOUNT,
            "k_coef": limits.K,
        }
        explain_map = {
            "Влияние на лимит при котировании": limit_impact_quote["explain"],
            "Влияние на лимит действующей сделки": limit_impact_active_deal['deltaLimitAmt']['explanation'],
            "Влияние на лимит КПК действующей сделки": limit_impact_active_deal['deltaLimitAmtKpk']['explanation'],
            "Влияние на лимит ЦА действующей сделки": limit_impact_active_deal['deltaLimitAmtCa']['explanation'],
            "График начисления лимита по сделке": limit_timetable,
            "k_coef": "Коэффициент K для доли/риска.",
            "limit_discount_coef": "Дисконт лимита по времени.",
            "ca_coef": "Доля центрального аппарата.",
        }

        row_fmt = {**row}
        for f in ("value_dt","deal_dt","end_dt"):
            if f in row_fmt:
                row_fmt[f] = str(row_fmt[f]).split(' ')[0]

        result = {
            "components": _decimal_to_float(components),
            "explain_map": explain_map,
            "inputs_used": row_fmt,
            "found_deal": {"present": True, "source": "deals_or_calcs"},
        }

        return PipelineResponse(status="success", data=result)

    except Exception as e:
        return PipelineResponse(status="error", error=f"Internal server error: {str(e)}")

@app.post(f"{PREFIX_TAIL}/api/both/from-selector", response_model=PipelineResponse)
async def run_both_pipeline_from_selector(
        request: UnifiedRequest = Body(default_factory=UnifiedRequest),
        _roles: List[str] = Depends(require_roles)
) -> PipelineResponse:
    """
    Run both pricing and limits pipelines from DealSelector
    """
    try:
        sel = DealSelector()
        sel.deal_id = request.deal_id
        # sel.inn = request.inn
        # sel.product = request.product
        # sel.currency = request.currency
        # sel.amount = request.amount
        # sel.deal_dt = request.deal_dt
        # sel.maturity_dt = request.maturity_dt
        # sel.term = request.term
        # sel.interest_rate = request.interest_rate

        # need = _ensure_required_pricing(sel)
        # if need:
        #     return PipelineResponse(status="error", error=need)

        row = _row_from_prefetched(request.prefetched_row)
        if row is None:
            try:
                log.info("--- Start DealExtractor process ---")
                row = DealExtractor(sel).extract_deal()
                if row is None:
                    log.info("--- Start CalcExtractor process ---")
                    row = CalcExtractor(sel).extract_calc()
            except Exception as e:
                log.error("extract error:", e)
        if not row:
            return PipelineResponse(status="error", error="Сделка/расчет не найден по заданным параметрам.")

        log.info(f"--- DealExtractor processing has been successfully ended, row is {row} ---")

        log.info("--- Start IndicatorsExtractor process ---")
        indicators = IndicatorsExtractor(row)

        log.info("--- Start PricingEngine process ---")
        pe = PricingEngine(row, indicators)

        log.info("--- Start LimitEngine process ---")
        limits = LimitEngine(row, indicators)

        for_r = pe.for_rate()
        fund_r = pe.funding_rate()
        eva_r = pe.eva_rate()
        be_r = pe.break_even()
        nu_r = pe.non_utilizing()
        mi_r = pe.marginal_income()
        tmi_r = pe.target_marginal_income()

        limit_impact_quote = limits.limit_impact_quote()
        if not isinstance(limit_impact_quote, dict) or "limit_impact" not in limit_impact_quote:
            return PipelineResponse(status="error", error="Влияние на лимит при котировании не может быть рассчитано для заданных параметров.")
        limit_impact_active_deal = limits.limit_impact_active_deal()
        limit_timetable = limits.limit_timetable()

        components = {
            # pricing
            "ets": pe.ets,
            "ets_rub": pe.ets_rub,
            "for_rate": for_r["for_rate"],
            "funding_rate": fund_r["funding_rate"],
            "eva_rate": eva_r["eva_rate"],
            "break_even_rate": be_r["break_even_rate"],
            "non_utilizing_rate": nu_r["non_utilizing_rate"],
            "crl": pe.crl,
            "nor": pe.nor,
            "option_price": pe.option_price,
            "indicative_eva": pe.indic_eva,
            "target_eva": pe.target_eva if hasattr(pe, "target_eva") else None,
            "marginal_income": mi_r["marginal_income"],
            "target_marginal_income": tmi_r["target_marginal_income"],
            # limits
            "Влияние на лимит при котировании": limit_impact_quote['limit_impact'],
            "Влияние на лимит действующей сделки": limit_impact_active_deal['deltaLimitAmt']['value'],
            "Влияние на лимит КПК действующей сделки": limit_impact_active_deal['deltaLimitAmtKpk']['value'],
            "Влияние на лимит ЦА действующей сделки": limit_impact_active_deal['deltaLimitAmtCa']['value'],
            "График начисления лимита по сделке": limit_timetable,
            # справочные по limits
            "eva_rate_limits": limits.eva_rate,
            "indicative_eva_limits": limits.indic_eva,
            "target_eva_limits": limits.target_eva,
            "eva_diff": limits.eva_diff,
            "limit_discount_coef": limits.LIMIT_COEF,
            "ca_coef": limits.CA_LIMIT_DISCOUNT,
            "k_coef": limits.K,
        }
        explain_map = {
            "for_rate": for_r["explain"],
            "funding_rate": fund_r["explain"],
            "eva_rate": eva_r["explain"],
            "break_even_rate": be_r["explain"],
            "non_utilizing_rate": nu_r["explain"],
            "marginal_income": mi_r["explain"],
            "target_marginal_income": tmi_r["explain"],
            "ets": "ЕТС: трансфертная ставка для срока/валюты.",
            "crl": "СРЛ: стоимость регуляторной ликвидности.",
            "nor": "ФОР/НОР: отчисления (резервы) в % годовых.",
            "Влияние на лимит при котировании": limit_impact_quote["explain"],
            "Влияние на лимит действующей сделки": limit_impact_active_deal['deltaLimitAmt']['explanation'],
            "Влияние на лимит КПК действующей сделки": limit_impact_active_deal['deltaLimitAmtKpk']['explanation'],
            "Влияние на лимит ЦА действующей сделки": limit_impact_active_deal['deltaLimitAmtCa']['explanation'],
        }

        row_fmt = {**row}
        for f in ("value_dt","deal_dt","end_dt"):
            if f in row_fmt:
                row_fmt[f] = str(row_fmt[f]).split(' ')[0]

        result = {
            "components": _decimal_to_float(components),
            "explain_map": explain_map,
            "inputs_used": row_fmt,
            "found_deal": {"present": True, "source": "deals_or_calcs"},
        }

        return PipelineResponse(status="success", data=result)

    except Exception as e:
        return PipelineResponse(status="error", error=f"Internal server error: {str(e)}")

@app.post(f"{PREFIX_TAIL}/api/report", response_model=PipelineResponse)
def run_report_pipeline(
        request: ReportRequest = Body(default_factory=ReportRequest),
        _roles: List[str] = Depends(require_roles)
):

    log.info("--- Start ReportExtractor process ---")

    period_start = request.period_start
    period_end = request.period_end
    if not request.inns:
        log.info("--- No inns provided ---")
        inns = []
    else:
        inns = request.inns

    try:
        log.info("--- Continue ReportExtractor process ---")
        report = ReportExtractor(
            period_start=period_start,
            period_end=period_end,
            inns=inns
        ).extract_report()

        if not report:
            log.info("--- ReportExtractor has been successfully finished ---")
            return PipelineResponse(status="success", data="No data found for the specified period and inns.")


        elif report['n_deals'] == 0:
            log.info("--- ReportExtractor has been successfully finished ---")

        result =  f"""Общее количество сделок: {report['n_deals']} шт.
    Сделки по каждому продукту: {report['count_by_product']}.
    Средневзвешенная срочность сделок: {report['weighted_mean_term']:.2f}
    Средневзвешенная ставка сделок: {report['weighted_mean_interest_rate']:.2f}%
    Средневзвешенный лимит суммы: {report['weighted_mean_sum_limit']:.2f}
    Средневзвешенный по сроку объем сделки: {report['weighted_amt']:.2f}
    Минимальная/максимальная дата окончания сделки: {report['maturity_dt_min']} / {report['maturity_dt_max']}
    Минимальная/максимальная сумма сделки: {report['single_deal_amt_min']:.2f} / {report['single_deal_amt_max']:.2f}
    Минимальная/максимальная ставка по сделке: {report['interest_rate_min']:.2f} / {report['interest_rate_max']:.2f}
    Минимальный/максимальный размер текущего лимита по сделке: {report['delta_limit_amt_min']:.2f} / {report['delta_limit_amt_max']:.2f}
    Минимальный/максимальный размер лимита при котировании по сделке: {report['original_delta_limit_amt_min']:.2f} / {report['original_delta_limit_amt_max']:.2f}
    Минимальная/максимальная доходность по сделке в рублях: {report['marginal_income_amt_min']:.2f} / {report['marginal_income_amt_max']:.2f}
    Минимальная/максимальная доходность по сделке в % : {report['eva_rate_min']:.4f} / {report['eva_rate_max']:.4f}
    """
        resp = PipelineResponse(status="success", data=result)
        log.info("--- ReportExtractor has been successfully finished ---")
        return resp
    except Exception as e:
        log.exception(e)
        return PipelineResponse(status="error", error=f"Internal server error: {str(e)}")


@app.post(f"{PREFIX_TAIL}/api/kpk/limit-change")
def kpk_limit_change(req: KpkRequest, _roles: List[str] = Depends(require_roles)):
    try:
        log.info(
            "[kpk_app] limit-change request division_cd=%s report_dt=%s report_dt_from=%s delta_amt=%s include_details=%s today_override=%s role_count=%s",
            req.division_cd,
            req.report_dt,
            req.report_dt_from,
            req.delta_amt,
            req.include_details,
            req.today_override,
            len(_roles or []),
        )
        data = load_for_limit_change(
            division_cd=req.division_cd or "",
            date=req.report_dt,
            date_from=req.report_dt_from,
            today_override=req.today_override,
        )
        log.info(
            "[kpk_app] limit-change loaded data division_cd=%s shapes=%s",
            req.division_cd,
            {
                "df_lim": _df_shape(data.get("df_lim")),
                "df_trx": _df_shape(data.get("df_trx")),
                "df_deals": _df_shape(data.get("df_deals")),
                "df_calc_logs": _df_shape(data.get("df_calc_logs")),
            },
        )

        log.info(
            "[kpk_app] limit-change analysis date mapping division_cd=%s request_report_dt=%s analysis_report_dt=%s request_report_dt_from=%s analysis_report_dt_from=%s",
            req.division_cd,
            req.report_dt,
            req.report_dt,
            req.report_dt_from,
            req.report_dt_from,
        )

        res = analyze_single_kpk(
            df_lim=data["df_lim"],
            df_trx=data["df_trx"],
            df_deals=data["df_deals"],
            df_calc_logs=data["df_calc_logs"],
            division_cd=req.division_cd or "",
            date=req.report_dt,
            date_from=req.report_dt_from,
            delta_hint=req.delta_amt,
            include_details=req.include_details,
            today_override=req.today_override,
        )
        log.info(
            "[kpk_app] limit-change analysis result division_cd=%s result=%s",
            req.division_cd,
            _kpk_result_summary(res),
        )
        if isinstance(res, dict) and res.get("status") == "success" and isinstance(res.get("data"), dict):
            log.info(
                "[kpk_app] limit-change enriching client names division_cd=%s",
                req.division_cd,
            )
            _enrich_deals_with_client_names(res["data"])
            log.info(
                "[kpk_app] limit-change client names enriched division_cd=%s",
                req.division_cd,
            )
        log.info(
            "[kpk_app] limit-change response ready division_cd=%s response=%s",
            req.division_cd,
            _kpk_result_summary(res),
        )
        return res
    except Exception as e:
        log.exception(f'error kpk_limit_change: {e}')
        return {"status": "error", "error": str(e)}

@app.post(f"{PREFIX_TAIL}/api/kpk/negative-report")
def kpk_negative_report(req: KpkRequest, _roles: List[str] = Depends(require_roles)):
    try:
        data = load_for_negative_report(date=req.report_dt)
        res = analyze_negative_report(
            df_lim=data["df_lim"],
            df_trx=data["df_trx"],
            df_deals=data["df_deals"],
            df_calc_logs=data["df_calc_logs"],
            date=req.report_dt,
            include_details=req.include_details,
            today_override=req.today_override,
        )
        return res
    except Exception as e:
        log.error(f'error kpk_negative_report: {e}')
        return {"status": "error", "error": str(e)}

@app.post(f"{PREFIX_TAIL}/api/kpk/find-deal")
def kpk_find_deal(req: KpkRequest, _roles: List[str] = Depends(require_roles)):
    if req.delta_amt is None or req.delta_amt == 0:
        return {"status": "error", "error": "delta_amt обязателен и не может быть 0 для поиска сделки"}
    if not req.division_cd:
        return {"status": "error", "error": "division_cd обязателен для поиска сделки"}
    try:
        data = load_for_find_deal(division_cd=req.division_cd or "", date=req.report_dt)
        res = find_deal_by_amount(
            df_deals=data["df_deals"],
            division_cd=req.division_cd or "",
            date=req.report_dt,
            target_amount=req.delta_amt
        )
        if isinstance(res, dict) and res.get("status") == "success" and isinstance(res.get("data"), dict):
            _enrich_deals_with_client_names(res["data"])
        return res
    except Exception as e:
        log.error(f'error kpk_find_deal: {e}')
        return {"status": "error", "error": str(e)}


@app.post(f"{PREFIX_TAIL}/api/kpk/incorrect-deals-report")
def kpk_incorrect_deals_report(req: KpkRequest, _roles: List[str] = Depends(require_roles)):
    try:
        data = load_for_negative_report(date=req.report_dt)
        res = analyze_negative_report(
            df_lim=data["df_lim"],
            df_trx=data["df_trx"],
            df_deals=data["df_deals"],
            df_calc_logs=data["df_calc_logs"],
            date=req.report_dt,
            include_details=req.include_details,
            today_override=req.today_override,
        )
        if not isinstance(res, dict) or res.get("status") != "success" or not isinstance(res.get("data"), dict):
            return JSONResponse(status_code=500, content=res)

        filename = build_incorrect_deals_report_filename(res["data"].get("report_dt"))
        content = build_incorrect_deals_report_xlsx(res["data"])
        headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
        return Response(content=content, media_type=EXCEL_MIME_TYPE, headers=headers)
    except Exception as e:
        log.error(f'error kpk_incorrect_deals_report: {e}')
        return {"status": "error", "error": str(e)}


@app.post(f"{PREFIX_TAIL}/api/kpk/investigate-deal")
def kpk_investigate_deal(req: InvestigateDealRequest, _roles: List[str] = Depends(require_roles)):
    try:
        data = load_for_investigate_deal(
            inn_num=req.inn_num,
            value_dt=req.value_dt,
            as_of_dt=req.as_of_dt,
            delta_amt=req.delta_amt,
            division_cd=req.division_cd,
            lookback_days=req.lookback_days
        )
        res = investigate_deal(
            df_deals=data["df_deals"],
            df_calc_logs=data["df_calc_logs"],
            inn_num=req.inn_num,
            value_dt=req.value_dt,
            as_of_dt=req.as_of_dt,
            delta_amt=req.delta_amt,
            division_cd=req.division_cd,
            lookback_days=req.lookback_days
        )
        return res
    except Exception as e:
        log.error(f'error investigate_deal: {e}')
        return {"status": "error", "error": str(e)}

@app.post(f"{PREFIX_TAIL}/api/kpk/client-history")
def kpk_client_history(req: ClientHistoryRequest, _roles: List[str] = Depends(require_roles)):
    try:
        date_to = req.date_to or req.date
        data = load_for_client_history(
            inn_num=req.inn_num,
            division_cd=req.division_cd,
            date=date_to,
            date_from=req.date_from,
            date_to=req.date_to,
            lookback_days=req.lookback_days
        )
        client_name = ClientDataService(req.inn_num).get_client_info().name
        res = analyze_client_history(
            df_deals=data["df_deals"],
            df_calc_logs=data["df_calc_logs"],
            inn_num=req.inn_num,
            division_cd=req.division_cd,
            date=date_to,
            date_from=req.date_from,
            date_to=req.date_to,
            lookback_days=req.lookback_days
        )
        if (
                client_name
                and isinstance(res, dict)
                and res.get("status") == "success"
                and isinstance(res.get("data"), dict)
        ):
            res["data"]["client_name"] = client_name
        return res
    except Exception as e:
        log.error(f'error kpk_client_history: {e}')
        return {"status": "error", "error": str(e)}
