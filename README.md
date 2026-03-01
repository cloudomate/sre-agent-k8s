# SRE Agent — Active Incident Investigator for HCI Platforms

An LLM-powered SRE agent that investigates service failures, collects evidence using real diagnostic tools, and produces structured incident reports. Runs fully local on **4–8 GB CPU RAM**.

---

## Architecture

```
HCI Portal / Alertmanager / Harvester Webhook
          │
          ▼
   FastAPI Server  (port 8080)
          │
          ▼
   SRE Agent (ReAct loop)
   ┌───────────────────────────────┐
   │  Think → Tool Call → Observe  │
   │  (up to 12 steps)             │
   │                               │
   │  Ollama: Qwen 2.5 7B Q4      │
   │  ~4.5 GB RAM, CPU-only        │
   └───────────────────────────────┘
          │
          ▼
   Structured Incident Report
   → Slack / Webhook / File / stdout
```

---

## Model

| Model | RAM | Quality |
|---|---|---|
| `qwen2.5:7b-instruct-q4_K_M` (**default**) | ~4.5 GB | Best tool-calling in class |
| `phi4-mini:q4` | ~2.5 GB | Lower RAM, acceptable quality |
| `mistral:7b-q4` | ~4.5 GB | Alternative |

---

## Quick Start

### 1. Clone and configure

```bash
cp .env.example .env
# Edit .env with your Prometheus URL, Slack webhook, etc.
```

### 2. Start with Docker Compose

```bash
docker compose up -d
```

This starts Ollama (pulls the model automatically) and the SRE agent API.

### 3. Trigger a manual investigation

```bash
curl -X POST http://localhost:8080/incidents \
  -H "Content-Type: application/json" \
  -d '{
    "service": "vm-api",
    "namespace": "production",
    "error_type": "CrashLoopBackOff",
    "alert_source": "manual",
    "severity": "P2"
  }'
```

### 4. Poll for results

```bash
curl http://localhost:8080/incidents/inc-20260228-143022-abc123
```

---

## Integration with Your HCI Portal

### Option A: Direct API call when a service goes unhealthy

In your portal's health check logic:

```python
import httpx

async def trigger_investigation(service: str, namespace: str, error: str):
    async with httpx.AsyncClient() as client:
        resp = await client.post("http://sre-agent:8080/incidents", json={
            "service": service,
            "namespace": namespace,
            "error_type": error,
            "alert_source": "portal-healthcheck",
            "severity": "P2",
        })
    return resp.json()["incident_id"]
```

### Option B: Alertmanager webhook

In `alertmanager.yml`:

```yaml
receivers:
  - name: sre-agent
    webhook_configs:
      - url: http://sre-agent:8080/incidents/webhook/alertmanager
        send_resolved: false

route:
  receiver: sre-agent
  group_wait: 30s
  group_interval: 5m
```

### Option C: Harvester portal webhook

Set your Harvester notification URL to:
```
http://sre-agent:8080/incidents/webhook/harvester
```

---

## Report Output

```json
{
  "incident_id": "inc-20260228-143022-abc123",
  "severity": "P2",
  "title": "vm-api OOMKilled due to memory limit",
  "affected_service": "vm-api",
  "affected_namespace": "production",
  "investigation_summary": "The vm-api pod was repeatedly killed by the OOM killer. Memory usage peaked at 820MB against a 512MB limit during a traffic spike at 14:28 UTC.",
  "root_cause": "Memory limit of 512MB is insufficient. Pod consumed 820MB before OOMKill.",
  "evidence": [
    "kubectl describe pod shows OOMKilled in Last State",
    "Exit code 137 confirms OOM kill",
    "kubectl top shows memory at 98% of limit before kill",
    "Prometheus: request rate spike 3x at 14:25 UTC"
  ],
  "timeline": [
    "14:25 UTC: Request rate spike 3x normal",
    "14:28 UTC: Memory hits 512MB limit",
    "14:28 UTC: OOMKilled by kernel"
  ],
  "recommended_actions": [
    {
      "action": "Increase memory limit to 1.5GB in deployment spec",
      "priority": "immediate",
      "safe_to_automate": false
    },
    {
      "action": "Investigate memory growth pattern — possible leak in v2.3.1",
      "priority": "short-term",
      "safe_to_automate": false
    }
  ],
  "runbook_refs": ["RB-001"],
  "requires_escalation": false,
  "confidence": "high",
  "investigation_duration_s": 34.2,
  "tool_calls_made": 5
}
```

---

## Adding Your Own Runbooks

Drop a JSON file in `./runbooks/` (see `example_runbook.json` for schema).
The agent will automatically embed and search them.

```json
[
  {
    "id": "RB-100",
    "title": "My Service - DB Connection Failure",
    "tags": ["database", "connection", "pool"],
    "summary": "Service cannot connect to database.",
    "steps": ["Check DB pod health", "Verify connection string", "..."],
    "causes": ["DB crashed", "Wrong credentials", "Network partition"]
  }
]
```

---

## API Reference

| Endpoint | Method | Description |
|---|---|---|
| `POST /incidents` | POST | Trigger investigation |
| `GET /incidents/{id}` | GET | Get incident status + report |
| `GET /incidents` | GET | List recent incidents |
| `POST /incidents/webhook/alertmanager` | POST | Alertmanager receiver |
| `POST /incidents/webhook/harvester` | POST | Harvester portal webhook |
| `GET /runbooks?query=oom` | GET | Browse/search runbooks |
| `GET /health` | GET | Health check |

---

## Hardware Requirements

| Component | Min RAM | Recommended |
|---|---|---|
| Ollama (Qwen 2.5 7B Q4) | 4.5 GB | 6 GB |
| SRE Agent API | 256 MB | 512 MB |
| Total | **~5 GB** | **~7 GB** |

Works fine on a 8GB node. For 4GB nodes, switch to `phi4-mini:q4` (~2.5GB).
