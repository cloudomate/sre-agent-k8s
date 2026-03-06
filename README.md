# SRE Agent — Kubernetes-native Incident Investigator

An LLM-powered SRE agent that automatically investigates service failures, collects evidence via the Kubernetes API and Prometheus, and produces structured incident reports with severity classification and category routing.

---

## Architecture

```
K8s Warning Events (watcher.py) ──┐
Webhook / API (main.py)           ├──► SRE Agent ReAct loop (agent.py)
Alertmanager webhook              ┘         │
                                     Tool calls (tools.py)
                                     Kubernetes Python client (in-cluster)
                                     Prometheus HTTP queries
                                     Loki / socket / HTTP checks
                                            │
                                     Runbook RAG lookup (runbook.py)
                                            │
                                     Report generation + delivery (reporter.py)
                                     stdout │ file │ slack │ teams │ webhook │ email │ resend │ sms │ whatsapp
```

### Classification

- **Severity**: P1 (critical), P2 (warning), P3 (info) — explicit criteria in the LLM prompt with validation and fallback
- **Category**: `infra`, `app`, `storage`, `network` — keyword-based inference when LLM output is invalid
- **Event grouping**: 30-second window buffers Warning events by namespace. If >=3 services fire in the same namespace, they dispatch as a single grouped investigation (cascading failure detection). P1 events flush immediately.

---

## LLM Configuration

The agent works with **any OpenAI-compatible endpoint**. Two modes:

### Option A: External API (recommended for production)

No local model needed — set `llamacpp.enabled=false` in Helm to skip the in-cluster LLM server.

| Endpoint | LLM_BASE_URL | LLM_MODEL |
|---|---|---|
| OpenAI | `https://api.openai.com/v1` | `gpt-4o` |
| Any OpenAI-compatible API | `https://your-api.example.com/v1` | `your-model-id` |
| vLLM | `http://vllm:8000/v1` | `model-name` |

### Option B: In-cluster llama.cpp (air-gapped / local)

Deploy with `llamacpp.enabled=true` (default). The llama.cpp server runs as a sidecar deployment with the model baked into the image.

| Endpoint | LLM_BASE_URL | LLM_MODEL |
|---|---|---|
| llama.cpp (in-cluster) | `http://llamacpp.sre-agent.svc.cluster.local:11434/v1` | `qwen3.5:4b` |
| llama.cpp (local dev) | `http://localhost:11434/v1` | `qwen3.5:4b` |

Current local model: Qwen3.5-4B Q4_0 (llama.cpp b8196). Requires ~4 GB RAM + 4 CPU cores.

---

## Quick Start

### Kubernetes — Helm (external LLM)

```bash
helm install sre-agent oci://cr.imys.in/hci/sre-agent-k8s/sre-agent \
  --version 0.3.0 -n sre-agent --create-namespace \
  --set llamacpp.enabled=false \
  --set llm.baseUrl=https://your-api.example.com/v1 \
  --set llm.apiKey=sk-your-key \
  --set llm.model=your-model-id \
  --set report.channels="stdout\,file\,email" \
  --set smtp.host=mail.example.com --set smtp.port=587

# Upgrade
helm upgrade sre-agent oci://cr.imys.in/hci/sre-agent-k8s/sre-agent \
  --version 0.3.0 -n sre-agent --reuse-values
```

### Kubernetes — Helm (local llama.cpp)

```bash
helm install sre-agent oci://cr.imys.in/hci/sre-agent-k8s/sre-agent \
  --version 0.3.0 -n sre-agent --create-namespace \
  --set llm.apiKey=llamacpp \
  --set report.channels="stdout\,file\,email" \
  --set smtp.host=192.168.4.102 --set smtp.port=1025 --set smtp.tls=false
```

### Kubernetes — Kustomize

```bash
# 1. Edit deploy/kustomize/configmap.yaml — set LLM_BASE_URL, LLM_MODEL, PROMETHEUS_URL
# 2. Edit deploy/kustomize/secret.yaml    — set LLM_API_KEY (base64), SLACK_WEBHOOK_URL, etc.
kubectl apply -k deploy/kustomize/
```

### Local development

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env: set LLM_BASE_URL, LLM_API_KEY, LLM_MODEL, K8S_IN_CLUSTER=false
python -m uvicorn sre_agent.main:app --host 0.0.0.0 --port 8080 --workers 1
```

### Docker Compose (local llama.cpp + agent)

```bash
docker compose up -d
```

---

## Trigger an Investigation

```bash
curl -X POST http://sre-agent:8080/incidents \
  -H "Content-Type: application/json" \
  -d '{
    "service": "my-service",
    "namespace": "default",
    "error_type": "CrashLoopBackOff",
    "severity": "P2"
  }'

# Poll for results
curl http://sre-agent:8080/incidents/{incident_id}

# List incidents (with optional filtering)
curl "http://sre-agent:8080/incidents?severity=P1&category=storage"
```

---

## Report Output

Reports include severity, category, evidence, timeline, and recommended actions:

```json
{
  "incident_id": "inc-20260304-145321-0e9d74",
  "severity": "P2",
  "category": "app",
  "title": "test-app CrashLoopBackOff in default namespace",
  "affected_service": "test-app",
  "affected_namespace": "default",
  "investigation_summary": "The incident reports a CrashLoopBackOff for test-app...",
  "root_cause": "unknown",
  "evidence": [
    "get_pod_status with label app=test-app returned empty list",
    "get_deployment_status for test-app returned Not Found error"
  ],
  "timeline": ["2026-03-04T14:53:21Z: Incident created"],
  "recommended_actions": [
    {"action": "Verify deployment manifest exists", "priority": "immediate", "safe_to_automate": false}
  ],
  "runbook_refs": ["RB-002", "RB-K8S-005"],
  "requires_escalation": true,
  "escalation_reason": "Unable to locate pods or deployment",
  "confidence": "low",
  "investigation_duration_s": 24.8,
  "tool_calls_made": 12
}
```

Reports are delivered to all configured channels: `stdout`, `file` (JSON + Markdown), `slack` (Block Kit), `teams` (Adaptive Cards), `webhook`, `email` (SMTP), `resend` (Resend API), `sms` (Twilio), `whatsapp` (Twilio).

Email subjects include severity and category: `[SRE Alert] [P2] [APP] test-app CrashLoopBackOff — test-app`

---

## Report Channels Configuration

Set `REPORT_CHANNELS` (comma-separated) to enable delivery targets. Each channel has its own configuration:

### Slack

| Variable | Location | Description |
|---|---|---|
| `SLACK_WEBHOOK_URL` | Secret | Slack incoming webhook URL |

### Microsoft Teams

Reports are formatted as [Adaptive Cards](https://adaptivecards.io/) (v1.4, full-width).

| Variable | Location | Description |
|---|---|---|
| `TEAMS_WEBHOOK_URL` | Secret | Teams webhook URL (the URL itself contains the auth token) |

**Setup via Power Automate (recommended):**

1. Open the Teams channel → click `+` (Add a tab) or `...` → **Workflows**
2. Select **"Post to a channel when a webhook request is received"**
3. Choose the target Team and Channel, then confirm
4. Copy the generated webhook URL (looks like `https://...powerplatform.com/.../invoke?api-version=1&...`)

**Setup via Incoming Webhook connector (legacy):**

1. Open the Teams channel → `...` → **Connectors** → **Incoming Webhook** → **Configure**
2. Name it (e.g. "SRE Agent"), click **Create**, copy the URL

```bash
# Helm
--set report.channels="stdout\,file\,teams" \
--set teams.webhookUrl="https://...powerplatform.com/.../invoke?api-version=1&..."
```

### Email (SMTP)

| Variable | Location | Description |
|---|---|---|
| `SMTP_HOST` | ConfigMap | SMTP server hostname |
| `SMTP_PORT` | ConfigMap | SMTP port (default: 587) |
| `SMTP_TLS` | ConfigMap | Enable STARTTLS (default: true, set false for local relays like MailHog) |
| `SMTP_USER` | ConfigMap | SMTP username (optional) |
| `SMTP_PASSWORD` | Secret | SMTP password (optional) |
| `EMAIL_FROM` | ConfigMap | Sender address |
| `EMAIL_TO` | ConfigMap | Comma-separated recipient addresses |

### Email (Resend)

[Resend](https://resend.com) is a developer email API — no SMTP server needed.

| Variable | Location | Description |
|---|---|---|
| `RESEND_API_KEY` | Secret | Resend API key (`re_...`) |
| `RESEND_FROM` | ConfigMap | Verified sender address (e.g. `SRE Agent <alerts@yourdomain.com>`) |
| `EMAIL_TO` | ConfigMap | Comma-separated recipient addresses (shared with SMTP email) |

```bash
# Helm
--set report.channels="stdout\,file\,resend" \
--set resend.apiKey="re_xxxx" \
--set resend.from="SRE Agent <alerts@yourdomain.com>" \
--set smtp.to="oncall@example.com"
```

### SMS / WhatsApp (Twilio)

| Variable | Location | Description |
|---|---|---|
| `TWILIO_ACCOUNT_SID` | Secret | Twilio account SID |
| `TWILIO_AUTH_TOKEN` | Secret | Twilio auth token |
| `TWILIO_FROM` | ConfigMap | Sender phone number (E.164) |
| `SMS_TO` | ConfigMap | Comma-separated recipient phone numbers |
| `WHATSAPP_TO` | ConfigMap | Comma-separated recipient phone numbers (`whatsapp:` prefix added automatically) |

### Generic Webhook

| Variable | Location | Description |
|---|---|---|
| `REPORT_WEBHOOK_URL` | Secret | URL to POST raw JSON report to |

---

## Event Watcher

The agent watches Kubernetes Warning events cluster-wide and auto-triggers investigations:

- **Trigger reasons**: Unhealthy, BackOff, OOMKilling, Failed, FailedMount, FailedScheduling, Evicted, NodeNotReady, KillContainer, NetworkNotReady
- **Ignored namespaces**: `kube-system`, `kube-public`, `kube-node-lease`, `sre-agent`
- **Cooldown**: 15 min per service/namespace (configurable via `WATCH_COOLDOWN_SECONDS`)
- **Grouping**: 30s window per namespace. >=3 services = single grouped investigation. P1 events flush immediately.
- **Env vars**: `WATCH_EVENTS=true`, `WATCH_COOLDOWN_SECONDS=900`, `WATCH_GROUP_WINDOW=30`

---

## Integration

### Alertmanager webhook

```yaml
receivers:
  - name: sre-agent
    webhook_configs:
      - url: http://sre-agent:8080/incidents/webhook/alertmanager
        send_resolved: false
```

### Direct API call

```python
import httpx

async def trigger_investigation(service: str, namespace: str, error: str):
    async with httpx.AsyncClient() as client:
        resp = await client.post("http://sre-agent:8080/incidents", json={
            "service": service,
            "namespace": namespace,
            "error_type": error,
            "severity": "P2",
        })
    return resp.json()["incident_id"]
```

---

## API Reference

| Endpoint | Method | Description |
|---|---|---|
| `POST /incidents` | POST | Trigger investigation |
| `GET /incidents/{id}` | GET | Get incident status + report |
| `GET /incidents` | GET | List incidents (optional `?severity=P1&category=storage`) |
| `POST /incidents/webhook/alertmanager` | POST | Alertmanager receiver |
| `POST /incidents/webhook/harvester` | POST | Harvester portal webhook |
| `GET /runbooks?query=oom` | GET | Browse/search runbooks |
| `GET /health` | GET | Health check |

---

## Runbooks (57 total)

| File | Count | Coverage |
|---|---|---|
| `kubernetes-core-runbooks.json` | 8 | etcd, apiserver, node NotReady, certs, OOMKill, Flatcar, PVC, CoreDNS |
| `ismp-runbooks.json` | 9 | ISMP platform runbooks |
| `uphci-runbooks.json` | 8 | upHCI platform runbooks |
| `rook-ceph-runbooks.json` | 6 | Ceph cluster health, OSD, MON, pool full, CSI |
| `kube-prometheus-runbooks.json` | 5 | Prometheus storage, scrape failures, Alertmanager |
| `kubevirt-runbooks.json` | 4 | VM not starting, virt-launcher, live migration |
| `kube-ovn-runbooks.json` | 4 | CNI failure, subnet/IP allocation, OVN controller |
| `metallb-runbooks.json` | 3 | LoadBalancer IP pending, speaker crash, IP conflict |
| Built-in (runbook.py) | 10 | OOMKill, CrashLoop, ImagePull, Pending, NodeNotReady, 5xx, PVC, HCI VM, etcd, cert expiry |

Add custom runbooks by dropping JSON files in `./runbooks/` (see existing files for schema).

---

## Building Images

```bash
# SRE Agent (multi-arch) — this is all you need when using an external LLM API
docker buildx build --platform linux/amd64,linux/arm64 \
  -t cr.imys.in/hci/sre-agent:latest --push .

# Helm chart (OCI registry)
helm package deploy/helm/sre-agent/
helm push sre-agent-0.3.0.tgz oci://cr.imys.in/hci/sre-agent-k8s
```

The following are only needed if you want to run the in-cluster llama.cpp LLM server (`llamacpp.enabled=true`):

```bash
# Base llama.cpp server (multi-arch, llama.cpp b8196)
docker buildx build --platform linux/amd64,linux/arm64 \
  -f images/llama-server.Dockerfile \
  -t cr.imys.in/hci/llama-server:latest --push .

# Model image (GGUF must be in build context directory)
# Build on a machine where the .gguf file is available:
cd /path/to/models
docker build -f /path/to/images/llama-qwen3.Dockerfile \
  --build-arg MODEL_FILE=Qwen3.5-4B-Q4_0.gguf \
  -t cr.imys.in/hci/llama-qwen3.5-4b:latest .
docker push cr.imys.in/hci/llama-qwen3.5-4b:latest
```
