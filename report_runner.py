import asyncio
import base64
import html
import json
import os
import re
import smtplib
import time
from io import BytesIO
from datetime import datetime
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
import uuid

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader, PdfWriter
from pyppeteer import launch
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import Image as PlatypusImage
from reportlab.platypus import KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer

from app_config import AI_PROVIDER_DEFAULT_MODELS, get_ai_settings, get_smtp_settings, get_telegram_settings, normalize_ai_model, smtp_is_configured
from database import (
    create_report_execution,
    get_grafana_server,
    get_report_template,
    get_schedule_recipients,
    should_abort_report_job,
)
from encryption import decrypt_password
from paths import data_path


LOG_FILE = data_path("dashboard_capture.log")
DEFAULT_AI_PROMPT = (
    "Analiza la imagen y los metadatos con foco operacional. Destaca anomalías, tendencias, riesgos, capacidad, "
    "disponibilidad y cualquier lectura práctica que ayude en la toma de decisiones."
)
IGNORED_PANEL_TYPES = {"text"}
COMPACT_PANEL_TYPES = {"stat", "gauge", "bargauge", "piechart"}
PANEL_TITLE_FALLBACK_PROMPT = (
    "Basándote exclusivamente en la imagen del panel y en los metadatos recibidos, genera un título corto, objetivo y técnico "
    "para este panel. Responde únicamente con el título final, sin comillas y sin texto adicional."
)


class ReportExecutionError(Exception):
    def __init__(self, message, details="", image_base64=""):
        super().__init__(message)
        self.details = details
        self.image_base64 = image_base64


def log_message(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a", encoding="utf-8") as log_file:
        log_file.write(f"[{timestamp}] {message}\n")


def build_failure_image_base64(title, lines):
    safe_title = html.escape(title or "Fallo de Ejecución")
    safe_lines = "".join(
        f'<tspan x="24" dy="22">{html.escape(str(line))[:140]}</tspan>'
        for line in lines[:10]
        if str(line).strip()
    )
    svg = f"""
    <svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720">
      <rect width="100%" height="100%" fill="#0b0f1a"/>
      <rect x="18" y="18" width="1244" height="684" rx="24" fill="#111827" stroke="#f97316" stroke-width="2"/>
      <text x="24" y="54" fill="#f97316" font-size="28" font-family="Arial, sans-serif" font-weight="700">{safe_title}</text>
      <text x="24" y="96" fill="#e5e7eb" font-size="18" font-family="Arial, sans-serif">{safe_lines}</text>
    </svg>
    """.strip()
    return base64.b64encode(svg.encode("utf-8")).decode("ascii")


def ensure_kiosk(url):
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["kiosk"] = "1"
    return urlunparse(parsed._replace(query=urlencode(query)))


def get_grafana_runtime_settings(schedule):
    server = get_grafana_server(schedule.get("grafana_server_id"))
    if not server:
        raise ReportExecutionError("Servidor Grafana no encontrado para el agendamiento.")
    return {
        "base_url": server["base_url"].rstrip("/"),
        "username": server["username"],
        "password": decrypt_password(server["password"]),
        "service_account_token": decrypt_password(server["service_account_token"]),
    }


def grafana_api_headers(runtime_settings):
    return {
        "Authorization": f"Bearer {runtime_settings['service_account_token']}",
        "Accept": "application/json",
    }


def grafana_basic_auth(runtime_settings):
    username = (runtime_settings.get("username") or "").strip()
    password = runtime_settings.get("password") or ""
    if username and password:
        return (username, password)
    return None


def grafana_api_get(runtime_settings, path, params=None, timeout=30):
    errors = []
    token = (runtime_settings.get("service_account_token") or "").strip()
    attempts = []

    if token:
        attempts.append(
            {
                "headers": grafana_api_headers(runtime_settings),
                "auth": None,
                "label": "service-account-token",
            }
        )

    basic_auth = grafana_basic_auth(runtime_settings)
    if basic_auth:
        attempts.append(
            {
                "headers": {"Accept": "application/json"},
                "auth": basic_auth,
                "label": "basic-auth",
            }
        )

    if not attempts:
        raise ReportExecutionError("Ninguna credencial válida fue configurada para consultar la API de Grafana.")

    for attempt in attempts:
        try:
            response = requests.get(
                f"{runtime_settings['base_url']}{path}",
                headers=attempt["headers"],
                auth=attempt["auth"],
                params=params,
                timeout=timeout,
            )
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            errors.append(f"{attempt['label']}: {exc}")

    raise ReportExecutionError("Fallo al consultar la API de Grafana.", details="; ".join(errors))


def get_schedule_template(schedule):
    template_id = schedule.get("report_template_id")
    if not template_id:
        return None
    template = get_report_template(template_id)
    if not template:
        return None
    template["show_summary"] = bool(template.get("show_summary"))
    return template


def color_from_hex(value, fallback):
    try:
        return colors.HexColor(value or fallback)
    except Exception:
        return colors.HexColor(fallback)


def decode_data_url(data_url):
    if not data_url:
        return b""
    if "," in data_url:
        _, encoded = data_url.split(",", 1)
    else:
        encoded = data_url
    return base64.b64decode(encoded)


def build_template_styles(template):
    styles = getSampleStyleSheet()
    font_family = (template or {}).get("font_family") or "Helvetica"
    title_size = int((template or {}).get("title_font_size") or 20)
    body_size = int((template or {}).get("body_font_size") or 11)
    primary_color = color_from_hex((template or {}).get("primary_color"), "#f97316")
    secondary_color = color_from_hex((template or {}).get("secondary_color"), "#0f172a")

    return {
        "title": ParagraphStyle(
            "TemplateTitle",
            parent=styles["Heading1"],
            fontName=font_family,
            fontSize=title_size,
            leading=title_size + 4,
            textColor=secondary_color,
            spaceAfter=12,
        ),
        "heading": ParagraphStyle(
            "TemplateHeading",
            parent=styles["Heading2"],
            fontName=font_family,
            fontSize=max(title_size - 4, body_size + 2),
            leading=max(title_size, body_size + 6),
            textColor=primary_color,
            spaceAfter=10,
        ),
        "subheading": ParagraphStyle(
            "TemplateSubheading",
            parent=styles["Heading3"],
            fontName=font_family,
            fontSize=max(body_size + 2, 12),
            leading=body_size + 6,
            textColor=secondary_color,
            spaceAfter=6,
        ),
        "body": ParagraphStyle(
            "TemplateBody",
            parent=styles["BodyText"],
            fontName=font_family,
            fontSize=body_size,
            leading=body_size + 5,
            textColor=secondary_color,
            spaceAfter=8,
            alignment=TA_JUSTIFY,
        ),
        "bullet": ParagraphStyle(
            "TemplateBullet",
            parent=styles["BodyText"],
            fontName=font_family,
            fontSize=body_size,
            leading=body_size + 5,
            textColor=secondary_color,
            leftIndent=14,
            firstLineIndent=0,
            bulletIndent=0,
            spaceAfter=6,
            alignment=TA_JUSTIFY,
        ),
        "muted": ParagraphStyle(
            "TemplateMuted",
            parent=styles["BodyText"],
            fontName=font_family,
            fontSize=max(body_size - 1, 8),
            leading=body_size + 4,
            textColor=colors.HexColor("#64748b"),
            spaceAfter=8,
        ),
        "caption": ParagraphStyle(
            "TemplateCaption",
            parent=styles["BodyText"],
            fontName=font_family,
            fontSize=max(body_size - 1, 8),
            leading=body_size + 3,
            textColor=colors.HexColor("#64748b"),
            alignment=TA_CENTER,
            spaceAfter=10,
        ),
        "email_title": ParagraphStyle(
            "TemplateEmailTitle",
            parent=styles["Heading1"],
            fontName=font_family,
            fontSize=max(title_size + 4, 22),
            leading=title_size + 10,
            textColor=secondary_color,
            spaceAfter=14,
        ),
        "primary_color": primary_color,
        "secondary_color": secondary_color,
        "font_family": font_family,
        "body_size": body_size,
    }


def abort_if_job_cancelled(job_id, schedule, context_label="processamento"):
    if not job_id:
        return
    if should_abort_report_job(job_id):
        raise ReportExecutionError(
            "Execucao cancelada manualmente.",
            details=f"O job {job_id} foi interrompido durante {context_label}.",
            image_base64=build_failure_image_base64(
                "Execucao cancelada",
                [schedule.get("titulo", ""), f"Job {job_id} cancelado durante {context_label}."],
            ),
        )


def build_page_chrome(template, schedule, styles):
    header_text = (template or {}).get("header_text", "").strip()
    footer_text = (schedule.get("report_footer") or "").strip()
    page_width, page_height = A4
    primary_color = styles["primary_color"]
    muted_color = colors.HexColor("#64748b")
    font_family = styles["font_family"]
    small_size = max(styles["body_size"] - 1, 8)
    logo_bytes = decode_data_url((template or {}).get("logo_base64", ""))
    logo_reader = ImageReader(BytesIO(logo_bytes)) if logo_bytes else None

    def _draw(canvas, doc):
        canvas.saveState()
        header_y = page_height - 26
        line_y = page_height - 34
        footer_y = 18
        logo_reserved_width = 0

        canvas.setStrokeColor(colors.HexColor("#d7dde5"))
        canvas.setLineWidth(0.6)
        canvas.line(doc.leftMargin, line_y, page_width - doc.rightMargin, line_y)

        if logo_reader:
            image_width, image_height = logo_reader.getSize()
            max_width = 82
            max_height = 22
            scale = min(max_width / image_width, max_height / image_height)
            draw_width = image_width * scale
            draw_height = image_height * scale
            draw_x = page_width - doc.rightMargin - draw_width
            draw_y = page_height - 28
            canvas.drawImage(
                logo_reader,
                draw_x,
                draw_y,
                width=draw_width,
                height=draw_height,
                preserveAspectRatio=True,
                mask="auto",
            )
            logo_reserved_width = draw_width + 12

        if header_text:
            canvas.setFont(font_family, small_size)
            canvas.setFillColor(primary_color)
            canvas.drawString(doc.leftMargin, header_y, truncate_text(header_text, 120))

        canvas.setFont(font_family, small_size)
        canvas.setFillColor(muted_color)
        canvas.drawRightString(page_width - doc.rightMargin - logo_reserved_width, header_y, f"Página {canvas.getPageNumber()}")

        if footer_text:
            canvas.drawCentredString(page_width / 2, footer_y, truncate_text(footer_text, 140))
        canvas.restoreState()

    return _draw


def merge_pdf_parts(parts, output_path):
    writer = PdfWriter()
    for part in parts:
        if not part or not os.path.exists(part):
            continue
        reader = PdfReader(part)
        for page in reader.pages:
            writer.add_page(page)
    with open(output_path, "wb") as output_stream:
        writer.write(output_stream)
    return output_path


def build_summary_appendix_pdf(report, template, output_path):
    analysis_text = (report.get("ai_summary") or "").strip()
    if not analysis_text:
        return None

    styles = build_template_styles(template)
    doc = SimpleDocTemplate(output_path, pagesize=A4, leftMargin=42, rightMargin=42, topMargin=48, bottomMargin=42)
    story = [Paragraph("Análisis", styles["heading"])]
    append_markdown_blocks(story, analysis_text, styles)
    page_chrome = build_page_chrome(template, {"report_footer": ""}, styles)
    doc.build(story, onFirstPage=page_chrome, onLaterPages=page_chrome)
    return output_path


def append_summary_to_report(schedule, report, template=None):
    base_path = report["pdf_path"]
    parts = [base_path]
    appendix_path = os.path.splitext(base_path)[0] + "_analise.pdf"
    merged_path = os.path.splitext(base_path)[0] + "_final.pdf"

    appendix_built = build_summary_appendix_pdf(report, template, appendix_path) if schedule.get("use_ai") else None
    if appendix_built:
        parts.append(appendix_path)
        merge_pdf_parts(parts, merged_path)
        report["pdf_path"] = merged_path

        for temp_path in [appendix_path, base_path]:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

    return report["pdf_path"]


def flatten_targets(selected_targets):
    for target in selected_targets:
        if target.get("type") == "dashboard" and target.get("uid"):
            return [
                {
                    "uid": target["uid"],
                    "title": target.get("title", target["uid"]),
                    "url": target.get("url", ""),
                }
            ]
    return []


def truncate_text(value, limit=1200):
    value = (value or "").strip()
    if len(value) <= limit:
        return value
    return f"{value[:limit].rstrip()}..."


def unique_values(values):
    ordered = []
    seen = set()
    for value in values:
        normalized = str(value or "").strip()
        if not normalized or normalized in seen:
            continue
        ordered.append(normalized)
        seen.add(normalized)
    return ordered


def extract_panel_datasources(panel):
    values = []
    datasource = panel.get("datasource")
    if isinstance(datasource, dict):
        values.extend([datasource.get("uid"), datasource.get("name"), datasource.get("type")])
    elif datasource:
        values.append(str(datasource))

    for target in panel.get("targets") or []:
        target_ds = target.get("datasource")
        if isinstance(target_ds, dict):
            values.extend([target_ds.get("uid"), target_ds.get("name"), target_ds.get("type")])
        elif target_ds:
            values.append(str(target_ds))
    return unique_values(values)


async def login_grafana(page, base_url, user, password):
    login_url = f"{base_url.rstrip('/')}/login"
    await page.goto(login_url, {"waitUntil": "networkidle2", "timeout": 120000})
    await page.waitForSelector('input[name="user"]', timeout=15000)
    await page.type('input[name="user"]', user)
    await page.type('input[name="password"]', password)
    await page.click('button[type="submit"]')
    await page.waitForNavigation({"waitUntil": "networkidle0", "timeout": 120000})


async def scroll_dashboard_to_bottom(page):
    previous_height = 0
    for _ in range(12):
        current_height = await page.evaluate(
            """
            () => Math.max(
                document.body.scrollHeight,
                document.documentElement.scrollHeight,
                document.body.offsetHeight,
                document.documentElement.offsetHeight
            )
            """
        )
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(1.2)
        if current_height == previous_height:
            break
        previous_height = current_height


async def measure_dashboard_dimensions(page, dashboard_url):
    await page.goto(dashboard_url, {"waitUntil": "networkidle0", "timeout": 300000})
    await page.waitForFunction("() => [...document.images].every(img => img.complete)", {"timeout": 60000})
    await scroll_dashboard_to_bottom(page)
    await asyncio.sleep(1)
    dimensions = await page.evaluate(
        """
        () => {
            const root = document.querySelector('.react-grid-layout');
            const body = document.body;
            const doc = document.documentElement;
            const width = Math.max(
                root ? Math.ceil(root.getBoundingClientRect().right + 24) : 0,
                body.scrollWidth,
                doc.scrollWidth,
                body.offsetWidth,
                doc.offsetWidth,
                1632
            );
            const height = Math.max(
                root ? Math.ceil(root.getBoundingClientRect().bottom + 32) : 0,
                body.scrollHeight,
                doc.scrollHeight,
                body.offsetHeight,
                doc.offsetHeight,
                1600
            );
            return { width, height };
        }
        """
    )
    return {
        "width": max(int(dimensions.get("width", 1632)), 1280),
        "height": max(int(dimensions.get("height", 1600)), 900),
    }


async def capture_dashboard_assets(page, dashboard_url, output_path, viewport):
    await page.goto(dashboard_url, {"waitUntil": "networkidle0", "timeout": 300000})
    await page.waitForFunction("() => [...document.images].every(img => img.complete)", {"timeout": 60000})
    await scroll_dashboard_to_bottom(page)
    await asyncio.sleep(1)
    await page.evaluate(
        """
        () => {
            document.querySelectorAll('.panel-info-corner, .react-resizable-handle')
            .forEach(el => el.style.display = 'none')
        }
        """
    )
    await page.setViewport(
        {
            "width": viewport["width"],
            "height": viewport["height"],
            "deviceScaleFactor": 2,
        }
    )
    await page.pdf(
        {
            "path": output_path,
            "width": f"{int(viewport['width'])}px",
            "height": f"{int(viewport['height'])}px",
            "printBackground": True,
            "margin": {"top": "0px", "bottom": "0px", "left": "0px", "right": "0px"},
        }
    )
    soup = BeautifulSoup(await page.content(), "html.parser")
    titles = [tag.get_text(strip=True) for tag in soup.find_all(["h2", "h3", "h4"]) if tag.get_text(strip=True)]
    screenshot_bytes = await page.screenshot({"fullPage": True, "type": "jpeg", "quality": 85})
    return {
        "screenshot_bytes": screenshot_bytes,
        "chart_titles": unique_values(titles)[:20],
    }


async def capture_panel_image_from_view(page, panel_url, panel_id, panel_type=""):
    await page.goto(panel_url, {"waitUntil": "networkidle0", "timeout": 300000})
    await page.waitForFunction("() => [...document.images].every(img => img.complete)", {"timeout": 60000})
    await asyncio.sleep(1.5)

    clip = await page.evaluate(
        """
        ({ panelId, compact }) => {
            const panelSelectors = [
                `[data-panelid="${panelId}"]`,
                `[data-panelid="panel-${panelId}"]`,
                `[data-panelid="${String(panelId)}"]`,
                '[data-panelid]',
                '.panel-container',
                '.panel-wrapper',
                'main'
            ];

            const toClip = (rect, pad = 16) => {
                if (!rect || rect.width <= 20 || rect.height <= 20) return null;
                return {
                    x: Math.max(0, Math.floor(rect.left - pad)),
                    y: Math.max(0, Math.floor(rect.top - pad)),
                    width: Math.ceil(rect.width + pad * 2),
                    height: Math.ceil(rect.height + pad * 2),
                };
            };

            let panelRoot = null;
            for (const selector of panelSelectors) {
                const candidate = document.querySelector(selector);
                if (!candidate) continue;
                const rect = candidate.getBoundingClientRect();
                if (rect.width > 40 && rect.height > 40) {
                    panelRoot = candidate;
                    break;
                }
            }

            if (!panelRoot) {
                return null;
            }

            if (compact) {
                const compactSelectors = [
                    'canvas',
                    'svg',
                    '[data-testid="stat-panel"]',
                    '[data-testid="viz-panel"]',
                    '[data-testid="panel content"]',
                    '.panel-content',
                    '.css-1mhnkuh',
                    '.css-1fov6x0'
                ];
                const matches = [];
                for (const selector of compactSelectors) {
                    panelRoot.querySelectorAll(selector).forEach((node) => {
                        const rect = node.getBoundingClientRect();
                        if (rect.width > 20 && rect.height > 20) {
                            matches.push(rect);
                        }
                    });
                }
                if (matches.length) {
                    matches.sort((a, b) => (a.width * a.height) - (b.width * b.height));
                    return toClip(matches[0], 10);
                }
            }

            const inner = panelRoot.querySelector('[data-testid="panel content"], .panel-content, canvas, svg');
            if (inner) {
                const innerRect = inner.getBoundingClientRect();
                const rootRect = panelRoot.getBoundingClientRect();
                if (innerRect.width > 30 && innerRect.height > 30 && (innerRect.width * innerRect.height) < (rootRect.width * rootRect.height * 0.75)) {
                    return toClip(innerRect, 12);
                }
            }

            return toClip(panelRoot.getBoundingClientRect(), 16);
        }
        """,
        {"panelId": panel_id, "compact": panel_type in COMPACT_PANEL_TYPES},
    )

    screenshot_kwargs = {"type": "png"}
    if clip:
        screenshot_kwargs["clip"] = clip
    else:
        screenshot_kwargs["fullPage"] = False
    return await page.screenshot(screenshot_kwargs)


async def build_dashboard_reports(schedule):
    abort_if_job_cancelled(schedule.get("_job_id"), schedule, "inicio da geracao")
    dashboards = flatten_targets(schedule["selected_targets"])
    if not dashboards:
        return []

    raw_title = schedule["titulo"] or f"agendamento_{schedule['id']}"
    safe_dir_name = "".join(c if c.isalnum() or c in ("-", "_", " ") else "_" for c in raw_title)[:80].strip() or f"agendamento_{schedule['id']}"
    client_dir = data_path("relatorios", safe_dir_name)
    os.makedirs(client_dir, exist_ok=True)
    runtime_settings = get_grafana_runtime_settings(schedule)
    report_template = get_schedule_template(schedule)
    base_url = runtime_settings["base_url"]

    if schedule["report_type"] == "detalhado":
        return await build_detailed_dashboard_reports(schedule, dashboards, runtime_settings, client_dir, report_template)

    reports = []
    page = None
    browser = None
    try:
        browser = await launch(
            headless=True,
            args=["--no-sandbox"],
            handleSIGINT=False,
            handleSIGTERM=False,
            handleSIGHUP=False,
            autoClose=False,
        )
        page = await browser.newPage()
        await page.setViewport({"width": 1632, "height": 1600, "deviceScaleFactor": 2})
        await login_grafana(page, base_url, runtime_settings["username"], runtime_settings["password"])

        measured_viewports = {}
        for dashboard in dashboards:
            abort_if_job_cancelled(schedule.get("_job_id"), schedule, "medicao da dashboard")
            dashboard_url = ensure_kiosk(dashboard["url"])
            measured_viewports[dashboard["uid"]] = await measure_dashboard_dimensions(page, dashboard_url)

        await browser.close()
        browser = await launch(
            headless=True,
            args=["--no-sandbox"],
            handleSIGINT=False,
            handleSIGTERM=False,
            handleSIGHUP=False,
            autoClose=False,
        )
        page = await browser.newPage()
        await page.setViewport({"width": 1632, "height": 1600, "deviceScaleFactor": 2})
        await login_grafana(page, base_url, runtime_settings["username"], runtime_settings["password"])

        for dashboard in dashboards:
            abort_if_job_cancelled(schedule.get("_job_id"), schedule, "captura da dashboard")
            safe_title = "".join(char if char.isalnum() or char in ("-", "_") else "_" for char in dashboard["title"])[:80]
            pdf_path = os.path.join(
                client_dir,
                f"{datetime.now().strftime('%Y-%m-%d')}_{schedule['id']}_{safe_title or dashboard['uid']}.pdf",
            )
            dashboard_url = ensure_kiosk(dashboard["url"])
            assets = await capture_dashboard_assets(
                page,
                dashboard_url,
                pdf_path,
                measured_viewports.get(dashboard["uid"], {"width": 1632, "height": 1600}),
            )
            ai_analysis = ""
            if schedule.get("use_ai"):
                ai_analysis = generate_visual_analysis(
                    schedule=schedule,
                    task_instruction=(
                        "Analiza este dashboard como una visión consolidada. Resume el comportamiento general del entorno, "
                        "señales de alerta, tendencias, consistencia operacional y cualquier indicio relevante percibido en la imagen."
                    ),
                    metadata_text="",
                    image_bytes=assets["screenshot_bytes"],
                    mime_type="image/jpeg",
                    context_label=f"dashboard-{dashboard['uid']}",
                )
            reports.append(
                {
                    "uid": dashboard["uid"],
                    "title": dashboard["title"],
                    "url": dashboard_url,
                    "pdf_path": pdf_path,
                    "chart_titles": assets["chart_titles"],
                    "viewport": measured_viewports.get(dashboard["uid"], {"width": 1632, "height": 1600}),
                    "ai_analysis": ai_analysis,
                    "ai_summary": ai_analysis,
                    "metadata_text": "",
                }
            )
        for report in reports:
            append_summary_to_report(schedule, report, report_template)
    except Exception as exc:
        image_base64 = ""
        if page is not None:
            try:
                image_base64 = await page.screenshot({"encoding": "base64", "fullPage": True})
            except Exception:
                image_base64 = ""
        if not image_base64:
            image_base64 = build_failure_image_base64(
                "Fallo al generar reporte",
                [schedule.get("titulo", ""), str(exc)],
            )
        raise ReportExecutionError("Fallo al generar reportes.", details=str(exc), image_base64=image_base64) from exc
    finally:
        if browser is not None:
            await browser.close()
    return reports


def flatten_dashboard_panels(panels):
    flattened = []
    for panel in panels or []:
        nested = panel.get("panels") or []
        if panel.get("type") == "row" or nested:
            flattened.extend(flatten_dashboard_panels(nested))
            continue
        panel_id = panel.get("id")
        if not panel_id:
            continue
        panel_type = str(panel.get("type") or "").strip().lower()
        if panel_type in IGNORED_PANEL_TYPES:
            log_message(f"Painel {panel_id} ignorado por tipo: {panel_type}")
            continue
        title_raw = str(panel.get("title") or "").strip()
        description = (
            panel.get("description")
            or panel.get("desc")
            or panel.get("options", {}).get("description", "")
        )
        unit = (
            panel.get("fieldConfig", {})
            .get("defaults", {})
            .get("unit", "")
        )
        flattened.append(
            {
                "id": panel_id,
                "title": title_raw or f"Panel {panel_id}",
                "title_raw": title_raw,
                "title_missing": not bool(title_raw),
                "description": description.strip(),
                "datasources": extract_panel_datasources(panel),
                "panel_type": panel_type,
                "unit": str(unit or "").strip(),
            }
        )
    return flattened


def fetch_dashboard_metadata(runtime_settings, dashboard):
    response = grafana_api_get(runtime_settings, f"/api/dashboards/uid/{dashboard['uid']}", timeout=30)
    payload = response.json()
    meta = payload.get("meta", {})
    dashboard_data = payload.get("dashboard", {})
    panels = flatten_dashboard_panels(dashboard_data.get("panels", []))
    dashboard_datasources = unique_values(
        datasource
        for panel in panels
        for datasource in panel.get("datasources", [])
    )
    return {
        "uid": dashboard["uid"],
        "title": dashboard_data.get("title") or dashboard["title"],
        "description": str(dashboard_data.get("description") or "").strip(),
        "slug": meta.get("slug") or "",
        "tags": dashboard_data.get("tags") or [],
        "datasources": dashboard_datasources,
        "panels": panels,
        "source_url": dashboard.get("url", ""),
    }


def build_dashboard_metadata_text(dashboard_meta):
    lines = [
        f"Dashboard: {dashboard_meta.get('title', 'Sin título')}",
        f"UID: {dashboard_meta.get('uid', '')}",
        f"Descripción: {truncate_text(dashboard_meta.get('description') or 'No informada.', 600)}",
        f"Tags: {', '.join(dashboard_meta.get('tags') or []) or 'Sin etiquetas'}",
        f"Datasources identificados: {', '.join(dashboard_meta.get('datasources') or []) or 'No identificado'}",
        f"Cantidad de paneles: {len(dashboard_meta.get('panels') or [])}",
    ]
    return "\n".join(lines)


def build_panel_metadata_text(dashboard_meta, panel, panel_number):
    lines = [
        f"Dashboard: {dashboard_meta.get('title', 'Sin título')}",
        f"Panel #{panel_number}",
        f"ID del panel: {panel.get('id')}",
        f"Título actual: {panel.get('title_raw') or 'Sin título definido'}",
        f"Descripción: {truncate_text(panel.get('description') or 'No informada.', 500)}",
        f"Datasource: {', '.join(panel.get('datasources') or []) or 'No identificado'}",
        f"Tipo de visualización: {panel.get('panel_type') or 'No identificado'}",
        f"Unidad principal: {panel.get('unit') or 'No informada'}",
    ]
    return "\n".join(lines)


def build_panel_view_url(dashboard_meta, panel_id):
    source_url = urlparse(dashboard_meta.get("source_url", ""))
    query = dict(parse_qsl(source_url.query, keep_blank_values=True))
    query["viewPanel"] = f"panel-{panel_id}"
    query["kiosk"] = "1"
    return urlunparse(source_url._replace(query=urlencode(query)))


def image_bytes_to_data_url(image_bytes, mime_type):
    encoded = base64.b64encode(image_bytes).decode("utf-8")
    return f"data:{mime_type};base64,{encoded}"


def build_ai_prompt_text(schedule, task_instruction, metadata_text, extra_context=""):
    user_prompt = truncate_text(schedule.get("ai_prompt_text") or DEFAULT_AI_PROMPT, 5000)
    extra_instruction = truncate_text(schedule.get("report_ai_instruction") or "", 1200)
    parts = [
        "Estás analizando un reporte operacional de Grafana.",
        f"Programación: {schedule.get('titulo', 'Sin nombre')}",
        task_instruction,
        "Si falta contexto, indícalo explícitamente en lugar de inventar.",
        "Responde en texto natural, con párrafos cortos y fluidos.",
        "No uses listas, tópicos, marcadores, numeración ni títulos con #.",
        "No uses bloques de código, no devuelvas JSON ni formatees como Markdown artificial.",
        "",
        "Prompt configurado por el usuario:",
        user_prompt,
    ]
    metadata_text = truncate_text(metadata_text or "", 6000).strip()
    if metadata_text:
        parts.extend(
            [
                "",
                "Usa los metadatos resumidos a continuación junto con la imagen enviada en la misma solicitud.",
                "",
                "Metadatos resumidos:",
                metadata_text,
            ]
        )
    else:
        parts.extend(["", "Basa el análisis únicamente en la imagen enviada en esta solicitud."])
    if extra_instruction:
        parts.extend(["", "Instrucciones adicionales del usuario:", extra_instruction])
    if extra_context:
        parts.extend(["", extra_context])
    return "\n".join(parts).strip()


def extract_text_from_openai_responses(data):
    if data.get("output_text"):
        return data["output_text"].strip()
    texts = []
    for item in data.get("output", []):
        for content in item.get("content", []):
            text_value = content.get("text")
            if text_value:
                texts.append(text_value)
    return "\n".join(texts).strip()


def extract_text_from_chat_completions(data):
    content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [item.get("text", "") for item in content if isinstance(item, dict)]
        return "\n".join(part for part in parts if part).strip()
    return ""


def extract_text_from_claude_messages(data):
    parts = []
    for item in data.get("content", []):
        if isinstance(item, dict) and item.get("type") == "text" and item.get("text"):
            parts.append(item["text"])
    return "\n".join(parts).strip()


def extract_http_error_detail(response):
    if response is None:
        return ""

    try:
        payload = response.json()
    except ValueError:
        return truncate_text((response.text or "").strip(), 500)

    error = payload.get("error")
    if isinstance(error, dict):
        error_type = str(error.get("type") or "").strip()
        error_message = str(error.get("message") or "").strip()
        if error_type and error_message:
            return f"{error_type}: {error_message}"
        return error_message or error_type

    message = str(payload.get("message") or "").strip()
    if message:
        return message

    return truncate_text(json.dumps(payload, ensure_ascii=False), 500)


def raise_for_status_with_detail(response):
    if response.ok:
        return

    detail = extract_http_error_detail(response)
    if detail:
        raise requests.HTTPError(
            f"{response.status_code} Client Error: {detail} for url: {response.url}",
            response=response,
        )

    response.raise_for_status()


def markdown_inline_to_reportlab(text):
    escaped = html.escape(text or "")
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
    escaped = re.sub(r"__(.+?)__", r"<b>\1</b>", escaped)
    escaped = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"<i>\1</i>", escaped)
    escaped = re.sub(r"(?<!_)_(?!\s)(.+?)(?<!\s)_(?!_)", r"<i>\1</i>", escaped)
    escaped = re.sub(r"`([^`]+)`", r"<font face='Courier'>\1</font>", escaped)
    return escaped


def append_markdown_blocks(story, markdown_text, styles, default_style_name="body"):
    text = (markdown_text or "").strip()
    if not text:
        return

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"```(?:markdown|md|text)?\n?", "", text, flags=re.IGNORECASE)
    text = text.replace("```", "").strip()
    blocks = [block.strip() for block in re.split(r"\n\s*\n", text) if block.strip()]

    for block in blocks:
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if not lines:
            continue

        first_line = lines[0]
        heading_match = re.match(r"^(#{1,3})\s+(.*)$", first_line)
        if heading_match:
            level = len(heading_match.group(1))
            heading_style = "heading" if level == 1 else "subheading"
            story.append(Paragraph(markdown_inline_to_reportlab(heading_match.group(2)), styles[heading_style]))
            remaining = lines[1:]
            if remaining:
                story.append(Paragraph(markdown_inline_to_reportlab(" ".join(remaining)), styles[default_style_name]))
            continue

        bullet_match = all(re.match(r"^([-*]|\d+\.)\s+", line) for line in lines)
        if bullet_match:
            for line in lines:
                cleaned = re.sub(r"^([-*]|\d+\.)\s+", "", line)
                story.append(Paragraph(markdown_inline_to_reportlab(cleaned), styles["bullet"], bulletText="•"))
            continue

        story.append(Paragraph(markdown_inline_to_reportlab(" ".join(lines)), styles[default_style_name]))


def normalize_azure_endpoint(endpoint, target):
    value = (endpoint or "").strip()
    if not value:
        raise ValueError("Endpoint de Azure AI Foundry no configurado.")

    parsed = urlparse(value)
    path = (parsed.path or "").rstrip("/")
    lower_path = path.lower()
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))

    if target == "responses":
        query.pop("api-version", None)
        if lower_path.endswith("/openai/responses"):
            new_path = path[: -len("/openai/responses")] + "/openai/v1/responses"
            return urlunparse(parsed._replace(path=new_path, query=urlencode(query)))
        if lower_path.endswith("/responses"):
            if "/openai/v1/" not in lower_path:
                new_path = path[: -len("/responses")] + "/v1/responses"
                return urlunparse(parsed._replace(path=new_path, query=urlencode(query)))
            return urlunparse(parsed._replace(query=urlencode(query)))
        if "/openai/v1" in lower_path:
            new_path = f"{path}/responses"
            return urlunparse(parsed._replace(path=new_path, query=urlencode(query)))
        if "/chat/completions" in lower_path:
            new_path = path[: lower_path.rfind("/chat/completions")] + "/responses"
            return urlunparse(parsed._replace(path=new_path, query=urlencode(query)))
        if "/openai" in lower_path:
            if lower_path.endswith("/openai"):
                new_path = f"{path}/v1/responses"
            else:
                new_path = f"{path}/responses"
            return urlunparse(parsed._replace(path=new_path, query=urlencode(query)))
        new_path = f"{path}/openai/v1/responses" if path else "/openai/v1/responses"
        return urlunparse(parsed._replace(path=new_path, query=urlencode(query)))

    if lower_path.endswith("/openai/responses"):
        new_path = path[: -len("/openai/responses")] + "/openai/chat/completions"
        if "api-version" not in query:
            query["api-version"] = "2024-05-01-preview"
        return urlunparse(parsed._replace(path=new_path, query=urlencode(query)))
    if lower_path.endswith("/chat/completions"):
        if "api-version" not in query:
            query["api-version"] = "2024-05-01-preview"
        return urlunparse(parsed._replace(query=urlencode(query)))
    if lower_path.endswith("/responses"):
        new_path = path[: lower_path.rfind("/responses")] + "/chat/completions"
        if "api-version" not in query:
            query["api-version"] = "2024-05-01-preview"
        return urlunparse(parsed._replace(path=new_path, query=urlencode(query)))
    if "/openai/v1" in lower_path:
        new_path = f"{path}/chat/completions"
        if "api-version" not in query:
            query["api-version"] = "2024-05-01-preview"
        return urlunparse(parsed._replace(path=new_path, query=urlencode(query)))
    if "/openai" in lower_path:
        new_path = f"{path}/chat/completions"
        if "api-version" not in query:
            query["api-version"] = "2024-05-01-preview"
        return urlunparse(parsed._replace(path=new_path, query=urlencode(query)))
    new_path = f"{path}/chat/completions" if path else "/chat/completions"
    if "api-version" not in query:
        query["api-version"] = "2024-05-01-preview"
    return urlunparse(parsed._replace(path=new_path, query=urlencode(query)))


def get_ai_runtime(schedule):
    ai_settings = get_ai_settings()
    schedule_provider = str(schedule.get("ai_provider") or "").strip().lower()
    provider = schedule_provider if schedule_provider in {"openai", "azure", "claude"} else ai_settings["provider"]
    return {
        "provider": provider,
        "model": normalize_ai_model(provider, ai_settings["model"] or AI_PROVIDER_DEFAULT_MODELS.get(provider, "gpt-4o-mini")),
        "api_key": ai_settings["api_key"],
        "endpoint": ai_settings["endpoint"],
    }


def call_ai_provider(
    prompt_text,
    provider,
    model,
    api_key,
    endpoint="",
    image_bytes=None,
    mime_type="image/png",
    context_label="",
    max_output_tokens=900,
):
    if not api_key:
        raise ValueError("API Key de IA no configurada.")

    if provider not in {"openai", "azure", "claude"}:
        raise ValueError(f"Proveedor de IA no soportado: {provider}")

    request_id = str(uuid.uuid4())
    image_data_url = image_bytes_to_data_url(image_bytes, mime_type) if image_bytes else ""

    for attempt in range(1, 4):
        try:
            if provider == "openai":
                content = [{"type": "input_text", "text": prompt_text}]
                if image_data_url:
                    content.append({"type": "input_image", "image_url": image_data_url})
                response = requests.post(
                    "https://api.openai.com/v1/responses",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                        "X-Client-Request-Id": request_id,
                    },
                    json={
                        "model": model,
                        "input": [{"role": "user", "content": content}],
                        "max_output_tokens": max_output_tokens,
                    },
                    timeout=120,
                )
                raise_for_status_with_detail(response)
                return extract_text_from_openai_responses(response.json())

            if provider == "azure":
                if not endpoint:
                    raise ValueError("Endpoint de Azure AI Foundry no configurado.")

                endpoint_lower = endpoint.lower()
                use_responses = "/openai/v1" in endpoint_lower or "/responses" in endpoint_lower

                if use_responses:
                    url = normalize_azure_endpoint(endpoint, "responses")
                    content = [{"type": "input_text", "text": prompt_text}]
                    if image_data_url:
                        content.append({"type": "input_image", "image_url": image_data_url})
                    response = requests.post(
                        url,
                        headers={
                            "api-key": api_key,
                            "Content-Type": "application/json",
                            "X-Client-Request-Id": request_id,
                        },
                        json={
                            "model": model,
                            "input": [{"role": "user", "content": content}],
                            "max_output_tokens": max_output_tokens,
                        },
                        timeout=120,
                    )
                    raise_for_status_with_detail(response)
                    return extract_text_from_openai_responses(response.json())

                url = normalize_azure_endpoint(endpoint, "chat")
                content = [{"type": "text", "text": prompt_text}]
                if image_data_url:
                    content.append({"type": "image_url", "image_url": {"url": image_data_url}})
                response = requests.post(
                    url,
                    headers={
                        "api-key": api_key,
                        "Content-Type": "application/json",
                        "X-Client-Request-Id": request_id,
                    },
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": content}],
                        "max_tokens": max_output_tokens,
                    },
                    timeout=120,
                )
                raise_for_status_with_detail(response)
                return extract_text_from_chat_completions(response.json())

            if provider == "claude":
                content = []
                if image_bytes:
                    # Anthropic requer imagem antes do texto para tarefas de visão
                    content.append(
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": mime_type,
                                "data": base64.b64encode(image_bytes).decode("utf-8"),
                            },
                        }
                    )
                content.append({"type": "text", "text": prompt_text})
                response = requests.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": model,
                        "max_tokens": max_output_tokens,
                        "messages": [{"role": "user", "content": content}],
                    },
                    timeout=120,
                )
                raise_for_status_with_detail(response)
                return extract_text_from_claude_messages(response.json())

        except Exception as exc:
            log_message(
                f"[IA] Falha na tentativa {attempt}/3 | contexto={context_label or 'sem-contexto'} "
                f"| provedor={provider} | request_id={request_id} | erro={exc}"
            )
            if attempt >= 3:
                raise
            time.sleep(attempt * 2)


def generate_visual_analysis(schedule, task_instruction, metadata_text, image_bytes, context_label, mime_type="image/png"):
    if not schedule.get("use_ai"):
        return ""
    runtime = get_ai_runtime(schedule)
    try:
        return call_ai_provider(
            prompt_text=build_ai_prompt_text(schedule, task_instruction, metadata_text),
            provider=runtime["provider"],
            model=runtime["model"],
            api_key=runtime["api_key"],
            endpoint=runtime["endpoint"],
            image_bytes=image_bytes,
            mime_type=mime_type,
            context_label=context_label,
        )
    except Exception as exc:
        log_message(f"[IA] Analise visual falhou | contexto={context_label} | erro={exc}")
        return f"Fallo al generar análisis de IA: {exc}"


def generate_missing_panel_title(schedule, dashboard_meta, panel, panel_number, image_bytes):
    if not schedule.get("use_ai") or not panel.get("title_missing"):
        return panel.get("title") or f"Panel {panel_number}"

    runtime = get_ai_runtime(schedule)
    metadata_text = build_panel_metadata_text(dashboard_meta, panel, panel_number)
    prompt_text = (
        "Estás nombrando un panel del dashboard.\n"
        f"Programación: {schedule.get('titulo', 'Sin nombre')}\n"
        f"{PANEL_TITLE_FALLBACK_PROMPT}\n\n"
        "Metadatos resumidos:\n"
        f"{metadata_text}"
    )
    try:
        title = call_ai_provider(
            prompt_text=prompt_text,
            provider=runtime["provider"],
            model=runtime["model"],
            api_key=runtime["api_key"],
            endpoint=runtime["endpoint"],
            image_bytes=image_bytes,
            mime_type="image/png",
            context_label=f"titulo-painel-{dashboard_meta['uid']}-{panel['id']}",
        )
        title = " ".join((title or "").split())
        return truncate_text(title, 120) or f"Panel {panel_number}"
    except Exception as exc:
        log_message(f"[IA] Falha ao gerar titulo do painel {panel['id']}: {exc}")
        return f"Panel {panel_number}"


def append_template_title_block(story, schedule, dashboard_meta, styles, template):
    story.append(Paragraph(html.escape(dashboard_meta["title"]), styles["email_title"]))
    story.append(Paragraph(f"Programación: {html.escape(schedule['titulo'])}", styles["muted"]))
    story.append(Spacer(1, 0.18 * inch))

def build_detailed_dashboard_pdf(schedule, dashboard_meta, panel_results, output_path, template=None, dashboard_screenshot_bytes=None):
    doc = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        leftMargin=36,
        rightMargin=36,
        topMargin=56,
        bottomMargin=46,
    )
    styles = build_template_styles(template)
    story = []
    panel_titles = []
    panel_analyses = []

    append_template_title_block(story, schedule, dashboard_meta, styles, template)

    if dashboard_screenshot_bytes:
        dashboard_image = PlatypusImage(BytesIO(dashboard_screenshot_bytes))
        dashboard_image._restrictSize(6.8 * inch, 7.6 * inch)
        dashboard_image.hAlign = "CENTER"
        story.append(dashboard_image)
        if panel_results:
            story.append(PageBreak())

    if template and template.get("show_summary") and panel_results:
        story.append(Paragraph("Resumen", styles["heading"]))
        for index, panel_result in enumerate(panel_results, start=1):
            title_for_summary = panel_result.get("resolved_title") or f"Panel {index}"
            story.append(Paragraph(f"{index}. {html.escape(title_for_summary)}", styles["body"]))
        story.append(PageBreak())

    for index, panel_result in enumerate(panel_results, start=1):
        panel = panel_result["panel"]
        if index > 1:
            story.append(PageBreak())

        image_bytes = panel_result["image_bytes"]
        panel_title = panel_result["resolved_title"]
        panel_titles.append(panel_title)
        panel_intro = [Paragraph(html.escape(panel_title), styles["heading"])]
        if panel["description"]:
            panel_intro.append(Paragraph(f"<b>Descripción:</b> {html.escape(panel['description'])}", styles["body"]))
            panel_intro.append(Spacer(1, 0.08 * inch))

        image = PlatypusImage(BytesIO(image_bytes))
        image._restrictSize(6.8 * inch, 4.8 * inch)
        image.hAlign = "CENTER"
        panel_intro.append(image)
        panel_intro.append(Spacer(1, 0.12 * inch))
        panel_intro.append(Paragraph(f"Figura {index} - {html.escape(panel_title)}", styles["caption"]))
        panel_intro.append(Spacer(1, 0.08 * inch))
        story.append(KeepTogether(panel_intro))

        analysis_text = panel_result.get("analysis_text", "")
        if analysis_text:
            story.append(Paragraph("Análisis", styles["heading"]))
            append_markdown_blocks(story, analysis_text, styles)
            panel_analyses.append(f"Panel {index} - {panel_title}\n{analysis_text}")

    page_chrome = build_page_chrome(template, schedule, styles)
    doc.build(story, onFirstPage=page_chrome, onLaterPages=page_chrome)
    return {"panel_titles": panel_titles, "panel_analyses": panel_analyses, "ai_summary": ""}


def apply_template_to_detailed_report(pdf_path, template):
    return pdf_path


async def build_detailed_dashboard_reports(schedule, dashboards, runtime_settings, client_dir, template=None):
    reports = []
    browser = None
    page = None
    try:
        browser = await launch(
            headless=True,
            args=["--no-sandbox"],
            handleSIGINT=False,
            handleSIGTERM=False,
            handleSIGHUP=False,
            autoClose=False,
        )
        page = await browser.newPage()
        await page.setViewport({"width": 1600, "height": 1100, "deviceScaleFactor": 2})
        await login_grafana(page, runtime_settings["base_url"], runtime_settings["username"], runtime_settings["password"])

        for dashboard in dashboards:
            abort_if_job_cancelled(schedule.get("_job_id"), schedule, "leitura detalhada da dashboard")
            dashboard_meta = fetch_dashboard_metadata(runtime_settings, dashboard)
            dashboard_screenshot_bytes = b""
            dashboard_url = ensure_kiosk(dashboard.get("url") or dashboard_meta.get("source_url", ""))
            if dashboard_url:
                overview_pdf_path = os.path.join(
                    client_dir,
                    f"_overview_{schedule['id']}_{dashboard_meta['uid']}.pdf",
                )
                overview_viewport = await measure_dashboard_dimensions(page, dashboard_url)
                overview_assets = await capture_dashboard_assets(page, dashboard_url, overview_pdf_path, overview_viewport)
                dashboard_screenshot_bytes = overview_assets["screenshot_bytes"]
                if os.path.exists(overview_pdf_path):
                    try:
                        os.remove(overview_pdf_path)
                    except OSError:
                        pass

            panel_results = []
            for index, panel in enumerate(dashboard_meta["panels"], start=1):
                abort_if_job_cancelled(schedule.get("_job_id"), schedule, f"captura do painel {panel.get('id')}")
                panel_url = build_panel_view_url(dashboard_meta, panel["id"])
                image_bytes = await capture_panel_image_from_view(page, panel_url, panel["id"], panel.get("panel_type", ""))
                resolved_title = panel.get("title_raw") or generate_missing_panel_title(schedule, dashboard_meta, panel, index, image_bytes)
                analysis_text = ""
                if schedule.get("use_ai"):
                    analysis_text = generate_visual_analysis(
                        schedule=schedule,
                        task_instruction=(
                            "Analiza este panel individualmente. Explica qué sugiere la visualización, qué señales llaman la atención, "
                            "si hay riesgo, anomalía, tendencia, saturación, degradación o ausencia de problema relevante."
                        ),
                        metadata_text=build_panel_metadata_text(dashboard_meta, panel, index),
                        image_bytes=image_bytes,
                        context_label=f"painel-{dashboard_meta['uid']}-{panel['id']}",
                    )
                panel_results.append(
                    {
                        "panel": panel,
                        "image_bytes": image_bytes,
                        "resolved_title": resolved_title,
                        "analysis_text": analysis_text,
                    }
                )

            safe_title = "".join(char if char.isalnum() or char in ("-", "_") else "_" for char in dashboard_meta["title"])[:80]
            pdf_path = os.path.join(
                client_dir,
                f"{datetime.now().strftime('%Y-%m-%d')}_{schedule['id']}_{safe_title or dashboard_meta['uid']}_detalhado.pdf",
            )
            details = build_detailed_dashboard_pdf(
                schedule,
                dashboard_meta,
                panel_results,
                pdf_path,
                template=template,
                dashboard_screenshot_bytes=dashboard_screenshot_bytes,
            )
            final_pdf_path = apply_template_to_detailed_report(pdf_path, template)
            reports.append(
                {
                    "uid": dashboard_meta["uid"],
                    "title": dashboard_meta["title"],
                    "url": dashboard_meta.get("source_url", dashboard.get("url", "")),
                    "pdf_path": final_pdf_path,
                    "chart_titles": details["panel_titles"],
                    "panel_analyses": details["panel_analyses"],
                    "ai_summary": details["ai_summary"],
                    "metadata_text": build_dashboard_metadata_text(dashboard_meta),
                }
            )
    finally:
        if browser is not None:
            await browser.close()
    return reports


def build_telegram_message(schedule):
    lines = [f"Agendamento {schedule['titulo']}"]
    if schedule.get("report_subject"):
        lines.append(schedule["report_subject"])
    if schedule.get("report_intro"):
        lines.append(schedule["report_intro"])
    return "\n".join(lines)


def build_email_digest_text(schedule, reports):
    title = (schedule.get("report_subject") or schedule["titulo"]).strip()
    intro = (schedule.get("report_intro") or "").strip()
    lines = [title]
    if intro:
        lines.extend(["", intro])
    return "\n".join(lines)


def send_email(recipients, subject, body_text, attachment_paths):
    if not recipients:
        log_message("Envio por e-mail ignorado: nenhum destinatario informado.")
        return
    if not smtp_is_configured():
        log_message("Envio por e-mail ignorado: SMTP nao configurado.")
        return

    smtp_settings = get_smtp_settings()
    message = MIMEMultipart()
    message["From"] = smtp_settings["from_email"]
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    message.attach(MIMEText(body_text, "plain", "utf-8"))

    for attachment_path in attachment_paths:
        with open(attachment_path, "rb") as file_handle:
            attachment = MIMEApplication(file_handle.read(), _subtype="pdf")
        attachment.add_header("Content-Disposition", "attachment", filename=os.path.basename(attachment_path))
        message.attach(attachment)

    has_credentials = bool(smtp_settings["username"] and smtp_settings["password"])

    def _send_via_smtp(use_tls):
        smtp_class = smtplib.SMTP_SSL if smtp_settings["port"] == 465 else smtplib.SMTP
        with smtp_class(smtp_settings["server"], smtp_settings["port"]) as server:
            if smtp_class is smtplib.SMTP:
                server.ehlo()
                if use_tls:
                    server.starttls()
                    server.ehlo()
            if has_credentials:
                server.login(smtp_settings["username"], smtp_settings["password"])
            server.sendmail(smtp_settings["from_email"], recipients, message.as_string())

    try:
        _send_via_smtp(smtp_settings["use_tls"])
    except smtplib.SMTPNotSupportedError:
        if has_credentials and not smtp_settings["use_tls"]:
            # Server likely requires STARTTLS before exposing AUTH — retry with TLS
            _send_via_smtp(True)
        else:
            raise

    log_message(f"Envio por e-mail concluido para {len(recipients)} destinatario(s).")
    return len(recipients)


def _resolve_telegram_recipients(chat_ids, bots):
    bots_by_id = {str(bot["id"]): bot for bot in bots if bot.get("bot_token")}
    recipients_by_bot = {}

    for recipient in chat_ids:
        if isinstance(recipient, dict):
            metadata = recipient.get("metadata") or {}
            bot_id = str(metadata.get("bot_id", "")).strip()
            chat_id = str(recipient.get("valor", "")).strip()
        else:
            bot_id = ""
            chat_id = str(recipient).strip()

        if not chat_id:
            continue

        if bot_id and bot_id in bots_by_id:
            recipients_by_bot.setdefault(bot_id, [])
            if chat_id not in recipients_by_bot[bot_id]:
                recipients_by_bot[bot_id].append(chat_id)
            continue

        matched_bot_id = ""
        for bot in bots:
            if any(str(chat.get("chat_id", "")).strip() == chat_id for chat in bot.get("selected_chats", [])):
                matched_bot_id = str(bot["id"])
                break

        if not matched_bot_id and len(bots_by_id) == 1:
            matched_bot_id = next(iter(bots_by_id.keys()))

        if matched_bot_id:
            recipients_by_bot.setdefault(matched_bot_id, [])
            if chat_id not in recipients_by_bot[matched_bot_id]:
                recipients_by_bot[matched_bot_id].append(chat_id)

    return {bot_id: chat_list for bot_id, chat_list in recipients_by_bot.items() if chat_list}


def send_telegram(chat_ids, caption, attachment_paths):
    telegram_settings = get_telegram_settings()
    bots = telegram_settings.get("bots", [])
    if not chat_ids:
        log_message("Envio por Telegram ignorado: nenhum destinatario informado.")
        return 0
    if not bots:
        log_message("Envio por Telegram ignorado: nenhum bot configurado.")
        return 0

    bots_by_id = {str(bot["id"]): bot for bot in bots if bot.get("bot_token")}
    recipients_by_bot = _resolve_telegram_recipients(chat_ids, bots)
    if not recipients_by_bot:
        log_message("Envio por Telegram ignorado: nao foi possivel associar chats aos bots configurados.")
        return 0

    total_sent = 0
    for bot_id, bot_chat_ids in recipients_by_bot.items():
        bot = bots_by_id.get(bot_id)
        if not bot:
            log_message(f"Bot Telegram {bot_id} nao encontrado para envio.")
            continue

        base_url = f"https://api.telegram.org/bot{bot['bot_token']}"
        for chat_id in bot_chat_ids:
            requests.post(
                f"{base_url}/sendMessage",
                data={"chat_id": chat_id, "text": caption[:4000]},
                timeout=30,
            ).raise_for_status()

            for attachment_path in attachment_paths:
                with open(attachment_path, "rb") as file_handle:
                    requests.post(
                        f"{base_url}/sendDocument",
                        data={"chat_id": chat_id, "caption": os.path.basename(attachment_path)},
                        files={"document": file_handle},
                        timeout=60,
                    ).raise_for_status()
        log_message(f"Envio por Telegram concluido via bot {bot.get('nome', bot_id)} para {len(bot_chat_ids)} chat(s).")
        total_sent += len(bot_chat_ids)
    return total_sent


def run_schedule(schedule, job_id=None):
    started_at = time.monotonic()
    schedule = dict(schedule)
    schedule["_job_id"] = job_id
    try:
        reports = asyncio.run(build_dashboard_reports(schedule))
    except ReportExecutionError as exc:
        log_message(f"Falha ao gerar relatorios do agendamento {schedule['id']}: {exc.details or exc}")
        create_report_execution(
            {
                "schedule_id": schedule["id"],
                "schedule_title": schedule["titulo"],
                "customer_name": schedule["nome_cliente"],
                "status": "failed",
                "error_message": str(exc),
                "error_details": exc.details or str(exc),
                "error_image_base64": exc.image_base64,
                "duration_seconds": round(time.monotonic() - started_at, 2),
                "delivery_methods": schedule.get("delivery_methods", []),
            }
        )
        raise
    except Exception as exc:
        log_message(f"Falha ao gerar relatorios do agendamento {schedule['id']}: {exc}")
        create_report_execution(
            {
                "schedule_id": schedule["id"],
                "schedule_title": schedule["titulo"],
                "customer_name": schedule["nome_cliente"],
                "status": "failed",
                "error_message": "Fallo al generar reportes.",
                "error_details": str(exc),
                "error_image_base64": build_failure_image_base64(
                    "Fallo al generar reporte",
                    [schedule.get("titulo", ""), str(exc)],
                ),
                "duration_seconds": round(time.monotonic() - started_at, 2),
                "delivery_methods": schedule.get("delivery_methods", []),
            }
        )
        raise

    if not reports:
        log_message(f"Nenhuma dashboard processada para agendamento {schedule['id']}")
        execution_id = create_report_execution(
            {
                "schedule_id": schedule["id"],
                "schedule_title": schedule["titulo"],
                "customer_name": schedule["nome_cliente"],
                "status": "failed",
                "error_message": "Ningún dashboard procesado.",
                "error_details": "El agendamiento fue ejecutado, pero no se generó ningún PDF.",
                "error_image_base64": build_failure_image_base64(
                    "Ningún dashboard procesado",
                    [schedule.get("titulo", ""), "No se generó ningún PDF."],
                ),
                "duration_seconds": round(time.monotonic() - started_at, 2),
                "delivery_methods": schedule.get("delivery_methods", []),
            }
        )
        return {"status": "failed", "execution_id": execution_id}

    body_text = build_email_digest_text(schedule, reports)
    telegram_text = build_telegram_message(schedule)

    recipients = get_schedule_recipients(schedule["id"])
    email_recipients = [item["valor"] for item in recipients if item["tipo"] == "email"]
    telegram_recipients = [item for item in recipients if item["tipo"] == "telegram"]
    attachments = [report["pdf_path"] for report in reports]
    subject = schedule.get("report_subject") or f"Reporte | {schedule['titulo']}"
    errors = []
    sent_email_count = 0
    sent_telegram_count = 0

    abort_if_job_cancelled(job_id, schedule, "preparacao do envio")

    if "email" in schedule["delivery_methods"]:
        try:
            abort_if_job_cancelled(job_id, schedule, "envio por e-mail")
            sent_email_count = send_email(email_recipients, subject, body_text, attachments) or 0
        except Exception as exc:
            log_message(f"Falha no envio por e-mail do agendamento {schedule['id']}: {exc}")
            errors.append(f"E-mail: {exc}")

    if "telegram" in schedule["delivery_methods"]:
        try:
            abort_if_job_cancelled(job_id, schedule, "envio por Telegram")
            sent_telegram_count = send_telegram(telegram_recipients, telegram_text, attachments) or 0
        except Exception as exc:
            log_message(f"Falha no envio por Telegram do agendamento {schedule['id']}: {exc}")
            errors.append(f"Telegram: {exc}")

    status = "success"
    if errors and (sent_email_count or sent_telegram_count):
        status = "partial"
    elif errors:
        status = "failed"

    execution_id = create_report_execution(
        {
            "schedule_id": schedule["id"],
            "schedule_title": schedule["titulo"],
            "customer_name": schedule["nome_cliente"],
            "status": status,
            "summary": body_text,
            "error_message": " ; ".join(errors) if errors else "",
            "error_details": "\n".join(errors) if errors else "",
            "error_image_base64": build_failure_image_base64(
                "Fallo en el envío del reporte",
                [schedule.get("titulo", "")] + errors,
            )
            if errors
            else "",
            "duration_seconds": round(time.monotonic() - started_at, 2),
            "report_count": len(reports),
            "sent_email_count": sent_email_count,
            "sent_telegram_count": sent_telegram_count,
            "delivery_methods": schedule.get("delivery_methods", []),
            "attachment_paths": attachments,
        }
    )
    return {"status": status, "execution_id": execution_id}
