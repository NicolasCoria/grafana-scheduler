import re
import os
import hmac
import secrets
from urllib.parse import urljoin

import requests
from flask import jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

from database import (
    enqueue_report_job,
    create_api_token,
    create_grafana_server,
    delete_ai_prompt,
    delete_api_token,
    create_schedule,
    delete_grafana_server,
    delete_report_template,
    delete_schedule,
    find_api_token_by_plaintext,
    generate_api_token_value,
    get_ai_config,
    get_ai_prompt,
    get_email_config,
    get_execution_config,
    get_grafana_server,
    get_schedule,
    get_report_execution,
    get_report_template,
    get_status_dashboard_data,
    get_telegram_bot,
    list_ai_prompts,
    list_api_tokens,
    list_grafana_servers,
    list_report_templates,
    list_schedules,
    list_telegram_bots,
    normalize_selected_targets,
    purge_report_queue,
    save_ai_config,
    save_ai_prompt,
    save_email_config,
    save_execution_config,
    save_report_template,
    save_telegram_bot,
    delete_telegram_bot,
    update_grafana_server,
    update_schedule,
    update_schedule_report_config,
)
from app_config import AI_PROVIDER_DEFAULT_MODELS, normalize_ai_model
from encryption import decrypt_password, encrypt_password


VALID_AI_PROVIDERS = {"openai", "azure", "claude"}
VALID_PERIODS = {"diario", "semanal", "mensal"}
SESSION_USER_KEY = "authenticated_user"
CSRF_SESSION_KEY = "csrf_token"
CSRF_EXEMPT_PATHS = {"/login"}
PUBLIC_API_PREFIX = "/api/v1/"


def _get_admin_username():
    return os.getenv("ADMIN_USERNAME", "").strip()


def _get_admin_password_hash():
    return os.getenv("ADMIN_PASSWORD_HASH", "").strip()


def _get_admin_password_plaintext():
    return os.getenv("ADMIN_PASSWORD", "").strip()


def _admin_auth_configured():
    return bool(_get_admin_username() and (_get_admin_password_hash() or _get_admin_password_plaintext()))


def _verify_admin_credentials(username, password):
    configured_username = _get_admin_username()
    if not configured_username or username != configured_username:
        return False

    password_hash = _get_admin_password_hash()
    if password_hash:
        return check_password_hash(password_hash, password)

    plaintext = _get_admin_password_plaintext()
    if plaintext:
        return hmac.compare_digest(plaintext, password)
    return False


def _get_csrf_token():
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


def _is_authenticated():
    return bool(session.get(SESSION_USER_KEY))


def _is_safe_redirect_target(target):
    if not target or not target.startswith("/"):
        return False
    from urllib.parse import urlparse, unquote
    decoded = unquote(target)
    parsed = urlparse(decoded)
    return not parsed.netloc and not parsed.scheme and not decoded.startswith("//")


def _json_error(message, status=400):
    return jsonify({"status": "error", "message": message}), status


def _normalize_ai_provider(provider, fallback="openai"):
    normalized = str(provider or "").strip().lower()
    if normalized in VALID_AI_PROVIDERS:
        return normalized
    return fallback


def _serialize_email_config():
    config = get_email_config()
    return {
        "smtp_server": config["smtp_server"],
        "smtp_port": config["smtp_port"],
        "smtp_username": config["smtp_username"],
        "smtp_from_email": config["smtp_from_email"],
        "smtp_use_tls": bool(config["smtp_use_tls"]),
        "password_configured": bool(config["smtp_password"]),
    }


def _serialize_telegram_config():
    bots = list_telegram_bots()
    return {
        "bots": [
            {
                "id": bot["id"],
                "nome": bot["nome"],
                "token_configured": bool(bot["bot_token"]),
                "selected_chats": bot["selected_chats"],
            }
            for bot in bots
        ],
        "bot_count": len(bots),
        "chat_count": sum(len(bot["selected_chats"]) for bot in bots),
    }


def _serialize_ai_config():
    config = get_ai_config()
    raw_provider = str(config.get("provider", "")).strip().lower()
    provider_is_supported = raw_provider in VALID_AI_PROVIDERS
    provider = raw_provider if provider_is_supported else "openai"
    return {
        "provider": provider,
        "endpoint": config["endpoint"] if provider == "azure" else "",
        "model": normalize_ai_model(provider, config.get("model", "")),
        "api_key_configured": bool(config["api_key"]) and provider_is_supported,
        "provider_needs_review": bool(raw_provider) and not provider_is_supported,
        "original_provider": raw_provider,
    }


def _serialize_ai_prompts():
    prompts = list_ai_prompts()
    return {
        "prompts": [
            {
                "id": item["id"],
                "titulo": item["titulo"],
                "prompt_text": item["prompt_text"],
            }
            for item in prompts
        ]
    }


def _serialize_execution_config():
    config = get_execution_config()
    return {
        "max_concurrent_reports": int(config.get("max_concurrent_reports", 5) or 5),
        "min_allowed": 1,
        "max_allowed": 100,
    }


def _serialize_schedules():
    ai_config = _serialize_ai_config()
    fallback_provider = ai_config["provider"]
    schedules = []
    for item in list_schedules():
        serialized = dict(item)
        serialized["ai_provider"] = (
            _normalize_ai_provider(serialized.get("ai_provider"), fallback_provider)
            if serialized.get("use_ai")
            else ""
        )
        schedules.append(serialized)
    return schedules


def _serialize_report_templates():
    templates = list_report_templates()
    return {
        "templates": [
            {
                "id": item["id"],
                "nome": item["nome"],
                "show_summary": bool(item["show_summary"]),
                "header_text": item["header_text"],
                "primary_color": item["primary_color"],
                "secondary_color": item["secondary_color"],
                "font_family": item["font_family"],
                "title_font_size": item["title_font_size"],
                "body_font_size": item["body_font_size"],
                "logo_base64": item["logo_base64"],
            }
            for item in templates
        ]
    }


def _serialize_api_access_config():
    tokens = list_api_tokens()
    return {
        "token_configured": bool(tokens),
        "tokens": [
            {
                "id": item["id"],
                "nome": item["nome"],
                "created_at": item["created_at"],
                "updated_at": item["updated_at"],
            }
            for item in tokens
        ],
    }


def _parse_days_arg():
    try:
        days = int(request.args.get("days", "7"))
    except ValueError:
        days = 7
    if days not in {1, 7, 30}:
        return 7
    return days


def _build_status_view_model(days):
    status_data = get_status_dashboard_data(days=days)
    return {
        "days": days,
        "status_data": status_data,
        "duration_metrics": status_data["duration_metrics"],
        "slowest_reports": status_data["slowest_reports"],
    }


def _build_metrics_api_payload(days):
    status_data = get_status_dashboard_data(days=days)
    return {
        "days": days,
        "timeseries_unit": status_data["timeseries_unit"],
        "timeseries_counts": status_data["timeseries_counts"],
        "timeseries_failure_counts": status_data["timeseries_failure_counts"],
        "timeseries_execution_counts": status_data["timeseries_execution_counts"],
        "worker_usage_timeseries": status_data["worker_usage_timeseries"],
        "window_metrics": status_data["window_metrics"],
        "status_metrics": status_data["status_metrics"],
        "delivery_metrics": status_data["delivery_metrics"],
        "duration_metrics": status_data["duration_metrics"],
        "slowest_reports": status_data["slowest_reports"],
        "totals": {
            "executions": status_data["total_executions"],
            "reports": status_data["total_reports"],
            "failures": status_data["total_failures"],
        },
    }


def _normalize_period(request_like):
    periodo = request_like.get("periodo", "").strip().lower()
    if periodo not in VALID_PERIODS:
        raise ValueError("Periodo inválido.")

    if periodo == "diario":
        return periodo, "", request_like.get("horario_diario", "").strip()
    if periodo == "semanal":
        return periodo, request_like.get("dia_semana", "").strip(), request_like.get("horario_semanal", "").strip()
    return periodo, request_like.get("dia_mes", "").strip(), request_like.get("horario_mensal", "").strip()


def _validate_email_list(form):
    emails = [value.strip() for value in form.getlist("emails[]") if value.strip()]
    if not emails:
        raise ValueError("Informe por lo menos un destinatario de e-mail.")

    email_regex = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    for email in emails:
        if not email_regex.match(email):
            raise ValueError(f"E-mail inválido: {email}")
    return emails


_TELEGRAM_TOKEN_RE = re.compile(r"^\d{8,12}:[A-Za-z0-9_-]{35,}$")


def _fetch_telegram_chats(bot_token):
    if not bot_token or not _TELEGRAM_TOKEN_RE.match(bot_token):
        raise ValueError("Formato de token Telegram inválido.")
    response = requests.get(
        f"https://api.telegram.org/bot{bot_token}/getUpdates",
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise ValueError(payload.get("description", "Fallo al consultar Telegram."))

    chats = {}
    for update in payload.get("result", []):
        message = update.get("message") or update.get("channel_post") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            continue

        title = chat.get("title") or " ".join(
            value for value in [chat.get("first_name"), chat.get("last_name")] if value
        ).strip()
        username = chat.get("username")
        label_parts = [title or "Sin nombre"]
        if username:
            label_parts.append(f"@{username}")

        chats[str(chat_id)] = {
            "chat_id": str(chat_id),
            "name": " | ".join(label_parts),
            "type": chat.get("type", "unknown"),
        }

    return list(chats.values())


def _normalize_base_url(base_url):
    return base_url.rstrip("/")


def _grafana_basic_auth(server):
    username = (server.get("username") or "").strip()
    password = server.get("password") or ""
    if username and password:
        return (username, password)
    return None


def _request_grafana_json(server, path, params=None, timeout=30):
    base_url = _normalize_base_url(server["base_url"])
    url = f"{base_url}{path}"
    errors = []

    token = (server.get("service_account_token") or "").strip()
    auth_attempts = []
    if token:
        auth_attempts.append(
            {
                "headers": {
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
                "auth": None,
                "label": "service-account-token",
            }
        )

    basic_auth = _grafana_basic_auth(server)
    if basic_auth:
        auth_attempts.append(
            {
                "headers": {"Accept": "application/json"},
                "auth": basic_auth,
                "label": "basic-auth",
            }
        )

    if not auth_attempts:
        raise ValueError("Ninguna credencial válida fue configurada para consultar la API de Grafana.")

    for attempt in auth_attempts:
        try:
            response = requests.get(
                url,
                headers=attempt["headers"],
                auth=attempt["auth"],
                params=params,
                timeout=timeout,
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            errors.append(f"{attempt['label']}: {exc}")

    raise requests.RequestException("; ".join(errors))


def _build_grafana_catalog(server):
    base_url = _normalize_base_url(server["base_url"])
    items = _request_grafana_json(server, "/api/search", params={"limit": 5000}, timeout=30)

    folders = {}
    dashboards_without_folder = []

    for item in items:
        if item.get("type") == "dash-folder":
            folder_uid = item.get("uid") or str(item.get("id"))
            folders[folder_uid] = {
                "uid": folder_uid,
                "title": item.get("title", "Sin nombre"),
                "url": urljoin(base_url + "/", item.get("url", "").lstrip("/")),
                "dashboards": [],
            }

    for item in items:
        if item.get("type") != "dash-db":
            continue

        dashboard = {
            "uid": item.get("uid"),
            "title": item.get("title", "Sin nombre"),
            "url": urljoin(base_url + "/", item.get("url", "").lstrip("/")),
            "folderUid": item.get("folderUid") or "",
            "folderTitle": item.get("folderTitle") or "",
        }

        folder_uid = dashboard["folderUid"]
        if folder_uid and folder_uid in folders:
            folders[folder_uid]["dashboards"].append(dashboard)
        else:
            dashboards_without_folder.append(dashboard)

    return {
        "server": {"id": server["id"], "nome": server["nome"], "base_url": base_url},
        "folders": list(folders.values()),
        "dashboards_without_folder": dashboards_without_folder,
    }


def setup_routes(app):
    @app.context_processor
    def inject_auth_context():
        return {
            "csrf_token": _get_csrf_token() if _is_authenticated() else "",
            "current_user": session.get(SESSION_USER_KEY, ""),
        }

    @app.before_request
    def enforce_security():
        endpoint = request.endpoint or ""
        path = request.path or ""

        if endpoint == "static" or path.startswith(PUBLIC_API_PREFIX):
            return None

        if not _admin_auth_configured():
            if endpoint == "login_page":
                return None
            if path.startswith("/api/"):
                return _json_error(
                    "Configure ADMIN_USERNAME y ADMIN_PASSWORD o ADMIN_PASSWORD_HASH antes de usar la aplicación.",
                    503,
                )
            return redirect(url_for("login_page"))

        if endpoint == "login_page":
            return None

        if not _is_authenticated():
            if path.startswith("/api/"):
                return _json_error("Autenticación necesaria.", 401)
            next_target = request.full_path if request.method == "GET" else request.path
            return redirect(url_for("login_page", next=next_target))

        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and path not in CSRF_EXEMPT_PATHS:
            provided_token = request.headers.get("X-CSRF-Token", "").strip() or request.form.get("csrf_token", "").strip()
            expected_token = session.get(CSRF_SESSION_KEY, "")
            if not provided_token or not expected_token or not hmac.compare_digest(provided_token, expected_token):
                if path.startswith("/api/"):
                    return _json_error("Token CSRF inválido.", 403)
                return redirect(url_for("dashboard_page"))

    def _extract_api_token():
        auth_header = request.headers.get("Authorization", "").strip()
        if auth_header.lower().startswith("bearer "):
            return auth_header[7:].strip()
        return request.headers.get("X-API-Token", "").strip()

    def _require_api_token():
        token_value = _extract_api_token()
        if not list_api_tokens():
            return _json_error("Token de API no configurado.", 503)
        if not token_value:
            return _json_error("Token de API inválido.", 401)
        if not find_api_token_by_plaintext(token_value, decrypt_password):
            return _json_error("Token de API inválido.", 401)
        return None

    def _queue_schedule_run(schedule, trigger_source, requested_by=""):
        return enqueue_report_job(
            schedule_id=schedule["id"],
            trigger_source=trigger_source,
            requested_by=requested_by,
        )

    def _build_schedule_recipients(delivery_methods, request_form):
        recipients = []
        email_recipients = []
        if "email" in delivery_methods:
            email_recipients.extend(_validate_email_list(request_form))
            recipients.extend({"tipo": "email", "valor": email, "label": email} for email in email_recipients)

        telegram_bots = list_telegram_bots()
        if "telegram" in delivery_methods:
            active_chats = [chat for bot in telegram_bots for chat in bot["selected_chats"]]
            if not active_chats:
                raise ValueError("Telegram seleccionado, pero ningún chat fue configurado.")
            for bot in telegram_bots:
                for chat in bot["selected_chats"]:
                    recipients.append(
                        {
                            "tipo": "telegram",
                            "valor": str(chat["chat_id"]),
                            "label": chat.get("name", str(chat["chat_id"])),
                            "metadata": {"bot_id": bot["id"], "bot_name": bot["nome"]},
                        }
                    )
        return recipients

    @app.route("/login", methods=["GET", "POST"])
    def login_page():
        if request.method == "GET":
            if _is_authenticated():
                return redirect(url_for("dashboard_page"))
            return render_template("login.html", auth_configured=_admin_auth_configured(), next_url=request.args.get("next", ""))

        if not _admin_auth_configured():
            return render_template("login.html", auth_configured=False, error_message="Configure ADMIN_USERNAME y ADMIN_PASSWORD o ADMIN_PASSWORD_HASH antes de entrar."), 503

        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        next_url = request.form.get("next", "").strip()

        if not _verify_admin_credentials(username, password):
            return render_template(
                "login.html",
                auth_configured=True,
                error_message="Usuario o contraseña inválidos.",
                next_url=next_url,
            ), 401

        session.clear()
        session.permanent = True
        session[SESSION_USER_KEY] = username
        _get_csrf_token()
        if not _is_safe_redirect_target(next_url):
            next_url = url_for("dashboard_page")
        return redirect(next_url)

    @app.route("/logout", methods=["POST"])
    def logout_route():
        session.clear()
        return redirect(url_for("login_page"))

    @app.route("/")
    def home():
        return redirect(url_for("dashboard_page"))

    @app.route("/dashboard", methods=["GET"])
    def dashboard_page():
        view_model = _build_status_view_model(_parse_days_arg())
        return render_template("dashboard.html", **view_model)

    @app.route("/relatorios", methods=["GET"])
    def reports_page():
        view_model = _build_status_view_model(_parse_days_arg())
        return render_template("relatorios.html", **view_model)

    @app.route("/falhas", methods=["GET"])
    def failures_page():
        view_model = _build_status_view_model(_parse_days_arg())
        return render_template("falhas.html", **view_model)

    @app.route("/api", methods=["GET"])
    def api_page():
        return render_template(
            "api.html",
            api_access_config=_serialize_api_access_config(),
        )

    @app.route("/configuracoes", methods=["GET"])
    def configuration_hub_page():
        return render_template(
            "configuracoes.html",
            email_config=_serialize_email_config(),
            telegram_config=_serialize_telegram_config(),
            ai_config=_serialize_ai_config(),
            ai_prompts=_serialize_ai_prompts(),
            execution_config=_serialize_execution_config(),
            report_templates=_serialize_report_templates(),
            grafana_servers=list_grafana_servers(),
        )

    @app.route("/configuracoes/execution", methods=["GET"])
    def execution_settings_page():
        return render_template(
            "configuracao_execucao.html",
            execution_config=_serialize_execution_config(),
        )

    @app.route("/agendamentos", methods=["GET"])
    def schedule_page():
        return render_template(
            "agendamentos.html",
            schedules=_serialize_schedules(),
            grafana_servers=list_grafana_servers(),
            telegram_config=_serialize_telegram_config(),
            ai_config=_serialize_ai_config(),
            ai_prompts=_serialize_ai_prompts(),
            report_templates=_serialize_report_templates(),
        )

    def _upsert_schedule(schedule_id=None):
        try:
            titulo = request.form.get("titulo", "").strip()
            grafana_server_id = int(request.form.get("grafana_server_id", "0"))
            periodo, detalhe_periodo, horario = _normalize_period(request.form)
        except ValueError as exc:
            return _json_error(str(exc))
        except Exception:
            return _json_error("Datos de la programación inválidos.")

        grafana_server = get_grafana_server(grafana_server_id)
        if not grafana_server:
            return _json_error("Servidor Grafana no encontrado.")

        selected_targets_raw = request.form.get("selected_targets_json", "[]")
        delivery_methods_raw = request.form.get("delivery_methods_json", "[]")
        try:
            import json

            selected_targets = json.loads(selected_targets_raw)
            delivery_methods = json.loads(delivery_methods_raw)
        except Exception:
            return _json_error("Fallo al interpretar dashboards o métodos de envío.")

        if not titulo:
            return _json_error("Informe el nombre de la programación.")
        if not horario:
            return _json_error("Informe el horario de la programación.")
        selected_targets = normalize_selected_targets(selected_targets)
        if len(selected_targets) != 1:
            return _json_error("Seleccione solo un dashboard.")
        if not delivery_methods:
            return _json_error("Seleccione al menos un método de envío.")

        report_type = request.form.get("report_type", "resumido").strip().lower()
        report_subject = request.form.get("report_subject", "").strip()
        report_intro = request.form.get("report_intro", "").strip()
        report_footer = request.form.get("report_footer", "").strip()
        report_ai_instruction = request.form.get("report_ai_instruction", "").strip()
        ai_prompt_id_raw = request.form.get("ai_prompt_id", "").strip()
        report_template_id_raw = request.form.get("report_template_id", "").strip()
        use_ai = request.form.get("use_ai", "false").strip().lower() == "true"
        ai_provider = request.form.get("ai_provider", "").strip().lower() if use_ai else ""
        ai_prompt_id = None
        report_template_id = None
        if ai_prompt_id_raw:
            try:
                ai_prompt_id = int(ai_prompt_id_raw)
            except ValueError:
                return _json_error("Prompt de IA inválido.")
            if not get_ai_prompt(ai_prompt_id):
                return _json_error("Prompt de IA no encontrado.")
        if report_template_id_raw:
            try:
                report_template_id = int(report_template_id_raw)
            except ValueError:
                return _json_error("Template de reporte inválido.")
            if not get_report_template(report_template_id):
                return _json_error("Template de reporte no encontrado.")

        if report_type not in {"resumido", "detalhado"}:
            return _json_error("Tipo de reporte inválido.")
        if use_ai and ai_provider not in VALID_AI_PROVIDERS:
            return _json_error("Seleccione un proveedor de IA válido. Solo OpenAI, Azure AI Foundry y Claude son soportados.")
        if use_ai and not ai_prompt_id:
            return _json_error("Seleccione un prompt de IA.")

        try:
            recipients = _build_schedule_recipients(delivery_methods, request.form)
        except ValueError as exc:
            return _json_error(str(exc))

        schedule_payload = {
            "titulo": titulo,
            "nome_cliente": "",
            "url_dashboard": grafana_server["base_url"],
            "usuario_dashboard": "",
            "senha_dashboard": "",
            "periodo": periodo,
            "detalhe_periodo": detalhe_periodo,
            "horario": horario,
            "aplicacao": "grafana",
            "grafana_server_id": grafana_server["id"],
            "selected_targets": selected_targets,
            "delivery_methods": delivery_methods,
            "report_type": report_type,
            "report_subject": report_subject,
            "report_intro": report_intro,
            "report_footer": report_footer,
            "report_ai_instruction": report_ai_instruction,
            "ai_prompt_id": ai_prompt_id,
            "report_template_id": report_template_id,
            "use_ai": use_ai,
            "ai_provider": ai_provider,
        }

        if schedule_id is None:
            create_schedule(schedule_payload, recipients)
            return jsonify({"status": "success", "message": "Programación registrada con éxito."})

        update_schedule(schedule_id, schedule_payload, recipients)
        return jsonify({"status": "success", "message": "Programación actualizada con éxito."})

    @app.route("/api/agendamentos", methods=["POST"])
    def create_schedule_route():
        return _upsert_schedule()

    @app.route("/api/agendamentos/<int:schedule_id>", methods=["POST"])
    def update_schedule_route(schedule_id):
        schedule = next((item for item in list_schedules() if item["id"] == schedule_id), None)
        if not schedule:
            return _json_error("Programación no encontrada.", 404)
        return _upsert_schedule(schedule_id=schedule_id)

    @app.route("/api/agendamentos/<int:schedule_id>", methods=["DELETE"])
    def delete_schedule_route(schedule_id):
        delete_schedule(schedule_id)
        return jsonify({"status": "success"})

    @app.route("/api/agendamentos/<int:schedule_id>/executar", methods=["POST"])
    def execute_schedule_now_route(schedule_id):
        schedule = get_schedule(schedule_id)
        if not schedule:
            return _json_error("Programación no encontrada.", 404)
        try:
            job = _queue_schedule_run(
                schedule,
                trigger_source="manual",
                requested_by=session.get(SESSION_USER_KEY, ""),
            )
        except Exception as exc:
            return _json_error(f"Fallo al encolar programación: {exc}", 500)

        if job["created"]:
            message = "Programación encolada para ejecución."
        else:
            message = "Ya existe una ejecución en curso o pendiente para esta programación."

        return jsonify(
            {
                "status": "success",
                "message": message,
                "job": {
                    "id": job["id"],
                    "status": job["status"],
                    "created": job["created"],
                    "created_at": job["created_at"],
                },
            }
        ), 202

    @app.route("/api/agendamentos/<int:schedule_id>/relatorio", methods=["POST"])
    def update_schedule_report_route(schedule_id):
        data = request.get_json(silent=True) or {}
        report_type = str(data.get("report_type", "resumido")).strip().lower()
        report_subject = str(data.get("report_subject", "")).strip()
        report_intro = str(data.get("report_intro", "")).strip()
        report_footer = str(data.get("report_footer", "")).strip()
        report_ai_instruction = str(data.get("report_ai_instruction", "")).strip()
        ai_prompt_id = data.get("ai_prompt_id")
        report_template_id = data.get("report_template_id")
        use_ai = bool(data.get("use_ai", False))
        ai_provider = str(data.get("ai_provider", "")).strip().lower() if use_ai else ""

        schedule = next((item for item in list_schedules() if item["id"] == schedule_id), None)
        if not schedule:
            return _json_error("Programación no encontrada.", 404)
        if report_type not in {"resumido", "detalhado"}:
            return _json_error("Tipo de reporte inválido.")
        if use_ai and ai_provider not in VALID_AI_PROVIDERS:
            return _json_error("Seleccione un proveedor de IA válido. Solo OpenAI, Azure AI Foundry y Claude son soportados.")
        if use_ai and not ai_prompt_id:
            return _json_error("Seleccione un prompt de IA.")
        if ai_prompt_id not in (None, "", 0):
            try:
                ai_prompt_id = int(ai_prompt_id)
            except ValueError:
                return _json_error("Prompt de IA inválido.")
            if not get_ai_prompt(ai_prompt_id):
                return _json_error("Prompt de IA no encontrado.")
        else:
            ai_prompt_id = None
        if report_template_id not in (None, "", 0):
            try:
                report_template_id = int(report_template_id)
            except ValueError:
                return _json_error("Template de reporte inválido.")
            if not get_report_template(report_template_id):
                return _json_error("Template de reporte no encontrado.")
        else:
            report_template_id = None

        update_schedule_report_config(
            schedule_id,
            {
                "report_type": report_type,
                "report_subject": report_subject,
                "report_intro": report_intro,
                "report_footer": report_footer,
                "report_ai_instruction": report_ai_instruction,
                "ai_prompt_id": ai_prompt_id,
                "report_template_id": report_template_id,
                "use_ai": use_ai,
                "ai_provider": ai_provider,
            },
        )
        return jsonify({"status": "success", "message": "Configuración del reporte actualizada."})

    @app.route("/editar-relatorio", methods=["GET"])
    def report_templates_page():
        return render_template(
            "editar_relatorio.html",
            report_templates=_serialize_report_templates(),
        )

    @app.route("/prompts", methods=["GET"])
    def prompts_page():
        return redirect(url_for("ai_settings_page"))

    @app.route("/api/prompts", methods=["POST"])
    def save_ai_prompt_route():
        data = request.get_json(silent=True) or {}
        prompt_id = data.get("prompt_id")
        titulo = str(data.get("titulo", "")).strip()
        prompt_text = str(data.get("prompt_text", "")).strip()

        if not titulo:
            return _json_error("Informe el título del prompt.")
        if not prompt_text:
            return _json_error("Informe el texto del prompt.")

        if prompt_id not in (None, "", 0):
            try:
                prompt_id = int(prompt_id)
            except ValueError:
                return _json_error("Prompt inválido.")
            if not get_ai_prompt(prompt_id):
                return _json_error("Prompt no encontrado.", 404)
        else:
            prompt_id = None

        saved_id = save_ai_prompt({"titulo": titulo, "prompt_text": prompt_text}, prompt_id=prompt_id)
        return jsonify({"status": "success", "message": "Prompt guardado.", "prompt_id": saved_id})

    @app.route("/api/prompts/<int:prompt_id>", methods=["DELETE"])
    def delete_ai_prompt_route(prompt_id):
        delete_ai_prompt(prompt_id)
        return jsonify({"status": "success", "message": "Prompt eliminado."})

    @app.route("/api/relatorios/templates", methods=["POST"])
    def save_report_template_route():
        data = request.get_json(silent=True) or {}
        template_id = data.get("template_id")
        nome = str(data.get("nome", "")).strip()
        primary_color = str(data.get("primary_color", "#f97316")).strip() or "#f97316"
        secondary_color = str(data.get("secondary_color", "#0f172a")).strip() or "#0f172a"
        font_family = str(data.get("font_family", "Helvetica")).strip() or "Helvetica"
        title_font_size = str(data.get("title_font_size", "20")).strip()
        body_font_size = str(data.get("body_font_size", "11")).strip()

        if not nome:
            return _json_error("Informe un nombre para el template.")
        if font_family not in {"Helvetica", "Times-Roman", "Courier"}:
            return _json_error("Fuente inválida.")
        try:
            title_font_size = int(title_font_size)
            body_font_size = int(body_font_size)
        except ValueError:
            return _json_error("Tamaño de fuente inválido.")
        if title_font_size < 12 or title_font_size > 40 or body_font_size < 8 or body_font_size > 24:
            return _json_error("Los tamaños de fuente están fuera del intervalo permitido.")

        if template_id not in (None, "", 0):
            try:
                template_id = int(template_id)
            except ValueError:
                return _json_error("Template inválido.")
            current = get_report_template(template_id)
            if not current:
                return _json_error("Template no encontrado.", 404)
        else:
            template_id = None
            current = {}

        saved_id = save_report_template(
            {
                "nome": nome,
                "logo_base64": str(data.get("logo_base64", current.get("logo_base64", ""))).strip(),
                "cover_base64": "",
                "back_cover_base64": "",
                "show_summary": bool(data.get("show_summary", True)),
                "header_text": str(data.get("header_text", "")).strip(),
                "primary_color": primary_color,
                "secondary_color": secondary_color,
                "font_family": font_family,
                "title_font_size": title_font_size,
                "body_font_size": body_font_size,
            },
            template_id=template_id,
        )
        return jsonify({"status": "success", "message": "Template guardado.", "template_id": saved_id})

    @app.route("/api/relatorios/templates/<int:template_id>", methods=["DELETE"])
    def delete_report_template_route(template_id):
        delete_report_template(template_id)
        return jsonify({"status": "success", "message": "Template eliminado."})

    @app.route("/configuracoes/envio", methods=["GET"])
    def delivery_settings_page():
        return render_template(
            "configuracoes_envio.html",
            email_config=_serialize_email_config(),
            telegram_config=_serialize_telegram_config(),
        )

    @app.route("/api/configuracoes/email", methods=["POST"])
    def save_email_settings_route():
        data = request.get_json(silent=True) or {}
        smtp_server = data.get("smtp_server", "").strip()
        smtp_port = str(data.get("smtp_port", "")).strip()
        smtp_username = data.get("smtp_username", "").strip()
        smtp_password = data.get("smtp_password", "")
        smtp_from_email = data.get("smtp_from_email", "").strip()
        smtp_use_tls = 1 if data.get("smtp_use_tls", True) else 0

        if not smtp_server or not smtp_port or not smtp_username or not smtp_from_email:
            return _json_error("Llene servidor, puerto, usuario y remitente.")

        try:
            normalized_port = int(smtp_port)
        except ValueError:
            return _json_error("Informe un puerto SMTP válido.")

        if normalized_port < 1 or normalized_port > 65535:
            return _json_error("El puerto SMTP debe estar entre 1 y 65535.")

        current = get_email_config()
        encrypted_password = current["smtp_password"]
        if smtp_password:
            encrypted_password = encrypt_password(smtp_password)

        save_email_config(
            {
                "smtp_server": smtp_server,
                "smtp_port": normalized_port,
                "smtp_username": smtp_username,
                "smtp_password": encrypted_password,
                "smtp_from_email": smtp_from_email,
                "smtp_use_tls": smtp_use_tls,
            }
        )
        return jsonify({"status": "success", "message": "Configuración de e-mail guardada."})

    @app.route("/api/configuracoes/telegram/chats", methods=["POST"])
    def telegram_chats_route():
        data = request.get_json(silent=True) or {}
        bot_token = data.get("bot_token", "").strip()
        if not bot_token:
            return _json_error("Informe el Bot Token.")

        try:
            chats = _fetch_telegram_chats(bot_token)
        except requests.RequestException as exc:
            return _json_error(f"Error al consultar Telegram: {exc}", 502)
        except ValueError as exc:
            return _json_error(str(exc))

        return jsonify({"status": "success", "chats": chats})

    @app.route("/api/configuracoes/telegram", methods=["POST"])
    def save_telegram_settings_route():
        data = request.get_json(silent=True) or {}
        bot_id = data.get("bot_id")
        nome = data.get("nome", "").strip()
        bot_token = data.get("bot_token", "").strip()
        selected_chats = data.get("selected_chats", [])

        if not nome:
            return _json_error("Ingrese un nombre para el bot.")
        if not selected_chats:
            return _json_error("Seleccione al menos un chat.")

        current = get_telegram_bot(bot_id) if bot_id else None
        encrypted_token = current["bot_token"] if current else ""
        if bot_token:
            encrypted_token = encrypt_password(bot_token)
        if not encrypted_token:
            return _json_error("Ingrese el Bot Token para la primera configuración.")

        normalized_chats = []
        for chat in selected_chats:
            chat_id = str(chat.get("chat_id", "")).strip()
            name = str(chat.get("name", "")).strip()
            if not chat_id:
                return _json_error("Todos los chats seleccionados deben tener chat_id.")
            normalized_chats.append({"chat_id": chat_id, "name": name})

        saved_id = save_telegram_bot(nome, encrypted_token, normalized_chats, bot_id=bot_id)
        return jsonify({"status": "success", "message": "Configuración de Telegram guardada.", "bot_id": saved_id})

    @app.route("/api/configuracoes/telegram/<int:bot_id>", methods=["DELETE"])
    def delete_telegram_settings_route(bot_id):
        delete_telegram_bot(bot_id)
        return jsonify({"status": "success", "message": "Bot de Telegram eliminado."})

    @app.route("/configuracoes/ia", methods=["GET"])
    def ai_settings_page():
        return render_template(
            "configuracoes_ia.html",
            ai_config=_serialize_ai_config(),
            ai_prompts=_serialize_ai_prompts(),
        )

    @app.route("/api/configuracoes/ia", methods=["POST"])
    def save_ai_settings_route():
        data = request.get_json(silent=True) or {}
        provider = data.get("provider", "").strip().lower()
        api_key = data.get("api_key", "")
        endpoint = data.get("endpoint", "").strip()
        model = normalize_ai_model(provider, data.get("model", "").strip())

        if provider not in VALID_AI_PROVIDERS:
            return _json_error("Proveedor de IA inválido. Solo se admiten OpenAI, Azure AI Foundry y Claude.")
        if provider == "azure" and not endpoint:
            return _json_error("Azure AI Foundry exige endpoint.")
        if provider in {"openai", "claude"}:
            endpoint = ""

        current = get_ai_config()
        encrypted_api_key = current["api_key"]
        if api_key:
            encrypted_api_key = encrypt_password(api_key)
        if not encrypted_api_key:
            return _json_error("Ingrese la API Key para guardar la configuración.")

        save_ai_config(
            {
                "provider": provider,
                "api_key": encrypted_api_key,
                "endpoint": endpoint,
                "model": model,
            }
        )
        return jsonify({"status": "success", "message": "Credenciales de IA guardadas."})

    @app.route("/api/configuracoes/execution", methods=["POST"])
    def save_execution_settings_route():
        data = request.get_json(silent=True) or {}
        max_concurrent_reports = data.get("max_concurrent_reports")

        try:
            normalized_value = int(max_concurrent_reports)
        except (TypeError, ValueError):
            return _json_error("Ingrese una cantidad válida de reportes simultáneos.")

        if normalized_value < 1 or normalized_value > 100:
            return _json_error("La cantidad de reportes simultáneos debe estar entre 1 y 100.")

        save_execution_config({"max_concurrent_reports": normalized_value})
        return jsonify(
            {
                "status": "success",
                "message": "Cantidad de reportes simultáneos actualizada.",
                "max_concurrent_reports": normalized_value,
            }
        )

    @app.route("/api/configuracoes/execution/purge", methods=["POST"])
    def purge_execution_queue_route():
        result = purge_report_queue("Cola cancelada manualmente por el operador.")
        return jsonify(
            {
                "status": "success",
                "message": (
                    f"Cola limpiada con éxito. "
                    f"{result['queued_removed']} trabajo(s) pendiente(s) eliminado(s) y "
                    f"{result['running_cancelled']} trabajo(s) en ejecución marcado(s) para cancelación."
                ),
                "queued_removed": result["queued_removed"],
                "running_cancelled": result["running_cancelled"],
            }
        )

    @app.route("/api/configuracoes/ia/teste", methods=["POST"])
    def test_ai_settings_route():
        data = request.get_json(silent=True) or {}
        provider = str(data.get("provider", "")).strip().lower()
        api_key = str(data.get("api_key", ""))
        endpoint = str(data.get("endpoint", "")).strip()
        model = str(data.get("model", "")).strip()

        if provider not in VALID_AI_PROVIDERS:
            return _json_error("Proveedor de IA inválido. Solo se admiten OpenAI, Azure AI Foundry y Claude.")

        current = get_ai_config()
        effective_api_key = api_key.strip()
        if not effective_api_key and current.get("api_key"):
            try:
                effective_api_key = decrypt_password(current["api_key"])
            except Exception:
                effective_api_key = ""

        if not effective_api_key:
            return _json_error("Ingrese la API Key o guarde una credencial válida antes de probar.")

        if provider == "azure":
            effective_endpoint = endpoint or str(current.get("endpoint", "")).strip()
            if not effective_endpoint:
                return _json_error("Azure AI Foundry exige endpoint.")
        else:
            effective_endpoint = ""

        effective_model = normalize_ai_model(
            provider,
            model or str(current.get("model", "")).strip() or AI_PROVIDER_DEFAULT_MODELS[provider],
        )

        try:
            from report_runner import call_ai_provider

            response_text = call_ai_provider(
                prompt_text="Responde solo con OK para validar la conexión.",
                provider=provider,
                model=effective_model,
                api_key=effective_api_key,
                endpoint=effective_endpoint,
                context_label=f"teste-conexao-{provider}",
                max_output_tokens=60,
            ).strip()
        except Exception as exc:
            return _json_error(f"Error al validar la conexión con la IA: {exc}", 502)

        if not response_text:
            return _json_error("La IA respondió sin contenido. Revise la credencial e intente nuevamente.", 502)

        return jsonify(
            {
                "status": "success",
                "message": "Conexión con la IA validada con éxito.",
                "response_preview": response_text[:240],
            }
        )

    @app.route("/servidores-grafana", methods=["GET"])
    def grafana_servers_page():
        return render_template("servidores_grafana.html", grafana_servers=list_grafana_servers())

    @app.route("/status", methods=["GET"])
    def status_page():
        return redirect(url_for("dashboard_page"))

    @app.route("/status/falhas/<int:execution_id>", methods=["GET"])
    def failure_detail_page(execution_id):
        execution = get_report_execution(execution_id)
        if not execution or execution["status"] != "failed":
            return _json_error("Fallo no encontrado.", 404)
        return render_template("failure_detail.html", execution=execution)

    @app.route("/api/configuracoes/api-token", methods=["POST"])
    def create_api_token_route():
        data = request.get_json(silent=True) or {}
        nome = str(data.get("nome", "")).strip()
        if not nome:
            return _json_error("Ingrese un nombre para el token.")
        plaintext_token = generate_api_token_value()
        token_id = create_api_token(nome, encrypt_password(plaintext_token))
        return jsonify(
            {
                "status": "success",
                "message": "Token de API generado.",
                "token": {
                    "id": token_id,
                    "nome": nome,
                    "value": plaintext_token,
                },
            }
        )

    @app.route("/api/configuracoes/api-token/<int:token_id>", methods=["DELETE"])
    def delete_api_token_route(token_id):
        delete_api_token(token_id)
        return jsonify({"status": "success", "message": "Token eliminado."})

    @app.route("/api/v1/metrics", methods=["GET"])
    def metrics_api_route():
        auth_error = _require_api_token()
        if auth_error:
            return auth_error
        return jsonify({"status": "success", "data": _build_metrics_api_payload(days=_parse_days_arg())})

    @app.route("/api/v1/relatorios/status", methods=["GET"])
    def reports_status_api_route():
        auth_error = _require_api_token()
        if auth_error:
            return auth_error
        status_data = get_status_dashboard_data(days=_parse_days_arg())
        return jsonify(
            {
                "status": "success",
                "data": {
                    "days": _parse_days_arg(),
                    "reports": [
                        {
                            "id": item["id"],
                            "created_at": item["created_at"],
                            "schedule_title": item["schedule_title"],
                            "status": item["status"],
                            "report_count": item["report_count"],
                            "sent_email_count": item["sent_email_count"],
                            "sent_telegram_count": item["sent_telegram_count"],
                        }
                        for item in status_data["report_rows"]
                    ],
                },
            }
        )

    @app.route("/api/v1/agendamentos/<int:schedule_id>/enviar", methods=["POST"])
    def send_schedule_api_route(schedule_id):
        auth_error = _require_api_token()
        if auth_error:
            return auth_error
        schedule = get_schedule(schedule_id)
        if not schedule:
            return _json_error("Programación no encontrada.", 404)
        try:
            job = _queue_schedule_run(schedule, trigger_source="api", requested_by="api")
        except Exception as exc:
            return _json_error(f"Fallo al encolar programación: {exc}", 500)

        if job["created"]:
            message = "Programación encolada para ejecución."
        else:
            message = "Ya existe una ejecución en curso o pendiente para esta programación."

        return jsonify(
            {
                "status": "success",
                "message": message,
                "job": {
                    "id": job["id"],
                    "status": job["status"],
                    "created": job["created"],
                    "created_at": job["created_at"],
                },
            }
        ), 202

    @app.route("/api/servidores-grafana", methods=["POST"])
    def create_grafana_server_route():
        data = request.get_json(silent=True) or {}
        server_id = data.get("server_id")
        nome = data.get("nome", "").strip()
        base_url = data.get("base_url", "").strip().rstrip("/")
        username = data.get("username", "").strip()
        password = data.get("password", "")
        service_account_token = data.get("service_account_token", "")

        if not nome or not base_url or not username:
            return _json_error("Complete el nombre, URL y usuario del servidor.")

        existing_server = None
        if server_id:
            try:
                server_id = int(server_id)
            except (TypeError, ValueError):
                return _json_error("Servidor Grafana inválido.")
            existing_server = get_grafana_server(server_id)
            if not existing_server:
                return _json_error("Servidor Grafana no encontrado.", 404)

        if not existing_server and not password:
            return _json_error("Al crear un servidor, ingrese la contraseña. El token de cuenta de servicio es opcional.")

        encrypted_password = (
            encrypt_password(password)
            if password
            else existing_server["password"]
        )
        encrypted_token = (
            encrypt_password(service_account_token)
            if service_account_token
            else existing_server["service_account_token"]
        )

        payload = {
            "nome": nome,
            "base_url": base_url,
            "username": username,
            "password": encrypted_password,
            "service_account_token": encrypted_token,
        }

        if existing_server:
            update_grafana_server(server_id, payload)
            return jsonify({"status": "success", "message": "Servidor Grafana actualizado.", "server_id": server_id})

        created_server_id = create_grafana_server(payload)
        return jsonify({"status": "success", "message": "Servidor Grafana guardado.", "server_id": created_server_id})

    @app.route("/api/servidores-grafana/<int:server_id>", methods=["DELETE"])
    def delete_grafana_server_route(server_id):
        server = get_grafana_server(server_id)
        if not server:
            return _json_error("Servidor Grafana no encontrado.", 404)
        delete_grafana_server(server_id)
        return jsonify({"status": "success", "message": "Servidor Grafana eliminado."})

    @app.route("/api/servidores-grafana/<int:server_id>/catalogo", methods=["GET"])
    def grafana_server_catalog_route(server_id):
        server = get_grafana_server(server_id)
        if not server:
            return _json_error("Servidor Grafana no encontrado.", 404)

        try:
            server["password"] = decrypt_password(server["password"])
            server["service_account_token"] = decrypt_password(server["service_account_token"])
            catalog = _build_grafana_catalog(server)
        except requests.RequestException as exc:
            return _json_error(f"Error al consultar Grafana: {exc}", 502)
        except Exception as exc:
            return _json_error(f"Error al construir el catálogo de Grafana: {exc}", 500)

        return jsonify({"status": "success", "catalog": catalog})
