"""
SRE Agent Tool Definitions and Executor
All tools the agent can call during incident investigation.
Uses the Kubernetes Python client for in-cluster API access (no kubectl binary required).
"""
import json
import logging
import subprocess
import shlex
import socket
import time
import urllib.request
import urllib.parse
from datetime import datetime
from typing import Any, Optional

from kubernetes import client as k8s_client, config as k8s_config
from kubernetes.client.rest import ApiException

from .config import config

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Kubernetes client initialisation
# ─────────────────────────────────────────────────────────────

def _init_k8s():
    try:
        if config.kubernetes.in_cluster:
            k8s_config.load_incluster_config()
            logger.info("Kubernetes: using in-cluster config (ServiceAccount).")
        else:
            k8s_config.load_kube_config(
                config_file=config.kubernetes.kubeconfig,
                context=config.kubernetes.context,
            )
            logger.info("Kubernetes: using kubeconfig file.")
    except Exception as e:
        logger.warning(f"Kubernetes client init failed: {e}. K8s tools will return errors.")

_init_k8s()


# ─────────────────────────────────────────────────────────────
# Tool result schema
# ─────────────────────────────────────────────────────────────

def ok(data: Any) -> dict:
    return {"status": "ok", "data": data}

def err(msg: str) -> dict:
    return {"status": "error", "error": msg}


# ─────────────────────────────────────────────────────────────
# Safety guard for non-k8s shell commands
# ─────────────────────────────────────────────────────────────

def _is_safe_command(cmd: str) -> bool:
    cmd_lower = cmd.strip().lower()
    for blocked in config.blocked_commands:
        if blocked in cmd_lower:
            return False
    for safe_prefix in config.safe_commands:
        if cmd_lower.startswith(safe_prefix.lower()):
            return True
    return False


def _run_shell(cmd: str, timeout: int = 15) -> dict:
    if not _is_safe_command(cmd):
        return err(f"Command blocked by safety policy: '{cmd}'")
    try:
        result = subprocess.run(
            shlex.split(cmd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return ok({
            "stdout": result.stdout[-3000:],
            "stderr": result.stderr[-500:],
            "returncode": result.returncode,
        })
    except subprocess.TimeoutExpired:
        return err(f"Command timed out after {timeout}s: {cmd}")
    except FileNotFoundError as e:
        return err(f"Command not found: {e}")
    except Exception as e:
        return err(f"Shell error: {e}")


# ─────────────────────────────────────────────────────────────
# Kubernetes helpers
# ─────────────────────────────────────────────────────────────

def _pod_summary(pod) -> dict:
    """Distil a V1Pod object into a compact summary dict."""
    status = pod.status
    containers = []
    for cs in (status.container_statuses or []):
        state_name = "unknown"
        state_detail = {}
        if cs.state.running:
            state_name = "running"
            state_detail = {"started_at": str(cs.state.running.started_at)}
        elif cs.state.waiting:
            state_name = "waiting"
            state_detail = {
                "reason": cs.state.waiting.reason,
                "message": cs.state.waiting.message,
            }
        elif cs.state.terminated:
            state_name = "terminated"
            state_detail = {
                "reason": cs.state.terminated.reason,
                "exit_code": cs.state.terminated.exit_code,
                "message": cs.state.terminated.message,
            }
        containers.append({
            "name": cs.name,
            "ready": cs.ready,
            "restart_count": cs.restart_count,
            "state": state_name,
            **state_detail,
        })
    return {
        "name": pod.metadata.name,
        "namespace": pod.metadata.namespace,
        "phase": status.phase,
        "conditions": [
            {"type": c.type, "status": c.status, "reason": c.reason}
            for c in (status.conditions or [])
        ],
        "containers": containers,
        "node": spec_node_name if (spec_node_name := pod.spec.node_name) else None,
        "start_time": str(status.start_time) if status.start_time else None,
    }


# ─────────────────────────────────────────────────────────────
# Individual tool implementations
# ─────────────────────────────────────────────────────────────

def get_service_logs(
    service: str,
    lines: int = 100,
    since: str = "10m",
    namespace: Optional[str] = None,
    container: Optional[str] = None,
) -> dict:
    """Fetch recent logs for a service/pod via the Kubernetes API or Loki."""
    ns = namespace or config.kubernetes.default_namespace
    lines = min(lines, config.logs.max_lines)

    if config.logs.backend == "loki" and config.logs.loki_url:
        try:
            end = int(time.time() * 1e9)
            duration_map = {"5m": 300, "10m": 600, "30m": 1800, "1h": 3600}
            secs = duration_map.get(since, 600)
            start = int((time.time() - secs) * 1e9)
            query = f'{{app="{service}"}}'
            params = urllib.parse.urlencode({
                "query": query, "start": start,
                "end": end, "limit": lines,
            })
            url = f"{config.logs.loki_url}/loki/api/v1/query_range?{params}"
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read())
            entries = []
            for stream in data.get("data", {}).get("result", []):
                for ts, line in stream.get("values", []):
                    entries.append(line)
            return ok("\n".join(entries[-lines:]))
        except Exception as e:
            return err(f"Loki query failed: {e}")

    if config.logs.backend == "journald":
        cmd = f"journalctl -u {service} -n {lines} --since '-{since}' --no-pager"
        return _run_shell(cmd)

    # Default: Kubernetes API
    try:
        core = k8s_client.CoreV1Api()
        duration_map = {"5m": 300, "10m": 600, "30m": 1800, "1h": 3600}
        since_seconds = duration_map.get(since, 600)

        pods = core.list_namespaced_pod(
            namespace=ns,
            label_selector=f"app={service}",
        )
        if not pods.items:
            return err(f"No pods found with label app={service} in namespace {ns}")

        all_logs = []
        for pod in pods.items[:3]:  # limit to 3 pods
            try:
                log = core.read_namespaced_pod_log(
                    name=pod.metadata.name,
                    namespace=ns,
                    container=container,
                    tail_lines=lines,
                    since_seconds=since_seconds,
                )
                all_logs.append(f"=== Pod: {pod.metadata.name} ===\n{log}")
            except ApiException as e:
                all_logs.append(f"=== Pod: {pod.metadata.name} — log error: {e.reason} ===")

        return ok("\n".join(all_logs)[-4000:])
    except ApiException as e:
        return err(f"Kubernetes API error: {e.reason}")
    except Exception as e:
        return err(f"Log fetch error: {e}")


def get_pod_status(
    namespace: Optional[str] = None,
    label_selector: Optional[str] = None,
    pod_name: Optional[str] = None,
) -> dict:
    """Get status of pods by label selector or name."""
    ns = namespace or config.kubernetes.default_namespace
    try:
        core = k8s_client.CoreV1Api()
        if pod_name:
            pod = core.read_namespaced_pod(name=pod_name, namespace=ns)
            return ok(_pod_summary(pod))
        else:
            kwargs = {"namespace": ns}
            if label_selector:
                kwargs["label_selector"] = label_selector
            pods = core.list_namespaced_pod(**kwargs)
            return ok([_pod_summary(p) for p in pods.items])
    except ApiException as e:
        return err(f"Kubernetes API error: {e.reason}")
    except Exception as e:
        return err(f"Pod status error: {e}")


def get_node_metrics(node_name: Optional[str] = None) -> dict:
    """Get CPU, memory usage across nodes via Prometheus or metrics-server."""
    if config.metrics.backend == "prometheus":
        queries = {
            "cpu_usage_pct": 'round(100 - (avg by(instance)(rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100), 0.1)',
            "mem_usage_pct": 'round((1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100, 0.1)',
            "disk_usage_pct": 'round((1 - node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"}) * 100, 0.1)',
        }
        results = {}
        for name, query in queries.items():
            try:
                params = urllib.parse.urlencode({"query": query})
                url = f"{config.metrics.prometheus_url}/api/v1/query?{params}"
                with urllib.request.urlopen(url, timeout=8) as resp:
                    data = json.loads(resp.read())
                results[name] = data.get("data", {}).get("result", [])
            except Exception as e:
                results[name] = f"error: {e}"
        return ok(results)

    # Fallback: list nodes from K8s API for basic info
    try:
        core = k8s_client.CoreV1Api()
        if node_name:
            node = core.read_node(name=node_name)
            nodes = [node]
        else:
            nodes = core.list_node().items

        summaries = []
        for n in nodes:
            conds = {c.type: c.status for c in (n.status.conditions or [])}
            capacity = n.status.capacity or {}
            allocatable = n.status.allocatable or {}
            summaries.append({
                "name": n.metadata.name,
                "ready": conds.get("Ready", "Unknown"),
                "capacity": {"cpu": capacity.get("cpu"), "memory": capacity.get("memory")},
                "allocatable": {"cpu": allocatable.get("cpu"), "memory": allocatable.get("memory")},
                "conditions": conds,
            })
        return ok(summaries)
    except ApiException as e:
        return err(f"Kubernetes API error: {e.reason}")
    except Exception as e:
        return err(f"Node metrics error: {e}")


def get_service_metrics(service: str, namespace: Optional[str] = None) -> dict:
    """Get request rate, error rate, latency for a service (RED method)."""
    if config.metrics.backend != "prometheus":
        return err("Prometheus backend required for service metrics. Set METRICS_BACKEND=prometheus.")

    queries = {
        "request_rate_rps": f'sum(rate(http_requests_total{{app="{service}"}}[5m]))',
        "error_rate_pct": f'round(sum(rate(http_requests_total{{app="{service}",status=~"5.."}}[5m])) / sum(rate(http_requests_total{{app="{service}"}}[5m])) * 100, 0.1)',
        "p99_latency_ms": f'histogram_quantile(0.99, sum by (le)(rate(http_request_duration_seconds_bucket{{app="{service}"}}[5m]))) * 1000',
        "pod_restarts": f'sum(kube_pod_container_status_restarts_total{{container="{service}"}})',
    }
    results = {}
    for name, query in queries.items():
        try:
            params = urllib.parse.urlencode({"query": query})
            url = f"{config.metrics.prometheus_url}/api/v1/query?{params}"
            with urllib.request.urlopen(url, timeout=8) as resp:
                data = json.loads(resp.read())
            r = data.get("data", {}).get("result", [])
            results[name] = r[0]["value"][1] if r else "no_data"
        except Exception as e:
            results[name] = f"error: {e}"
    return ok(results)


def run_diagnostic_cmd(cmd: str) -> dict:
    """Run a safe non-k8s diagnostic shell command (df, free, ps, curl, ping, etc.)."""
    return _run_shell(cmd)


def check_network_connectivity(host: str, port: int = 80, timeout: int = 5) -> dict:
    """Check if a host:port is reachable."""
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.close()
        return ok({"reachable": True, "host": host, "port": port})
    except socket.timeout:
        return ok({"reachable": False, "reason": "timeout", "host": host, "port": port})
    except ConnectionRefusedError:
        return ok({"reachable": False, "reason": "connection_refused", "host": host, "port": port})
    except Exception as e:
        return err(str(e))


def get_recent_events(namespace: Optional[str] = None, since_minutes: int = 30) -> dict:
    """Get Kubernetes warning events from the past N minutes."""
    ns = namespace or config.kubernetes.default_namespace
    try:
        core = k8s_client.CoreV1Api()
        events = core.list_namespaced_event(
            namespace=ns,
            field_selector="type=Warning",
        )
        summaries = []
        for e in sorted(events.items, key=lambda x: x.last_timestamp or datetime.min, reverse=True)[:50]:
            summaries.append({
                "reason": e.reason,
                "message": e.message,
                "object": f"{e.involved_object.kind}/{e.involved_object.name}",
                "count": e.count,
                "last_seen": str(e.last_timestamp),
            })
        return ok(summaries)
    except ApiException as e:
        return err(f"Kubernetes API error: {e.reason}")
    except Exception as e:
        return err(f"Events error: {e}")


def get_service_health(endpoint: str, expected_status: int = 200) -> dict:
    """HTTP health check against a service endpoint."""
    try:
        req = urllib.request.Request(endpoint)
        req.add_header("User-Agent", "sre-agent/1.0")
        start = time.time()
        with urllib.request.urlopen(req, timeout=8) as resp:
            latency_ms = round((time.time() - start) * 1000, 1)
            status = resp.getcode()
            body = resp.read(500).decode("utf-8", errors="ignore")
        return ok({
            "endpoint": endpoint,
            "http_status": status,
            "healthy": status == expected_status,
            "latency_ms": latency_ms,
            "body_preview": body,
        })
    except urllib.error.HTTPError as e:
        return ok({"endpoint": endpoint, "http_status": e.code, "healthy": False})
    except Exception as e:
        return ok({"endpoint": endpoint, "reachable": False, "error": str(e)})


def get_resource_quotas(namespace: Optional[str] = None) -> dict:
    """Check resource quotas and limits in a namespace."""
    ns = namespace or config.kubernetes.default_namespace
    try:
        core = k8s_client.CoreV1Api()
        quotas = core.list_namespaced_resource_quota(namespace=ns)
        result = []
        for q in quotas.items:
            result.append({
                "name": q.metadata.name,
                "hard": q.status.hard,
                "used": q.status.used,
            })
        return ok(result if result else "No resource quotas defined in this namespace.")
    except ApiException as e:
        return err(f"Kubernetes API error: {e.reason}")
    except Exception as e:
        return err(f"Resource quota error: {e}")


def get_deployment_status(deployment: str, namespace: Optional[str] = None) -> dict:
    """Check rollout status and replica counts for a deployment."""
    ns = namespace or config.kubernetes.default_namespace
    try:
        apps = k8s_client.AppsV1Api()
        d = apps.read_namespaced_deployment(name=deployment, namespace=ns)
        spec = d.spec
        status = d.status
        return ok({
            "name": d.metadata.name,
            "namespace": ns,
            "desired_replicas": spec.replicas,
            "ready_replicas": status.ready_replicas,
            "available_replicas": status.available_replicas,
            "updated_replicas": status.updated_replicas,
            "conditions": [
                {"type": c.type, "status": c.status, "reason": c.reason, "message": c.message}
                for c in (status.conditions or [])
            ],
            "strategy": d.spec.strategy.type if d.spec.strategy else None,
        })
    except ApiException as e:
        return err(f"Kubernetes API error: {e.reason}")
    except Exception as e:
        return err(f"Deployment status error: {e}")


def get_hci_vm_status(vm_name: Optional[str] = None, namespace: Optional[str] = None) -> dict:
    """Get Harvester/KubeVirt VM instance status via the CustomObjects API."""
    ns = namespace or config.kubernetes.default_namespace
    try:
        custom = k8s_client.CustomObjectsApi()
        if vm_name:
            vmi = custom.get_namespaced_custom_object(
                group="kubevirt.io", version="v1",
                namespace=ns, plural="virtualmachineinstances", name=vm_name,
            )
            status = vmi.get("status", {})
            return ok({
                "name": vmi["metadata"]["name"],
                "namespace": ns,
                "phase": status.get("phase"),
                "node": status.get("nodeName"),
                "conditions": status.get("conditions", []),
                "interfaces": status.get("interfaces", []),
            })
        else:
            vmis = custom.list_namespaced_custom_object(
                group="kubevirt.io", version="v1",
                namespace=ns, plural="virtualmachineinstances",
            )
            return ok([
                {
                    "name": v["metadata"]["name"],
                    "phase": v.get("status", {}).get("phase"),
                    "node": v.get("status", {}).get("nodeName"),
                }
                for v in vmis.get("items", [])
            ])
    except ApiException as e:
        if e.status == 404:
            return ok("No VirtualMachineInstances found (KubeVirt may not be installed).")
        return err(f"Kubernetes API error: {e.reason}")
    except Exception as e:
        return err(f"HCI VM status error: {e}")


def get_active_alerts(
    filter_labels: Optional[str] = None,
    severity: Optional[str] = None,
) -> dict:
    """Query Alertmanager for currently firing alerts."""
    try:
        url = f"{config.metrics.alertmanager_url}/api/v2/alerts?active=true&silenced=false&inhibited=false"
        if filter_labels:
            url += f"&filter={urllib.parse.quote(filter_labels)}"
        with urllib.request.urlopen(url, timeout=8) as resp:
            alerts = json.loads(resp.read())

        results = []
        for a in alerts:
            labels = a.get("labels", {})
            if severity and labels.get("severity", "").lower() != severity.lower():
                continue
            results.append({
                "alertname": labels.get("alertname"),
                "severity": labels.get("severity"),
                "namespace": labels.get("namespace"),
                "pod": labels.get("pod"),
                "service": labels.get("service"),
                "summary": a.get("annotations", {}).get("summary"),
                "description": a.get("annotations", {}).get("description"),
                "starts_at": a.get("startsAt"),
            })
        return ok(results if results else "No active alerts matching the filter.")
    except Exception as e:
        return err(f"Alertmanager query failed: {e}")


def escalate_to_human(reason: str, severity: str = "P2") -> dict:
    """Signal that this incident requires human intervention."""
    return ok({
        "escalated": True,
        "severity": severity,
        "reason": reason,
        "timestamp": datetime.utcnow().isoformat(),
        "message": "Incident marked for human escalation.",
    })


# ─────────────────────────────────────────────────────────────
# Tool registry for the agent
# ─────────────────────────────────────────────────────────────

TOOL_REGISTRY = {
    "get_service_logs": get_service_logs,
    "get_pod_status": get_pod_status,
    "get_node_metrics": get_node_metrics,
    "get_service_metrics": get_service_metrics,
    "get_active_alerts": get_active_alerts,
    "run_diagnostic_cmd": run_diagnostic_cmd,
    "check_network_connectivity": check_network_connectivity,
    "get_recent_events": get_recent_events,
    "get_service_health": get_service_health,
    "get_resource_quotas": get_resource_quotas,
    "get_deployment_status": get_deployment_status,
    "get_hci_vm_status": get_hci_vm_status,
    "escalate_to_human": escalate_to_human,
}


# Ollama/OpenAI-compatible tool definitions (function calling format)
TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "get_service_logs",
            "description": "Fetch recent logs for a service or pod. Use this first to see error messages, stack traces, and crash reasons.",
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {"type": "string", "description": "Service name or app label"},
                    "lines": {"type": "integer", "description": "Number of log lines (default 100)", "default": 100},
                    "since": {"type": "string", "description": "How far back to look: 5m, 10m, 30m, 1h", "default": "10m"},
                    "namespace": {"type": "string", "description": "Kubernetes namespace"},
                    "container": {"type": "string", "description": "Specific container name if pod has multiple"},
                },
                "required": ["service"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_pod_status",
            "description": "Get status of pods. Use to check if pods are Running/Pending/CrashLoopBackOff, see restart counts, and view container details.",
            "parameters": {
                "type": "object",
                "properties": {
                    "namespace": {"type": "string"},
                    "label_selector": {"type": "string", "description": "e.g. 'app=myservice'"},
                    "pod_name": {"type": "string", "description": "Specific pod name for detailed describe"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_node_metrics",
            "description": "Get CPU, memory, disk usage across cluster nodes. Use to detect resource exhaustion.",
            "parameters": {
                "type": "object",
                "properties": {
                    "node_name": {"type": "string", "description": "Specific node name, or omit for all nodes"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_service_metrics",
            "description": "Get RED metrics (request rate, error rate, latency) for a service from Prometheus.",
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {"type": "string", "description": "Service/app name"},
                    "namespace": {"type": "string"},
                },
                "required": ["service"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_active_alerts",
            "description": "Query Alertmanager for currently firing alerts. Use early in an investigation to get a full picture of what is alerting in the cluster.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filter_labels": {"type": "string", "description": "Alertmanager label filter, e.g. '{namespace=\"production\"}'"},
                    "severity": {"type": "string", "description": "Filter by severity: critical, warning, info"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_diagnostic_cmd",
            "description": "Run a safe non-Kubernetes diagnostic shell command. Only df, free, ps, netstat, ss, ping, curl, nslookup, journalctl, systemctl status are allowed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cmd": {"type": "string", "description": "The command to run"},
                },
                "required": ["cmd"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_network_connectivity",
            "description": "Check if a host and port are reachable. Use to diagnose network partitions or service discovery issues.",
            "parameters": {
                "type": "object",
                "properties": {
                    "host": {"type": "string"},
                    "port": {"type": "integer", "default": 80},
                    "timeout": {"type": "integer", "default": 5},
                },
                "required": ["host"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recent_events",
            "description": "Get Kubernetes warning events from the past 30 minutes. Use to spot OOMKilled, FailedScheduling, ImagePullBackOff etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "namespace": {"type": "string"},
                    "since_minutes": {"type": "integer", "default": 30},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_service_health",
            "description": "HTTP health check against a service endpoint. Returns status code, latency, and response preview.",
            "parameters": {
                "type": "object",
                "properties": {
                    "endpoint": {"type": "string", "description": "Full URL e.g. http://myservice.namespace.svc.cluster.local:8080/health"},
                    "expected_status": {"type": "integer", "default": 200},
                },
                "required": ["endpoint"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_deployment_status",
            "description": "Check rollout status and replica counts for a Kubernetes deployment.",
            "parameters": {
                "type": "object",
                "properties": {
                    "deployment": {"type": "string"},
                    "namespace": {"type": "string"},
                },
                "required": ["deployment"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_hci_vm_status",
            "description": "Get Harvester/KubeVirt VM instance status. Use for HCI platform VM-related failures.",
            "parameters": {
                "type": "object",
                "properties": {
                    "vm_name": {"type": "string", "description": "VM instance name, or omit for all"},
                    "namespace": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "escalate_to_human",
            "description": "Mark this incident for human escalation. Use when the root cause is unclear after investigation, when the fix requires destructive actions, or when the incident is critical.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "Why human intervention is needed"},
                    "severity": {"type": "string", "enum": ["P1", "P2", "P3"], "default": "P2"},
                },
                "required": ["reason"],
            },
        },
    },
]


def execute_tool(name: str, arguments: dict) -> dict:
    """Dispatch a tool call from the agent to the actual implementation."""
    if name not in TOOL_REGISTRY:
        return err(f"Unknown tool: {name}")
    try:
        logger.info(f"Tool call: {name}({json.dumps(arguments, default=str)})")
        result = TOOL_REGISTRY[name](**arguments)
        logger.debug(f"Tool result: {json.dumps(result, default=str)[:500]}")
        return result
    except TypeError as e:
        return err(f"Invalid arguments for {name}: {e}")
    except Exception as e:
        logger.exception(f"Tool {name} raised exception")
        return err(f"Tool error: {e}")
