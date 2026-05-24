import os
import json
import random
import logging
import requests
import socket
import re
import uuid
import numpy as np
from datetime import datetime
from typing import Optional, Dict, Any
from urllib.parse import urlencode

from flask import request as flask_request, Response as flask_response

import dash
from dash import dcc, html, callback_context
from dash.dependencies import Input, Output, State, ALL
import dash_bootstrap_components as dbc

from utils.assets_bootstrap_runtime import CSS_RAW
from utils.config import configure_logging
from utils.log_settings import LOGGER_CONFIG


configure_logging()
logger = logging.getLogger("api-run-ui-for-agent")
audit_logger = logging.getLogger("audit")

try:
    from utils.auth_middleware import get_user
except ModuleNotFoundError:
    from .utils.auth_middleware import get_user

CUSTOM_INDEX_STRING = f'''
<!DOCTYPE html>
<html>
    <head>
        {{%metas%}}
        <title>{{%title%}}</title>
        <style>
            {CSS_RAW}
        </style>
        {{%favicon%}}
        {{%css%}}
    </head>
    <body>
        {{%app_entry%}}
        <footer>
            {{%config%}}
            {{%scripts%}}
            {{%renderer%}}
        </footer>
    </body>
</html>
'''

HOST = os.getenv("SERVICE_HOST", "0.0.0.0")
PORT = int(os.getenv("SERVICE_PORT", "8080"))
TIMEOUT = int(os.getenv("AGENT_TIMEOUT", "180"))

API_BASE_URL = os.getenv(
    "AGENT_SERVICE_URL",
    "http://agent-v1.ci09529287-aif-agnbf-dt-kmhelp-corpdep-dev.apps.a4x8eda3.k8s.delta.sbrf.ru"
)
ASK_ENDPOINT = f"{API_BASE_URL}/chat"
INCORRECT_DEALS_REPORT_ENDPOINT = f"{API_BASE_URL}/api/kpk/incorrect-deals-report"

PREFIX = os.getenv("ASSETS_PREFIX", "/dev/pss/")
if not PREFIX.endswith("/"):
    PREFIX += "/"
DOWNLOAD_REPORT_PATH = f"{PREFIX}download/incorrect-deals-report"

MAX_INPUT_CHARS = 32000
TRACE_HEADER_NAME = "x-trace-id"


def gen_id(prefix: str = "") -> str:
    rand_part = "".join(np.random.choice(list("0123456789abcdef"), 16))
    return f"{prefix}{rand_part}"


def get_local_ip():
    """Получение IP адреса пода/хоста"""
    try:
        # Попытка определить IP через подключение
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0)
        # Адрес не важен, важно, как OS выберет маршрут
        s.connect(('10.254.254.254', 1))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        logger.error("can't get pod ip with socket")
        ip = "0.0.0.0"
    return ip


def get_request_jwt_token() -> Optional[str]:
    for header_name in ("authorization", "Authorization"):
        raw_value = flask_request.headers.get(header_name)
        if raw_value:
            return raw_value.split()[-1]
    return None


def extract_filename(content_disposition: Optional[str], fallback: str) -> str:
    if not content_disposition:
        return fallback
    match = re.search(r'filename="?([^";]+)"?', content_disposition)
    if not match:
        return fallback
    return match.group(1) or fallback


# --- API клиент для агента ---
class AgentAPIClient:
    def __init__(self, base_url: str = ASK_ENDPOINT):
        self.base_url = base_url

    def send_message(
            self,
            message: str,
            chat_id: str,
            auth_header: Optional[str] = None,
            trace_id: Optional[str] = None,
            user_id: Optional[str] = None,
    ) -> dict:
        """Отправляет сообщение агенту через API"""
        trace_id = trace_id or str(uuid.uuid4())
        try:
            body = {"message": message, "chat_id": chat_id}
            logger.info("Sending message to agent: %s trace_id=%s", body, trace_id)

            headers = {
                TRACE_HEADER_NAME: trace_id,
                "X-Source-System": "support-ui",
            }
            if auth_header:
                headers["Authorization"] = auth_header
            if user_id:
                headers["X-User-Id"] = user_id

            response = requests.post(
                self.base_url,
                json=body,
                headers=headers,
                timeout=TIMEOUT,
            )
            response.raise_for_status()
            result = response.json()
            result["_trace_id"] = response.headers.get(TRACE_HEADER_NAME, trace_id)
            return result

        except requests.exceptions.RequestException as e:
            logger.error("API request failed: %s trace_id=%s", e, trace_id, exc_info=True)
            # Возвращаем спец-ответ, чтобы UI показал ошибку, но аудит знал, что это FAIL
            return {
                "answer": "Извините, произошла ошибка при обращении к агенту.",
                "destination": "error",
                "confidence": None,
                "sources": [],
                "error": str(e),
                "_trace_id": trace_id,
            }

    def download_incorrect_deals_report(
            self,
            chat_id: str,
            report_dt: str,
            auth_header: Optional[str] = None,
            trace_id: Optional[str] = None,
            user_id: Optional[str] = None,
    ) -> dict:
        trace_id = trace_id or str(uuid.uuid4())
        headers = {
            TRACE_HEADER_NAME: trace_id,
            "X-Source-System": "support-ui",
        }
        if auth_header:
            headers["Authorization"] = auth_header
        if user_id:
            headers["X-User-Id"] = user_id

        response = requests.post(
            INCORRECT_DEALS_REPORT_ENDPOINT,
            json={"chat_id": chat_id, "report_dt": report_dt},
            headers=headers,
            timeout=TIMEOUT,
        )
        response.raise_for_status()

        fallback = f"new_deals_report_{report_dt.replace('-', '')}.xlsx"
        return {
            "content": response.content,
            "filename": extract_filename(response.headers.get("Content-Disposition"), fallback),
            "media_type": response.headers.get(
                "Content-Type",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ),
            "trace_id": response.headers.get(TRACE_HEADER_NAME, trace_id),
        }


def create_loading_overlay(is_loading: bool) -> html.Div:
    if not is_loading:
        return html.Div(style={'display': 'none'})

    funny = random.choice([
        "Шуршу данными…", "Склеиваю EVA…",
        "Вызываю калькулятор…", "Размораживаю лимиты…",
        "Вызываю команду бизнес-поддержки...", "Изучаю методологию..."
    ])

    return html.Div([
        html.Div(className="kmh-spinner"),
        html.Div([
            funny,
            html.Span(className="kmh-dot kmh-dot1"),
            html.Span(className="kmh-dot kmh-dot2"),
            html.Span(className="kmh-dot kmh-dot3"),
        ], className="kmh-caption"),
    ], className="kmh-overlay", id="loading-overlay-content")


# --- ОБНОВЛЕННЫЙ АУДИТ ---
def log_audit_event(
        event_name: str,
        status: str,  # SUCCESS / FAIL
        user_login: Optional[str] = None,
        reason: Optional[str] = None,
        session_id: Optional[str] = None,
        object_name: str = "agent-ui",
        **kwargs
) -> None:
    """
    Формирует сообщение для аудита в строгом соответствии с требованиями.
    """
    # 1. Определяем пользователя
    if not user_login:
        try:
            u = get_user()
            user_login = getattr(u, "login", "anonymous") if u else "anonymous"
        except Exception:
            user_login = "anonymous"

    # 2. Переменные среды
    app_id = os.getenv("AUDIT_APP_ID", LOGGER_CONFIG.get("audit_app_id"))
    process_name = os.getenv("AUDIT_PROCESS_NAME", LOGGER_CONFIG.get("audit_process_name"))

    # 3. Сетевые параметры
    current_ip = get_local_ip()
    pod_name = os.getenv("POD_NAME", socket.gethostname())

    # 4. Формируем payload
    audit_payload = {
        "APP_ID": app_id,
        "EVENT_NAME": event_name,
        "USER_LOGIN": user_login,
        "SUBTYPE_ID": "C0",
        "HOST": HOST,
        "OPERATION_DATE": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "TYPE_ID": "Audit",
        "STATUS": status,
        "PROCESS_NAME": process_name,
        "OBJECT_ID": 0,
        "DNS_NAME": pod_name,
        "OBJECT_NAME": object_name,
        "IP_ADDRESS": current_ip,
        "SESSION_ID": session_id or "unknown",
    }

    # Если есть ошибка - добавляем REASON
    if reason:
        audit_payload["REASON"] = reason

    # Добавляем любые доп. поля из kwargs, если нужны (например, вопрос/ответ для отладки,
    # хотя в строгом формате они могут быть лишними, но оставим как доп. данные или уберем при необходимости)
    # Для строгого соответствия примеру лишние поля лучше не слать в корень JSON,
    # но можно добавить их в поле REASON или отдельное поле, если схема позволяет.
    # В данной реализации придерживаемся строгих полей, но REASON используем для деталей.

    # Отправляем словарь - AuditRawFormatter превратит его в JSON
    audit_logger.info(audit_payload)


# --- Dash app ---
app = dash.Dash(
    __name__,
    index_string=CUSTOM_INDEX_STRING,
    update_title=None,
    title='PSS Agent UI',
    url_base_pathname=PREFIX,
)

server = app.server


@server.get("/health")
def health():
    return {"status": "ok"}, 200


@server.get("/ready")
def ready():
    return {"status": "ready"}, 200


@server.get(DOWNLOAD_REPORT_PATH)
def download_incorrect_deals_report():
    chat_id = (flask_request.args.get("chat_id") or "").strip()
    report_dt = (flask_request.args.get("report_dt") or "").strip()
    if not chat_id or not report_dt:
        return {"error": "chat_id and report_dt are required"}, 400

    trace_id = str(uuid.uuid4())
    user = get_user()
    user_id = getattr(user, "login", None) if user else None

    logger.info(
        "Download generated report requested: chat_id=%s report_dt=%s trace_id=%s",
        chat_id,
        report_dt,
        trace_id,
    )
    try:
        result = AgentAPIClient().download_incorrect_deals_report(
            chat_id=chat_id,
            report_dt=report_dt,
            auth_header=get_request_jwt_token(),
            trace_id=trace_id,
            user_id=user_id,
        )
    except Exception as e:
        logger.error("Failed to download generated report: %s trace_id=%s", e, trace_id, exc_info=True)
        return {"error": "Failed to download report"}, 502

    headers = {
        "Content-Disposition": f'attachment; filename="{result["filename"]}"',
        TRACE_HEADER_NAME: result["trace_id"],
    }
    return flask_response(
        result["content"],
        mimetype=result["media_type"],
        headers=headers,
    )


def serve_layout():
    """
    Рендер макета + Аудит входа (SUCCESS/FAIL)
    """
    user = get_user()

    # Генерируем ID сессии сразу, чтобы записать в лог (даже при ошибке)
    # Т.к. dcc.Store еще не существует на клиенте в этот момент, генерируем временно для лога
    temp_session_id = gen_id("sess_")

    if user is None:
        # АУДИТ: Неудачная попытка входа (нет прав/токена)
        log_audit_event(
            event_name="Вход в интерфейс",
            status="FAIL",
            user_login="anonymous",
            reason="User not authenticated or missing roles",
            session_id=temp_session_id
        )

        return html.Div(
            dbc.Container(
                html.Div([
                    html.H1("🚫 Доступ запрещен", className="text-danger mb-4"),
                    html.P("У вас нет необходимой роли для доступа к этому приложению."),
                    html.Hr(),
                    html.P("При необходимости оформите доступ..."),
                ], className="p-5 bg-light rounded text-center my-5 shadow-sm"),
                fluid=True,
            ),
            style={'height': '100vh', 'display': 'flex', 'alignItems': 'center', 'justifyContent': 'center'}
        )

    # Получаем логин
    login = getattr(user, "login", "unknown")

    # АУДИТ: Успешный вход
    log_audit_event(
        event_name="Вход в интерфейс",
        status="SUCCESS",
        user_login=login,
        session_id=temp_session_id
    )

    full_name = getattr(user, "full_name", None) or login
    try:
        parts = full_name.split()
        first_name = parts[1] if len(parts) >= 2 else full_name
    except Exception:
        first_name = full_name

    current_hour = datetime.now().hour
    if current_hour < 11:
        greeting_part = "Доброе утро"
    elif 11 <= current_hour < 16:
        greeting_part = "Добрый день"
    else:
        greeting_part = "Добрый вечер"
    greeting_text = f"{greeting_part}, {full_name}!"

    return html.Div(
        [
            dcc.Location(id='url', refresh=False),
            dcc.Store(id='session-store', data={
                'session_id': temp_session_id,  # Используем тот же ID, что в логе
                'chat_id': gen_id("chat_"),
                'ui_messages': [],
                'disliked_flags': {},
                'msg_counter': 0,
                'current_user': login,
                'user_first_name': first_name,
            }),
            dcc.Store(id='loading-flag', data={'is_loading': False}),
            dcc.Store(id='rerun-trigger', data=0),

            dbc.Toast(
                greeting_text,
                id="greeting-toast",
                header="👋 Приветствие",
                is_open=True,
                dismissable=True,
                duration=4000,
                icon="success",
                style={"position": "fixed", "top": 20, "right": 20, "width": 350, "zIndex": 9999},
            ),

            dbc.Container(
                [
                    dbc.Row([
                        dbc.Col(
                            html.Div(
                                [
                                    html.H4("⚙️ Настройки", className="mb-3"),
                                    html.P(["Session: ", html.Span(id="session-id-display")]),
                                    html.P(["Chat: ", html.Span(id="chat-id-display")]),
                                    html.Hr(),
                                    html.H4("👤 Пользователь", className="mb-3"),
                                    html.Div(
                                        html.Span(id="user-name-display", className="fw-bold fs-5"),
                                        className="mb-3"
                                    ),
                                    html.Hr(),
                                    dbc.Button(
                                        "Новый диалог",
                                        id="reset-button",
                                        color="secondary",
                                        className="w-100",
                                    ),
                                ],
                                className="p-3 bg-light rounded",
                            ),
                            width=3,
                        ),
                        dbc.Col(
                            [
                                html.H1("💬 AI-агент по ценообразованию и фондированию СЮЛ", className="my-4"),
                                html.Div(id="chat-history-container", className="mb-4"),
                                html.Div(
                                    [
                                        dbc.InputGroup(
                                            [
                                                dbc.Input(
                                                    id="user-input",
                                                    placeholder="Напишите вопрос…",
                                                    type="text",
                                                    debounce=False,
                                                    maxLength=MAX_INPUT_CHARS
                                                ),
                                                dbc.Button("Отправить", id="send-button", color="primary"),
                                            ],
                                            className="mb-1"
                                        ),
                                        # Блок для счетчика и предупреждения
                                        html.Div(
                                            id="char-counter-display",
                                            className="text-end small text-muted",
                                            style={"fontSize": "0.85rem", "minHeight": "20px"}
                                        )
                                    ]
                                ),
                            ],
                            width=9,
                        ),
                    ], className="g-0")
                ],
                fluid=True
            ),
            html.Div(id='loading-overlay')
        ]
    )


app.layout = serve_layout


# --- Callbacks ---

@app.callback(
    Output('session-store', 'data', allow_duplicate=True),
    Input('reset-button', 'n_clicks'),
    State('session-store', 'data'),
    prevent_initial_call=True
)
def reset_dialog(n_clicks, session_data):
    if not n_clicks:
        return dash.no_update
    new_data = session_data.copy()
    new_data['chat_id'] = gen_id("chat_")
    new_data['ui_messages'] = []
    new_data['disliked_flags'] = {}
    new_data['msg_counter'] = 0
    return new_data


@app.callback(
    [Output("session-id-display", "children"),
     Output("chat-id-display", "children"),
     Output("user-name-display", "children")],
    Input("session-store", "data")
)
def update_sidebar_info(session_data):
    user_name = session_data.get('user_first_name', session_data.get('current_user', 'anonymous'))
    return (session_data.get('session_id', '')[:10],
            session_data.get('chat_id', '')[:10],
            user_name)


@app.callback(
    [Output('session-store', 'data', allow_duplicate=True),
     Output('user-input', 'value'),
     Output('loading-flag', 'data'),
     Output('rerun-trigger', 'data', allow_duplicate=True)],
    [Input('send-button', 'n_clicks'), Input('user-input', 'n_submit')],
    [State('user-input', 'value'),
     State('session-store', 'data'),
     State('loading-flag', 'data')],
    prevent_initial_call=True
)
def handle_user_input(send_clicks, n_submit, user_input, session_data, loading_flag):
    """
    Обработка ввода пользователя. Аудит здесь НЕ пишем,
    пишем по факту успешного/неуспешного ответа API.
    """
    trigger = callback_context.triggered[0]['prop_id'].split('.')[0] if callback_context.triggered else ""
    if not user_input or not user_input.strip():
        return dash.no_update, "", dash.no_update, dash.no_update

    if trigger in ('send-button', 'user-input'):
        new_data = session_data.copy()
        new_data['ui_messages'].append({"role": "user", "content": user_input})
        loading_flag = (loading_flag or {}).copy()
        loading_flag['is_loading'] = True
        return new_data, "", loading_flag, dash.no_update

    return dash.no_update, dash.no_update, dash.no_update, dash.no_update


@app.callback(
    [Output('session-store', 'data', allow_duplicate=True),
     Output('loading-flag', 'data', allow_duplicate=True),
     Output('rerun-trigger', 'data')],
    Input('loading-flag', 'data'),
    State('session-store', 'data'),
    prevent_initial_call=True
)
def call_agent_api(loading_flag, session_data):
    """
    Вызов агента. Здесь логируем событие 'Отправка сообщения в агент'.
    """
    if not (loading_flag or {}).get('is_loading'):
        return dash.no_update, dash.no_update, dash.no_update

    user_message = next((m['content'] for m in reversed(session_data['ui_messages']) if m['role'] == 'user'), None)
    if not user_message:
        loading_flag['is_loading'] = False
        return dash.no_update, loading_flag, 0

    agent_client = AgentAPIClient()

    jwt_token = get_request_jwt_token()

    if not jwt_token:
        msg = 'JWT token is missing'
        logger.exception(msg)

    trace_id = str(uuid.uuid4())
    logger.info("try to send message to agent with jwt_token trace_id=%s", trace_id)

    response = agent_client.send_message(
        message=user_message,
        chat_id=session_data['chat_id'],
        auth_header=jwt_token,
        trace_id=trace_id,
        user_id=session_data.get("current_user"),
    )

    answer_text = response.get("answer", "Ошибка получения ответа.")
    destination = response.get("destination", "unknown")
    error_msg = response.get("error")
    generated_report = response.get("generated_report")

    # --- АУДИТ ОТПРАВКИ СООБЩЕНИЯ ---
    # Если destination == "error" или есть поле error -> FAIL
    is_fail = (destination == "error" or error_msg is not None)

    audit_status = "FAIL" if is_fail else "SUCCESS"
    reason_text = error_msg if is_fail else None

    # Для REASON при успехе можно ничего не писать или детали
    if not is_fail:
        # Опционально: можно в REASON записать усеченный вопрос, если это помогает
        # Но по требованиям: REASON "заполнено если есть ошибка"
        pass

    log_audit_event(
        event_name="Отправка сообщения в агент",
        status=audit_status,
        user_login=session_data.get("current_user"),
        session_id=session_data.get("session_id"),
        reason=reason_text,
        # Доп поля, которые не пойдут в основной JSON аудита, но могут быть полезны в отладке,
        # если форматтер позволит. Наш AuditRawFormatter их проигнорирует,
        # так как мы передаем словарь явно внутри log_audit_event.
    )

    answer_uid = gen_id("ans_")
    new_data = session_data.copy()
    assistant_message = {"role": "assistant", "content": answer_text, "answer_uid": answer_uid}
    assistant_message["trace_id"] = response.get("_trace_id", trace_id)
    if generated_report:
        assistant_message["generated_report"] = generated_report
    new_data['ui_messages'].append(assistant_message)
    new_data['msg_counter'] = int(new_data.get('msg_counter', 0)) + 1

    loading_flag = (loading_flag or {}).copy()
    loading_flag['is_loading'] = False
    rerun_val = new_data['msg_counter']

    return new_data, loading_flag, rerun_val


@app.callback(
    Output('loading-overlay', 'children'),
    Input('loading-flag', 'data')
)
def display_loading_overlay(loading_flag):
    is_loading = loading_flag.get('is_loading', False)
    return create_loading_overlay(is_loading)


def render_chat_message(msg: Dict[str, Any], session_data: Dict[str, Any]) -> html.Div:
    role = msg["role"]
    content = msg["content"]
    ans_uid = msg.get("answer_uid")
    generated_report = msg.get("generated_report")

    message_content = html.Div(
        dcc.Markdown(content, className="p-2"),
        className=f"chat-message {'chat-user' if role == 'user' else 'chat-assistant'}"
    )

    if role == "assistant" and ans_uid:
        disliked = session_data['disliked_flags'].get(ans_uid, False)

        # Кнопки лайк/дизлайк
        feedback_buttons = dbc.Row(
            [
                dbc.Col(dbc.Button("👍",
                                   id={'type': 'like-button', 'index': ans_uid},
                                   color="success", title="Полезный ответ"), width="auto"),
                dbc.Col(dbc.Button("👎",
                                   id={'type': 'dislike-button', 'index': ans_uid},
                                   color="danger", title="Неполезный ответ"), width="auto"),
                dbc.Col(width=True)
            ],
            className="chat-feedback g-1 mt-1 mb-2",
            justify="start"
        )

        report_button = html.Div()
        if generated_report and generated_report.get("kind") == "incorrect_deals_report":
            report_title = generated_report.get("title") or "Выгрузить отчет XLSX"
            report_dt = generated_report.get("report_dt")
            chat_id = (session_data or {}).get("chat_id")
            report_href = None
            if report_dt and chat_id:
                report_href = f"{DOWNLOAD_REPORT_PATH}?{urlencode({'chat_id': chat_id, 'report_dt': report_dt})}"

            if report_href:
                report_button = html.A(
                    "Скачать XLSX",
                    href=report_href,
                    target="_self",
                    title=report_title,
                    className="btn btn-secondary btn-sm me-2 mb-2",
                )
            else:
                report_button = dbc.Button(
                    "Скачать XLSX",
                    color="secondary",
                    size="sm",
                    title=report_title,
                    className="me-2 mb-2",
                    disabled=True,
                )

            # report_button = dbc.Button(
            #     "Скачать XLSX",
            #     href=report_href,
            #     color="secondary",
            #     size="sm",
            #     title=report_title,
            #     className="me-2 mb-2",
            #     disabled=not bool(report_href),
            # )

        feedback_form = html.Div()
        if disliked:
            feedback_form = html.Div([
                dbc.Textarea(
                    id={'type': 'feedback-text', 'index': ans_uid},
                    placeholder="Опишите, что не так с ответом",
                    className="mb-2"
                ),
                dbc.Button(
                    "Отправить фидбек",
                    id={'type': 'feedback-submit-button', 'index': ans_uid},
                    color="primary",
                    size="sm"
                )
            ])

        return html.Div(
            [message_content, report_button, feedback_buttons, feedback_form],
            className="chat-feedback-container",
        )

    return message_content


@app.callback(
    Output("chat-history-container", "children"),
    Input("session-store", "data"),
    Input("rerun-trigger", "data")
)
def display_chat_history(session_data, _):
    messages = session_data.get("ui_messages", [])
    chat_elements = [render_chat_message(m, session_data) for m in messages]
    chat_elements.append(html.Div(id="scroll-anchor"))
    return chat_elements


# Callbacks для лайков/дизлайков (Аудит для них не требовался в новом формате,
# но при желании можно добавить аналогичный вызов log_audit_event)
@app.callback(
    [Output('session-store', 'data', allow_duplicate=True),
     Output('rerun-trigger', 'data', allow_duplicate=True),
     Output({'type': 'feedback-text', 'index': ALL}, 'value')],
    [Input({'type': 'like-button', 'index': ALL}, 'n_clicks'),
     Input({'type': 'dislike-button', 'index': ALL}, 'n_clicks'),
     Input({'type': 'feedback-submit-button', 'index': ALL}, 'n_clicks')],
    [State('session-store', 'data'),
     State({'type': 'feedback-text', 'index': ALL}, 'value')],
    prevent_initial_call=True
)
def handle_feedback(like_clicks, dislike_clicks, submit_clicks, session_data, feedback_values):
    ctx = callback_context
    num_textareas = len(feedback_values or [])
    no_update_values = [dash.no_update] * num_textareas

    if not ctx.triggered:
        return dash.no_update, dash.no_update, no_update_values

    triggered_id = ctx.triggered[0]['prop_id']
    try:
        id_info = json.loads(triggered_id.split('.')[0])
    except Exception:
        return dash.no_update, dash.no_update, no_update_values

    button_type = id_info.get('type')
    ans_uid = id_info.get('index')
    if not ans_uid:
        return dash.no_update, dash.no_update, no_update_values

    new_data = session_data.copy()
    rerun = False
    new_textarea_values = no_update_values

    val = (ctx.triggered[0].get("value") or 0)
    clicked = val > 0

    id_dicts_from_state = []
    if len(ctx.states_list) > 1 and ctx.states_list[1] is not None:
        id_dicts_from_state = [comp['id'] for comp in ctx.states_list[1]]
    feedback_map = {
        state_id.get('index'): value
        for state_id, value in zip(id_dicts_from_state, (feedback_values or []))
        if isinstance(state_id, dict) and 'index' in state_id
    }

    if button_type == "like-button" and clicked:
        # Старый логгер можно оставить для отладки, или заменить на новый, если нужно
        logger.info("User liked answer: %s", ans_uid)
        rerun = True

    elif button_type == "dislike-button" and clicked:
        if new_data['disliked_flags'].get(ans_uid):
            return dash.no_update, dash.no_update, no_update_values
        new_data['disliked_flags'][ans_uid] = True
        rerun = True

    elif button_type == "feedback-submit-button" and clicked:
        feedback = feedback_map.get(ans_uid, "") or ""
        logger.info("User disliked answer: %s with feedback: '%s'", ans_uid, feedback)

        new_data['disliked_flags'][ans_uid] = False
        rerun = True

        new_values_list = []
        for state_id, val in zip(id_dicts_from_state, (feedback_values or [])):
            if state_id.get('index') == ans_uid:
                new_values_list.append("")
            else:
                new_values_list.append(val)
        new_textarea_values = new_values_list

    if rerun:
        new_rerun_val = int(session_data.get('msg_counter', 0)) + 1
        return new_data, new_rerun_val, new_textarea_values

    return dash.no_update, dash.no_update, no_update_values


app.clientside_callback(
    f"""
    function(text_value) {{
        const max_chars = {MAX_INPUT_CHARS};
        const current_len = text_value ? text_value.length : 0;
        const remaining = max_chars - current_len;

        let message = "";
        let style = {{}};
        let button_disabled = false;

        // Логика отображения
        if (current_len === 0) {{
            // Пусто - ничего не показываем или показываем подсказку
            message = "";
        }} else if (remaining < 0) {{
             // Это состояние вряд ли наступит из-за maxLength, но на всякий случай
            message = "⚠️ Превышен лимит символов: " + current_len + " / " + max_chars;
            style = {{"color": "#dc3545", "fontWeight": "bold"}}; // Красный
            button_disabled = true;
        }} else if (remaining < 2000) {{
            // Подходим к лимиту (осталось меньше 2000) - предупреждаем
            message = current_len + " / " + max_chars;
            style = {{"color": "#fd7e14"}}; // Оранжевый
        }} else {{
            // Обычное состояние (по желанию можно скрывать, если мало символов)
            // Покажем серым, если введено больше 100 символов, чтобы не мельтешило
            if (current_len > 100) {{
                message = current_len + " / " + max_chars;
            }}
        }}

        return [message, style, button_disabled];
    }}
    """,
    [Output("char-counter-display", "children"),
     Output("char-counter-display", "style"),
     Output("send-button", "disabled")],
    [Input("user-input", "value")],
    prevent_initial_call=False
)


if __name__ == '__main__':
    app.run(host=HOST, port=PORT, debug=False)
