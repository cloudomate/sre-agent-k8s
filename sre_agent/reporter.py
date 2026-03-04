"""
Incident Report Publisher
Formats and delivers incident reports to configured channels.
Supported channels: slack, webhook, file, stdout, sms, whatsapp, email
"""
import json
import logging
import smtplib
import ssl
import urllib.request
import urllib.parse
import urllib.error
from base64 import b64encode
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from .config import config

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Severity styling
# ─────────────────────────────────────────────────────────────

SEVERITY_EMOJI = {"P1": "🔴", "P2": "🟠", "P3": "🟡"}
SEVERITY_COLOR = {"P1": "#FF0000", "P2": "#FFA500", "P3": "#FFCC00"}
CONFIDENCE_EMOJI = {"high": "✅", "medium": "⚠️", "low": "❓"}


# ─────────────────────────────────────────────────────────────
# Formatters
# ─────────────────────────────────────────────────────────────

def format_markdown(report: dict) -> str:
    """Format report as a human-readable markdown string."""
    sev = report.get("severity", "P2")
    conf = report.get("confidence", "low")
    emoji = SEVERITY_EMOJI.get(sev, "🔵")
    conf_emoji = CONFIDENCE_EMOJI.get(conf, "❓")

    lines = [
        f"# {emoji} Incident Report: {report.get('title', 'Service Failure')}",
        f"**ID**: `{report.get('incident_id', 'N/A')}`  |  "
        f"**Severity**: {sev}  |  "
        f"**Category**: {report.get('category', 'unknown').upper()}  |  "
        f"**Confidence**: {conf_emoji} {conf.upper()}",
        f"**Service**: `{report.get('affected_service', 'unknown')}`  |  "
        f"**Namespace**: `{report.get('affected_namespace', 'unknown')}`",
        "",
        "## Summary",
        report.get("investigation_summary", "No summary available."),
        "",
        "## Root Cause",
        f"```\n{report.get('root_cause', 'unknown')}\n```",
        "",
    ]

    evidence = report.get("evidence", [])
    if evidence:
        lines.append("## Evidence")
        for e in evidence:
            lines.append(f"- {e}")
        lines.append("")

    timeline = report.get("timeline", [])
    if timeline:
        lines.append("## Timeline")
        for t in timeline:
            lines.append(f"- {t}")
        lines.append("")

    actions = report.get("recommended_actions", [])
    if actions:
        lines.append("## Recommended Actions")
        for a in actions:
            priority = a.get("priority", "short-term")
            automatable = "🤖 Auto-safe" if a.get("safe_to_automate") else "👤 Manual"
            lines.append(f"- **[{priority.upper()}]** {a.get('action', '')} _{automatable}_")
        lines.append("")

    runbooks = report.get("runbook_refs", [])
    if runbooks:
        lines.append(f"**Runbooks**: {', '.join(runbooks)}")
        lines.append("")

    if report.get("requires_escalation"):
        lines.append(f"⚠️ **ESCALATION REQUIRED**: {report.get('escalation_reason', '')}")
        lines.append("")

    meta = []
    if report.get("investigation_duration_s"):
        meta.append(f"Duration: {report['investigation_duration_s']}s")
    if report.get("tool_calls_made") is not None:
        meta.append(f"Tools used: {report['tool_calls_made']}")
    if meta:
        lines.append(f"_Investigation metadata: {' | '.join(meta)}_")

    return "\n".join(lines)


def format_sms(report: dict) -> str:
    """Format a compact plain-text alert for SMS / WhatsApp."""
    sev = report.get("severity", "P2")
    service = report.get("affected_service", "unknown")
    ns = report.get("affected_namespace", "unknown")
    root_cause = report.get("root_cause", "unknown")
    incident_id = report.get("incident_id", "N/A")
    escalation = " ⚠️ ESCALATION REQUIRED" if report.get("requires_escalation") else ""

    first_action = ""
    actions = report.get("recommended_actions", [])
    if actions:
        first_action = f"\nAction: {actions[0].get('action', '')}"

    category = report.get("category", "unknown").upper()

    return (
        f"[SRE Alert] {sev} [{category}]{escalation}\n"
        f"Service: {service} ({ns})\n"
        f"Cause: {root_cause}"
        f"{first_action}\n"
        f"ID: {incident_id}"
    )


def format_slack_payload(report: dict) -> dict:
    """Format report as a Slack Block Kit message."""
    sev = report.get("severity", "P2")
    emoji = SEVERITY_EMOJI.get(sev, "🔵")
    color = SEVERITY_COLOR.get(sev, "#808080")
    conf = report.get("confidence", "low")
    conf_emoji = CONFIDENCE_EMOJI.get(conf, "❓")

    actions_text = "\n".join(
        f"• [{a.get('priority', '').upper()}] {a.get('action', '')}"
        for a in report.get("recommended_actions", [])[:5]
    ) or "_No actions recommended_"

    evidence_text = "\n".join(
        f"• {e}" for e in report.get("evidence", [])[:6]
    ) or "_No evidence collected_"

    escalation_block = []
    if report.get("requires_escalation"):
        escalation_block = [
            {"type": "divider"},
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"⚠️ *ESCALATION REQUIRED*\n{report.get('escalation_reason', '')}",
                },
            },
        ]

    return {
        "attachments": [
            {
                "color": color,
                "blocks": [
                    {
                        "type": "header",
                        "text": {
                            "type": "plain_text",
                            "text": f"{emoji} [{sev}] {report.get('title', 'Service Failure')}",
                        },
                    },
                    {
                        "type": "section",
                        "fields": [
                            {"type": "mrkdwn", "text": f"*Incident ID*\n`{report.get('incident_id', 'N/A')}`"},
                            {"type": "mrkdwn", "text": f"*Service*\n`{report.get('affected_service', 'unknown')}`"},
                            {"type": "mrkdwn", "text": f"*Namespace*\n`{report.get('affected_namespace', 'unknown')}`"},
                            {"type": "mrkdwn", "text": f"*Category*\n`{report.get('category', 'unknown').upper()}`"},
                            {"type": "mrkdwn", "text": f"*Confidence*\n{conf_emoji} {conf.upper()}"},
                        ],
                    },
                    {"type": "divider"},
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*Summary*\n{report.get('investigation_summary', 'No summary')}",
                        },
                    },
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*Root Cause*\n```{report.get('root_cause', 'unknown')}```",
                        },
                    },
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": f"*Evidence*\n{evidence_text}"},
                    },
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": f"*Recommended Actions*\n{actions_text}"},
                    },
                    *escalation_block,
                    {
                        "type": "context",
                        "elements": [
                            {
                                "type": "mrkdwn",
                                "text": (
                                    f"SRE Agent | "
                                    f"Duration: {report.get('investigation_duration_s', '?')}s | "
                                    f"Tools: {report.get('tool_calls_made', '?')} | "
                                    f"Runbooks: {', '.join(report.get('runbook_refs', [])) or 'none'}"
                                ),
                            }
                        ],
                    },
                ],
            }
        ]
    }


def format_email_html(report: dict) -> str:
    """Format report as an HTML email body."""
    sev = report.get("severity", "P2")
    color = SEVERITY_COLOR.get(sev, "#808080")
    md = format_markdown(report)
    # Basic markdown → HTML (headers, bold, code blocks, bullets)
    html_body = md
    for level, tag in [("## ", "h2"), ("# ", "h1")]:
        lines = html_body.split("\n")
        html_body = "\n".join(
            f"<{tag}>{l[len(level):]}</{tag}>" if l.startswith(level) else l
            for l in lines
        )
    html_body = html_body.replace("```\n", "<pre><code>").replace("\n```", "</code></pre>")
    html_body = html_body.replace("**", "<strong>", 1)
    while "**" in html_body:
        html_body = html_body.replace("**", "<strong>", 1).replace("**", "</strong>", 1)
    html_body = "\n".join(
        f"<li>{l[2:]}</li>" if l.startswith("- ") else l
        for l in html_body.split("\n")
    )
    html_body = html_body.replace("\n", "<br>\n")

    return f"""<!DOCTYPE html>
<html>
<body style="font-family: monospace; max-width: 800px; margin: 0 auto; padding: 20px;">
  <div style="border-left: 6px solid {color}; padding-left: 16px;">
    {html_body}
  </div>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────
# Delivery channels
# ─────────────────────────────────────────────────────────────

def _deliver_slack(report: dict):
    webhook = config.report.slack_webhook
    if not webhook:
        logger.warning("Slack webhook not configured, skipping.")
        return
    try:
        payload = json.dumps(format_slack_payload(report)).encode()
        req = urllib.request.Request(
            webhook,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            logger.info(f"Slack delivered: {resp.getcode()}")
    except Exception as e:
        logger.error(f"Slack delivery failed: {e}")


def _deliver_webhook(report: dict):
    webhook = config.report.report_webhook
    if not webhook:
        logger.warning("Report webhook not configured, skipping.")
        return
    try:
        payload = json.dumps(report, default=str).encode()
        req = urllib.request.Request(
            webhook,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            logger.info(f"Webhook delivered: {resp.getcode()}")
    except Exception as e:
        logger.error(f"Webhook delivery failed: {e}")


def _deliver_file(report: dict):
    report_dir = Path(config.report.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    incident_id = report.get("incident_id", "unknown")
    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")

    json_path = report_dir / f"{incident_id}_{ts}.json"
    with open(json_path, "w") as f:
        json.dump(report, f, indent=2, default=str)

    md_path = report_dir / f"{incident_id}_{ts}.md"
    with open(md_path, "w") as f:
        f.write(format_markdown(report))

    logger.info(f"Report saved: {json_path} | {md_path}")


def _deliver_stdout(report: dict):
    print("\n" + "=" * 70)
    print(format_markdown(report))
    print("=" * 70 + "\n")


def _twilio_send(to: str, body: str, from_override: str = None):
    """Send a single Twilio message (SMS or WhatsApp)."""
    cfg = config.report
    sid = cfg.twilio_account_sid
    token = cfg.twilio_auth_token
    from_ = from_override or cfg.twilio_from
    if not all([sid, token, from_]):
        raise ValueError("TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, and TWILIO_FROM must be set.")

    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    auth = b64encode(f"{sid}:{token}".encode()).decode()
    data = urllib.parse.urlencode({"To": to, "From": from_, "Body": body}).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _deliver_sms(report: dict):
    recipients = config.report.sms_to
    if not recipients:
        logger.warning("SMS_TO not configured, skipping.")
        return
    body = format_sms(report)
    for number in recipients:
        try:
            result = _twilio_send(to=number.strip(), body=body)
            logger.info(f"SMS sent to {number}: sid={result.get('sid')}")
        except Exception as e:
            logger.error(f"SMS to {number} failed: {e}")


def _deliver_whatsapp(report: dict):
    recipients = config.report.whatsapp_to
    if not recipients:
        logger.warning("WHATSAPP_TO not configured, skipping.")
        return
    body = format_sms(report)  # same compact format works for WhatsApp
    cfg = config.report
    whatsapp_from = f"whatsapp:{cfg.twilio_from}" if cfg.twilio_from else None
    for number in recipients:
        wa_to = f"whatsapp:{number.strip()}"
        try:
            result = _twilio_send(to=wa_to, body=body, from_override=whatsapp_from)
            logger.info(f"WhatsApp sent to {number}: sid={result.get('sid')}")
        except Exception as e:
            logger.error(f"WhatsApp to {number} failed: {e}")


def _deliver_email(report: dict):
    cfg = config.report
    if not all([cfg.smtp_host, cfg.email_from, cfg.email_to]):
        logger.warning("Email not fully configured (SMTP_HOST, EMAIL_FROM, EMAIL_TO required), skipping.")
        return

    sev = report.get("severity", "P2")
    category = report.get("category", "unknown").upper()
    subject = (
        f"[SRE Alert] [{sev}] [{category}] {report.get('title', 'Service Failure')} "
        f"— {report.get('affected_service', 'unknown')}"
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = cfg.email_from
    msg["To"] = ", ".join(cfg.email_to)

    msg.attach(MIMEText(format_markdown(report), "plain"))
    msg.attach(MIMEText(format_email_html(report), "html"))

    try:
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=15) as server:
            server.ehlo()
            if cfg.smtp_tls:
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
            if cfg.smtp_user and cfg.smtp_password:
                server.login(cfg.smtp_user, cfg.smtp_password)
            server.sendmail(cfg.email_from, cfg.email_to, msg.as_string())
        logger.info(f"Email sent to {cfg.email_to}")
    except Exception as e:
        logger.error(f"Email delivery failed: {e}")


# ─────────────────────────────────────────────────────────────
# Main publisher
# ─────────────────────────────────────────────────────────────

CHANNEL_HANDLERS = {
    "slack":     _deliver_slack,
    "webhook":   _deliver_webhook,
    "file":      _deliver_file,
    "stdout":    _deliver_stdout,
    "sms":       _deliver_sms,
    "whatsapp":  _deliver_whatsapp,
    "email":     _deliver_email,
}


def publish_report(report: dict):
    """
    Deliver the incident report to all configured channels.
    Channels are configured via REPORT_CHANNELS env var (comma-separated).
    Supported: slack, webhook, file, stdout, sms, whatsapp, email
    """
    channels = config.report.channels
    logger.info(f"Publishing report {report.get('incident_id')} to channels: {channels}")

    for channel in channels:
        channel = channel.strip()
        handler = CHANNEL_HANDLERS.get(channel)
        if handler:
            try:
                handler(report)
            except Exception as e:
                logger.error(f"Failed to deliver to {channel}: {e}")
        else:
            logger.warning(f"Unknown report channel: {channel}")
