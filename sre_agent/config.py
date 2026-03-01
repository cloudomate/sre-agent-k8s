"""
SRE Agent Configuration
Loads from environment variables or .env file
"""
import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class LLMConfig:
    # Supports any OpenAI-compatible endpoint: Ollama, vLLM, OpenAI, etc.
    base_url: str = os.getenv("LLM_BASE_URL", "http://ollama:11434/v1")
    api_key: str = os.getenv("LLM_API_KEY", "ollama")  # "ollama" for local, real key for cloud
    model: str = os.getenv("LLM_MODEL", "qwen3:4b")
    embed_model: str = os.getenv("LLM_EMBED_MODEL", "nomic-embed-text")
    timeout: int = int(os.getenv("LLM_TIMEOUT", "120"))
    max_agent_steps: int = int(os.getenv("AGENT_MAX_STEPS", "12"))


@dataclass
class KubernetesConfig:
    in_cluster: bool = os.getenv("K8S_IN_CLUSTER", "true").lower() == "true"
    kubeconfig: Optional[str] = os.getenv("KUBECONFIG", None)
    context: Optional[str] = os.getenv("K8S_CONTEXT", None)
    default_namespace: str = os.getenv("K8S_NAMESPACE", "default")


@dataclass
class LogConfig:
    # Log sources: "kubernetes", "file", "journald", "loki"
    backend: str = os.getenv("LOG_BACKEND", "kubernetes")
    loki_url: Optional[str] = os.getenv("LOKI_URL", None)
    log_dir: str = os.getenv("LOG_DIR", "/var/log/services")
    max_lines: int = int(os.getenv("LOG_MAX_LINES", "200"))


@dataclass
class MetricsConfig:
    # Metrics sources: "prometheus", "none"
    backend: str = os.getenv("METRICS_BACKEND", "prometheus")
    prometheus_url: str = os.getenv("PROMETHEUS_URL", "http://kube-prometheus-stack-prometheus.monitoring.svc.cluster.local:9090")
    alertmanager_url: str = os.getenv("ALERTMANAGER_URL", "http://kube-prometheus-stack-alertmanager.monitoring.svc.cluster.local:9093")


@dataclass
class ReportConfig:
    # Output channels: "slack", "webhook", "file", "stdout", "sms", "whatsapp", "email"
    channels: list = field(default_factory=lambda: os.getenv(
        "REPORT_CHANNELS", "stdout,file"
    ).split(","))
    slack_webhook: Optional[str] = os.getenv("SLACK_WEBHOOK_URL", None)
    report_webhook: Optional[str] = os.getenv("REPORT_WEBHOOK_URL", None)
    report_dir: str = os.getenv("REPORT_DIR", "./reports")
    runbook_dir: str = os.getenv("RUNBOOK_DIR", "./runbooks")

    # SMS / WhatsApp via Twilio
    twilio_account_sid: Optional[str] = os.getenv("TWILIO_ACCOUNT_SID", None)
    twilio_auth_token: Optional[str] = os.getenv("TWILIO_AUTH_TOKEN", None)
    twilio_from: Optional[str] = os.getenv("TWILIO_FROM", None)       # e.g. +15551234567
    sms_to: list = field(default_factory=lambda: [n for n in os.getenv("SMS_TO", "").split(",") if n.strip()])
    whatsapp_to: list = field(default_factory=lambda: [n for n in os.getenv("WHATSAPP_TO", "").split(",") if n.strip()])

    # Email via SMTP
    smtp_host: Optional[str] = os.getenv("SMTP_HOST", None)
    smtp_port: int = int(os.getenv("SMTP_PORT", "587"))
    smtp_tls: bool = os.getenv("SMTP_TLS", "true").lower() == "true"
    smtp_user: Optional[str] = os.getenv("SMTP_USER", None)
    smtp_password: Optional[str] = os.getenv("SMTP_PASSWORD", None)
    email_from: Optional[str] = os.getenv("EMAIL_FROM", None)
    email_to: list = field(default_factory=lambda: [e for e in os.getenv("EMAIL_TO", "").split(",") if e.strip()])


@dataclass
class AgentConfig:
    llm: LLMConfig = field(default_factory=LLMConfig)
    kubernetes: KubernetesConfig = field(default_factory=KubernetesConfig)
    logs: LogConfig = field(default_factory=LogConfig)
    metrics: MetricsConfig = field(default_factory=MetricsConfig)
    report: ReportConfig = field(default_factory=ReportConfig)

    # Safety: commands the agent is NEVER allowed to run
    blocked_commands: list = field(default_factory=lambda: [
        "rm", "delete", "shutdown", "reboot", "poweroff",
        "dd", "mkfs", "fdisk", "format",
    ])

    # Non-k8s diagnostic commands that are safe to run
    safe_commands: list = field(default_factory=lambda: [
        "systemctl status", "journalctl",
        "df", "free", "top", "ps", "netstat", "ss",
        "ping", "curl --max-time 5", "nslookup",
        "cat /proc/", "cat /sys/", "uname",
    ])


# Singleton config instance
config = AgentConfig()
