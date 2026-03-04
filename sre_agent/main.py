"""
SRE Agent - FastAPI Server
Receives incident events and triggers investigation asynchronously.
Exposes REST API for your HCI portal to call.
"""
import asyncio
import json
import logging
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .agent import SREAgent
from .runbook import RunbookSearcher
from .reporter import publish_report
from .config import config
from .watcher import run_event_watcher

# ─────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("sre_agent.server")

# ─────────────────────────────────────────────────────────────
# App setup
# ─────────────────────────────────────────────────────────────

app = FastAPI(
    title="SRE Agent",
    description="Active SRE investigation agent for HCI platforms",
    version="1.0.0",
)

# Shared state
_runbook_searcher: Optional[RunbookSearcher] = None
_agent: Optional[SREAgent] = None
_executor = ThreadPoolExecutor(max_workers=2)

# In-memory incident store (replace with Redis/DB for production)
_incidents: dict[str, dict] = {}

# Background event watcher
_watcher_stop = threading.Event()
_watcher_thread: Optional[threading.Thread] = None


# ─────────────────────────────────────────────────────────────
# Startup
# ─────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    global _runbook_searcher, _agent, _watcher_thread
    logger.info("Initializing SRE Agent...")
    loop = asyncio.get_event_loop()
    # Initialize in background thread (may trigger model pull)
    _runbook_searcher = await loop.run_in_executor(_executor, RunbookSearcher)
    _agent = await loop.run_in_executor(_executor, lambda: SREAgent(_runbook_searcher))
    logger.info("SRE Agent ready.")

    # Start Kubernetes event watcher if enabled
    if os.getenv("WATCH_EVENTS", "true").lower() == "true":
        _watcher_stop.clear()
        _watcher_thread = threading.Thread(
            target=run_event_watcher,
            args=(_enqueue_from_watcher, _watcher_stop),
            daemon=True,
            name="k8s-event-watcher",
        )
        _watcher_thread.start()


@app.on_event("shutdown")
async def shutdown():
    _watcher_stop.set()
    if _watcher_thread and _watcher_thread.is_alive():
        _watcher_thread.join(timeout=5)


# ─────────────────────────────────────────────────────────────
# Request/Response models
# ─────────────────────────────────────────────────────────────

class IncidentRequest(BaseModel):
    service: str = Field(..., description="Service or component name that failed")
    namespace: Optional[str] = Field(None, description="Kubernetes namespace")
    error_type: Optional[str] = Field(None, description="Brief error description: OOMKilled, 5xx, timeout, etc.")
    alert_source: Optional[str] = Field("manual", description="Who triggered this: prometheus, healthcheck, manual")
    severity: Optional[str] = Field("P2", description="Initial severity: P1, P2, P3")
    extra_context: Optional[dict] = Field(None, description="Any additional metadata from your portal")


class IncidentResponse(BaseModel):
    incident_id: str
    status: str
    message: str
    report_url: Optional[str] = None


# ─────────────────────────────────────────────────────────────
# Background investigation task
# ─────────────────────────────────────────────────────────────

def _enqueue_from_watcher(service: str, namespace: str, error_type: str,
                          severity: str, alert_source: str):
    """Called by the background watcher thread to queue a new investigation."""
    if not _agent:
        return
    incident_id = f"inc-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-{str(uuid.uuid4())[:6]}"
    incident_data = {
        "incident_id": incident_id,
        "service": service,
        "namespace": namespace,
        "error_type": error_type,
        "alert_source": alert_source,
        "severity": severity,
        "extra_context": {},
        "created_at": datetime.utcnow().isoformat(),
    }
    _incidents[incident_id] = {
        "status": "queued",
        "incident": incident_data,
        "created_at": datetime.utcnow().isoformat(),
        "report": None,
    }
    _executor.submit(_run_investigation, incident_id, incident_data)
    logger.info(f"[watcher] Queued investigation {incident_id} for {service}/{namespace}")


def _run_investigation(incident_id: str, incident_data: dict):
    """Run investigation in thread pool and publish report."""
    try:
        _incidents[incident_id]["status"] = "investigating"
        logger.info(f"[{incident_id}] Investigation started")

        report = _agent.investigate(incident_data)
        report["incident_id"] = incident_id

        _incidents[incident_id]["status"] = "complete"
        _incidents[incident_id]["report"] = report
        _incidents[incident_id]["severity"] = report.get("severity", "P2")
        _incidents[incident_id]["category"] = report.get("category", "unknown")
        _incidents[incident_id]["completed_at"] = datetime.utcnow().isoformat()

        publish_report(report)
        logger.info(f"[{incident_id}] Investigation complete. Severity={report.get('severity')} Confidence={report.get('confidence')}")

    except Exception as e:
        logger.exception(f"[{incident_id}] Investigation failed")
        _incidents[incident_id]["status"] = "error"
        _incidents[incident_id]["error"] = str(e)


# ─────────────────────────────────────────────────────────────
# API Routes
# ─────────────────────────────────────────────────────────────

@app.post("/incidents", response_model=IncidentResponse, status_code=202)
async def create_incident(
    body: IncidentRequest,
    background_tasks: BackgroundTasks,
):
    """
    Trigger a new SRE investigation.
    Returns immediately with incident ID; investigation runs in background.
    Poll /incidents/{id} for results.
    """
    if not _agent:
        raise HTTPException(status_code=503, detail="Agent not initialized yet")

    incident_id = f"inc-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-{str(uuid.uuid4())[:6]}"
    incident_data = {
        "incident_id": incident_id,
        "service": body.service,
        "namespace": body.namespace or config.kubernetes.default_namespace,
        "error_type": body.error_type or "unknown failure",
        "alert_source": body.alert_source,
        "severity": body.severity,
        "extra_context": body.extra_context or {},
        "created_at": datetime.utcnow().isoformat(),
    }

    _incidents[incident_id] = {
        "status": "queued",
        "incident": incident_data,
        "created_at": datetime.utcnow().isoformat(),
        "report": None,
    }

    # Run investigation in thread pool (Ollama is sync)
    loop = asyncio.get_event_loop()
    loop.run_in_executor(_executor, _run_investigation, incident_id, incident_data)

    return IncidentResponse(
        incident_id=incident_id,
        status="queued",
        message="Investigation started. Poll /incidents/{id} for results.",
        report_url=f"/incidents/{incident_id}",
    )


@app.get("/incidents/{incident_id}")
async def get_incident(incident_id: str):
    """Get the status and report for an incident."""
    incident = _incidents.get(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    return JSONResponse(content=incident)


@app.get("/incidents")
async def list_incidents(
    limit: int = 20,
    severity: Optional[str] = None,
    category: Optional[str] = None,
):
    """List recent incidents. Optionally filter by severity (P1/P2/P3) or category (infra/app/storage/network)."""
    items = list(_incidents.values())
    if severity:
        items = [i for i in items if i.get("severity") == severity]
    if category:
        items = [i for i in items if i.get("category") == category]
    items = sorted(items, key=lambda x: x.get("created_at", ""), reverse=True)[:limit]
    return {"incidents": items, "total": len(_incidents)}


@app.post("/incidents/webhook/alertmanager")
async def alertmanager_webhook(request: Request):
    """
    Receive Prometheus Alertmanager webhooks and auto-trigger investigation.
    Configure in alertmanager.yml:
      receivers:
        - name: sre-agent
          webhook_configs:
            - url: http://sre-agent:8080/incidents/webhook/alertmanager
    """
    if not _agent:
        raise HTTPException(status_code=503, detail="Agent not initialized")

    body = await request.json()
    results = []

    for alert in body.get("alerts", []):
        # Only investigate firing alerts — skip resolved ones
        if alert.get("status") == "resolved":
            continue

        labels = alert.get("labels", {})
        annotations = alert.get("annotations", {})
        service = labels.get("service") or labels.get("app") or labels.get("job", "unknown")
        namespace = labels.get("namespace", config.kubernetes.default_namespace)

        incident_req = IncidentRequest(
            service=service,
            namespace=namespace,
            error_type=labels.get("alertname", "prometheus_alert"),
            alert_source="alertmanager",
            severity=_map_prometheus_severity(labels.get("severity", "warning")),
            extra_context={
                "alert_name": labels.get("alertname"),
                "description": annotations.get("description", ""),
                "summary": annotations.get("summary", ""),
                "labels": labels,
                "fingerprint": alert.get("fingerprint"),
            },
        )

        background_tasks = BackgroundTasks()
        resp = await create_incident(incident_req, background_tasks)
        results.append(resp.dict())

    return {"triggered": len(results), "incidents": results}


@app.post("/incidents/webhook/harvester")
async def harvester_webhook(request: Request):
    """
    Receive webhook from Harvester HCI portal when a VM or service fails.
    Set this URL in your Harvester portal notification settings.
    """
    if not _agent:
        raise HTTPException(status_code=503, detail="Agent not initialized")

    body = await request.json()

    # Harvester event schema (adapt to your portal's actual schema)
    event_type = body.get("type", "unknown")
    resource = body.get("resource", {})

    service = resource.get("name") or resource.get("vm_name", "unknown")
    namespace = resource.get("namespace", config.kubernetes.default_namespace)
    error = body.get("message") or body.get("reason", event_type)

    incident_req = IncidentRequest(
        service=service,
        namespace=namespace,
        error_type=error,
        alert_source="harvester",
        severity="P2",
        extra_context=body,
    )

    background_tasks = BackgroundTasks()
    return await create_incident(incident_req, background_tasks)


@app.get("/runbooks")
async def list_runbooks(query: Optional[str] = None, limit: int = 10):
    """Browse or search runbooks."""
    if not _runbook_searcher:
        raise HTTPException(status_code=503, detail="Not initialized")
    if query:
        results = _runbook_searcher.search(query, top_k=limit)
    else:
        results = _runbook_searcher.runbooks[:limit]
    return {"runbooks": results, "total": len(_runbook_searcher.runbooks)}


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {
        "status": "ok",
        "agent_ready": _agent is not None,
        "model": config.llm.model,
        "incidents_tracked": len(_incidents),
    }


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _map_prometheus_severity(prom_severity: str) -> str:
    return {"critical": "P1", "warning": "P2", "info": "P3"}.get(prom_severity.lower(), "P2")


# ─────────────────────────────────────────────────────────────
# Entrypoint
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "sre_agent.main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8080")),
        reload=False,
        workers=1,  # Keep at 1 — model state is not thread-safe across workers
    )
