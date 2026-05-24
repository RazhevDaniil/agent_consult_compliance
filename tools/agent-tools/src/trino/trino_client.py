from __future__ import annotations

import os
import time
import base64
import logging
import urllib3
import requests
import pandas as pd
from pathlib import Path
from dataclasses import dataclass
from typing import List, Optional, Tuple
from requests.exceptions import RequestException

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from .query_utils import test_sql

from ..utils.logger_config import configure_logging


configure_logging()
log = logging.getLogger("---Trino Client---")


# -------------------- config --------------------
@dataclass(frozen=True)
class TrinoConfig:
    coordinators: str = os.getenv("TRINO_URLS", "https://palm-trino-dev.delta.sbrf.ru:8443")
    statement_path: str = "/v1/statement"
    timeout: int = int("90")
    verify_tls: bool = os.getenv("TRINO_VERIFY_TLS", "false").lower() == "true"
    vault_file: str = os.getenv("VAULT_FILE_PATH", "vault/secrets/secrets.properties")
    log_level: str = os.getenv("LOG_LEVEL", "INFO")


CFG = TrinoConfig()

# -------------------- helpers --------------------
class TrinoClientError(RuntimeError):
    pass


def load_properties(path: str) -> dict:
    props = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            props[k.strip()] = v.strip()
    return props


def _get_trino_credentials() -> Tuple[Optional[str], Optional[str]]:
    props = load_properties(CFG.vault_file)
    return props.get("trino_username"), props.get("trino_password")


def _basic_auth_header(username: str, password: str) -> str:
    raw = f"{username}:{password}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("utf-8")


def _coordinators() -> List[str]:
    items = []
    for c in CFG.coordinators.split(","):
        c = c.strip()
        if not c:
            continue
        items.append(c.rstrip("/") + CFG.statement_path)
    return items or ["https://palm-trino-dev.delta.sbrf.ru:8443" + CFG.statement_path]


def _safe_snippet(text: str, limit: int = 800) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "...(truncated)"


def _raise_if_trino_error(data: dict, url: str) -> None:
    if isinstance(data, dict) and "error" in data and data["error"]:
        err = data["error"]
        msg = err.get("message") or str(err)
        code = err.get("errorCode")
        name = err.get("errorName")
        raise TrinoClientError(f"Trino error from {url}: {name}({code}) {msg}")


# -------------------- main --------------------
def run_trino(sql: str = "SHOW CATALOGS", catalog: str = "system", schema: str = "information_schema", is_test: bool=False) -> pd.DataFrame:
    log.info("RUN_TRINO start")
    log.info("SQL: %s", sql)

    username, password = _get_trino_credentials()
    if not username or not password:
        raise TrinoClientError("Trino credentials not found (trino_username/trino_password)")

    urls = _coordinators()
    log.info("Trino coordinators (statement endpoints): %s", urls)

    headers = {
        "Content-Type": "text/plain",
        "X-Trino-Catalog": catalog,
        "X-Trino-Schema": schema,
        "X-Trino-User": username,
        "Authorization": _basic_auth_header(username, password),
    }

    last_error: Optional[Exception] = None

    # 1) POST: стартуем запрос
    data = None
    used_url = None
    for url in urls:
        if is_test:
            try:
                test_headers = {
                    "Content-Type": "text/plain",
                    "X-Trino-Catalog": test_sql['catalog'],
                    "X-Trino-Schema": test_sql['schema'],
                    "Authorization": _basic_auth_header(username, password),
                }
                log.info((f"--- TEST REQUEST to TRINO URL: {url} and SQL: {test_sql}"))

                test_resp = requests.post(url, headers=test_headers, data=test_sql['query'], verify=False, timeout=90)
                log.info("POST response: status=%s content_type=%s", test_resp.status_code, test_resp.headers.get("Content-Type"))
                log.info(f"--- TEXT FROM TRINO RESPONSE {test_resp.text} ---")
                test_data = test_resp.json()
                log.info(f"--- DATA FROM TRINO RESPONSE {test_data} ---")
            except Exception as e:
                log.error(f"--- TEST REQUEST FAILED {e} ---")
                continue
        try:
            log.info("POST %s (timeout=%s verify_tls=%s)", url, CFG.timeout, CFG.verify_tls)
            resp = requests.post(url, headers=headers, data=sql, verify=CFG.verify_tls, timeout=CFG.timeout)

            log.info("POST response: status=%s content_type=%s", resp.status_code, resp.headers.get("Content-Type"))

            if resp.status_code >= 400:
                # здесь часто приходит HTML от прокси/ingress — это важно видеть
                body = _safe_snippet(resp.text)
                raise TrinoClientError(f"POST {url} failed: HTTP {resp.status_code}, body={body}")

            try:
                data = resp.json()
            except ValueError:
                body = _safe_snippet(resp.text)
                raise TrinoClientError(f"POST {url} returned non-JSON body={body}")

            _raise_if_trino_error(data, url)

            used_url = url
            log.info("POST ok: keys=%s id=%s nextUri=%s",
                     list(data.keys()), data.get("id"), data.get("nextUri"))
            break

        except Exception as e:
            last_error = e
            log.exception("POST attempt failed for %s", url)

    if data is None:
        raise TrinoClientError(f"All coordinators failed on POST. Last error: {last_error!r}")

    # 2) GET: дочитываем nextUri
    rows = []
    columns = None
    query_id = data.get("id")

    # Ретраи только на GET nextUri (чтобы не создавать дублирующие запросы POST-ом)
    max_nexturi_retries = int(os.getenv("TRINO_NEXTURI_RETRIES", "2"))
    nexturi_retry_sleep = float(os.getenv("TRINO_NEXTURI_RETRY_SLEEP_SEC", "0.5"))

    while True:
        if "columns" in data and columns is None:
            columns = [c["name"] for c in data["columns"]]
            log.info("Columns received (%s): %s", len(columns), columns)

        if "data" in data:
            rows.extend(data["data"])
            log.info("Fetched chunk: total_rows=%s query_id=%s", len(rows), query_id)

        next_uri = data.get("nextUri")
        if not next_uri:
            break

        # логируем nextUri чтобы было видно куда реально идёт GET
        log.info("GET nextUri: %s", next_uri)

        attempt = 0
        while True:
            try:
                resp = requests.get(next_uri, headers=headers, verify=CFG.verify_tls, timeout=CFG.timeout)
                if resp.status_code >= 400:
                    body = _safe_snippet(resp.text)
                    raise TrinoClientError(f"GET nextUri failed: HTTP {resp.status_code}, body={body}")

                try:
                    data = resp.json()
                except ValueError:
                    body = _safe_snippet(resp.text)
                    raise TrinoClientError(f"GET nextUri returned non-JSON body={body}")

                _raise_if_trino_error(data, next_uri)
                break

            except RequestException as e:
                # типичная история: Connection reset by peer
                if attempt < max_nexturi_retries:
                    attempt += 1
                    log.warning("GET nextUri network error (attempt %s/%s): %r",
                                attempt, max_nexturi_retries, e)
                    time.sleep(nexturi_retry_sleep)
                    continue
                log.exception("GET nextUri failed окончательно after retries")
                raise TrinoClientError(f"GET nextUri failed after retries: {e!r}") from e

    if not columns or not rows:
        log.warning("Dataset is empty. query_id=%s used_url=%s", query_id, used_url)
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=columns)
    log.info("RUN_TRINO done: query_id=%s shape=%s", query_id, df.shape)
    return df
