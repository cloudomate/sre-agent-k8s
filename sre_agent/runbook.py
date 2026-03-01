"""
Runbook RAG (Retrieval-Augmented Generation)
Stores and searches runbooks using vector embeddings via any OpenAI-compatible endpoint.
Falls back to keyword search if the embed model is unavailable.
"""
import json
import logging
import math
import os
from pathlib import Path
from typing import Optional
from openai import OpenAI

from .config import config

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Built-in runbooks (common HCI/K8s patterns)
# ─────────────────────────────────────────────────────────────

BUILTIN_RUNBOOKS = [
    {
        "id": "RB-001",
        "title": "OOMKilled Pod",
        "tags": ["oom", "memory", "killed", "crash"],
        "summary": "Pod was terminated by kernel OOM killer due to exceeding memory limit.",
        "steps": [
            "Check kubectl describe pod for 'OOMKilled' in Last State",
            "Review memory usage with kubectl top pod",
            "Increase memory limit in deployment spec or find memory leak",
            "Check application for unbounded caches or memory leaks",
        ],
        "causes": ["Memory limit too low", "Memory leak in application", "Sudden traffic spike"],
    },
    {
        "id": "RB-002",
        "title": "CrashLoopBackOff",
        "tags": ["crashloop", "crash", "restart", "backoff"],
        "summary": "Pod is repeatedly crashing and Kubernetes is backing off restarts.",
        "steps": [
            "kubectl logs <pod> --previous to see last crash logs",
            "Check exit code in kubectl describe pod",
            "Verify liveness probe configuration",
            "Check for config/secret mount errors",
            "Verify image is correct and not corrupted",
        ],
        "causes": ["Application startup failure", "Missing config/secret", "Bad liveness probe", "Port conflict"],
    },
    {
        "id": "RB-003",
        "title": "ImagePullBackOff",
        "tags": ["image", "pull", "registry", "docker", "imagepull"],
        "summary": "Kubernetes cannot pull the container image from the registry.",
        "steps": [
            "Verify image name and tag in deployment spec",
            "Check registry credentials (imagePullSecret)",
            "Verify network connectivity to registry",
            "Check if image exists in registry",
            "Inspect docker config secret for expiry",
        ],
        "causes": ["Wrong image tag", "Expired credentials", "Registry unreachable", "Image deleted"],
    },
    {
        "id": "RB-004",
        "title": "Pod Pending / FailedScheduling",
        "tags": ["pending", "scheduling", "node", "resource", "taint", "affinity"],
        "summary": "Pod cannot be scheduled onto any node.",
        "steps": [
            "kubectl describe pod to find scheduling failure reason",
            "Check node capacity with kubectl top nodes",
            "Verify node selectors and affinity rules",
            "Check for taints on nodes that block scheduling",
            "Check PVC/PV binding if pod uses persistent storage",
        ],
        "causes": ["Insufficient CPU/memory on nodes", "Node taints", "PVC not bound", "Affinity mismatch"],
    },
    {
        "id": "RB-005",
        "title": "Node Not Ready",
        "tags": ["node", "notready", "kubelet", "pressure", "disk", "memory"],
        "summary": "A Kubernetes node is in NotReady state.",
        "steps": [
            "kubectl describe node to check conditions",
            "Check MemoryPressure, DiskPressure, PIDPressure conditions",
            "SSH to node and check kubelet status: systemctl status kubelet",
            "Check disk usage: df -h",
            "Review kubelet logs: journalctl -u kubelet --since '30m ago'",
        ],
        "causes": ["Disk pressure", "Memory pressure", "Kubelet crash", "Network issue to control plane"],
    },
    {
        "id": "RB-006",
        "title": "High Error Rate (5xx)",
        "tags": ["5xx", "error", "http", "rate", "service", "latency"],
        "summary": "Service is returning high rate of HTTP 5xx errors.",
        "steps": [
            "Check service logs for error patterns",
            "Verify downstream dependencies are healthy",
            "Check database connection pool exhaustion",
            "Review recent deployments for regressions",
            "Check resource limits (CPU throttling causing timeouts)",
        ],
        "causes": ["Database connection issues", "CPU throttling", "Bad deployment", "Dependency failure"],
    },
    {
        "id": "RB-007",
        "title": "Persistent Volume Claim Pending",
        "tags": ["pvc", "storage", "volume", "persistent", "bound"],
        "summary": "PVC is stuck in Pending state, cannot provision storage.",
        "steps": [
            "kubectl describe pvc to see provisioning events",
            "Verify StorageClass exists and is correct",
            "Check if storage backend (Longhorn, Rook-Ceph) is healthy",
            "Verify sufficient storage capacity on nodes",
        ],
        "causes": ["StorageClass missing", "Storage backend unhealthy", "Insufficient capacity", "Wrong access mode"],
    },
    {
        "id": "RB-008",
        "title": "HCI VM Not Starting",
        "tags": ["vm", "virtual machine", "kubevirt", "harvester", "hci", "vmi"],
        "summary": "Harvester/KubeVirt virtual machine instance is not starting.",
        "steps": [
            "kubectl get vmi -n <namespace> to check VMI status",
            "kubectl describe vmi <name> for events",
            "Check virt-launcher pod logs",
            "Verify node has sufficient CPU/memory for VM",
            "Check if VM image/data volume is ready",
            "Inspect network config and ensure bridge exists on node",
        ],
        "causes": ["Insufficient node resources", "Data volume not ready", "Network config error", "KubeVirt controller issue"],
    },
    {
        "id": "RB-009",
        "title": "etcd Latency / Control Plane Degraded",
        "tags": ["etcd", "control plane", "apiserver", "latency", "leader"],
        "summary": "Kubernetes control plane is slow or degraded, likely etcd issues.",
        "steps": [
            "Check etcd pod health: kubectl get pods -n kube-system",
            "Review etcd logs for election/leader changes",
            "Check etcd disk latency (etcd is IOPS-sensitive)",
            "Verify etcd cluster has quorum (odd number of members)",
            "Monitor apiserver latency metrics",
        ],
        "causes": ["Disk I/O bottleneck", "etcd quorum lost", "Network partition", "etcd data corruption"],
    },
    {
        "id": "RB-010",
        "title": "Certificate Expiry",
        "tags": ["certificate", "tls", "ssl", "expired", "x509", "cert"],
        "summary": "TLS certificate has expired or is about to expire.",
        "steps": [
            "kubectl get certificates -A to list cert-manager certs",
            "Check cert expiry: openssl s_client -connect host:port | openssl x509 -noout -dates",
            "Trigger cert renewal via cert-manager or manually",
            "Verify cert-manager is running and ACME/issuer is reachable",
        ],
        "causes": ["cert-manager failure", "ACME challenge failure", "Manual cert not renewed"],
    },
]


# ─────────────────────────────────────────────────────────────
# Embedding helpers
# ─────────────────────────────────────────────────────────────

def _get_embedding(text: str) -> Optional[list]:
    """Get embedding vector via OpenAI-compatible embeddings endpoint."""
    try:
        client = OpenAI(base_url=config.llm.base_url, api_key=config.llm.api_key)
        resp = client.embeddings.create(model=config.llm.embed_model, input=text)
        return resp.data[0].embedding
    except Exception as e:
        logger.debug(f"Embedding failed: {e}")
        return None


def _cosine_similarity(a: list, b: list) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


def _keyword_score(query: str, runbook: dict) -> float:
    """Simple keyword overlap score as fallback."""
    query_words = set(query.lower().split())
    symptoms = runbook.get("symptoms") or []
    rb_text = " ".join([
        runbook.get("title", ""),
        runbook.get("summary", ""),
        " ".join(symptoms if isinstance(symptoms, list) else [symptoms]),
        " ".join(runbook.get("tags", [])),
        " ".join(runbook.get("causes", [])),
    ]).lower()
    rb_words = set(rb_text.split())
    overlap = query_words & rb_words
    return len(overlap) / max(len(query_words), 1)


# ─────────────────────────────────────────────────────────────
# RunbookSearcher
# ─────────────────────────────────────────────────────────────

class RunbookSearcher:
    def __init__(self):
        self.runbooks = list(BUILTIN_RUNBOOKS)
        self._embeddings: dict[str, list] = {}  # id -> vector
        self._embed_available = False

        # Load any user-provided runbooks from disk
        self._load_from_disk()

        # Try to build embeddings
        self._build_embeddings()

    def _load_from_disk(self):
        """Load additional runbooks from RUNBOOK_DIR."""
        runbook_dir = Path(config.report.runbook_dir)
        if not runbook_dir.exists():
            return
        for f in runbook_dir.glob("*.json"):
            try:
                with open(f) as fh:
                    rb = json.load(fh)
                if isinstance(rb, list):
                    self.runbooks.extend(rb)
                elif isinstance(rb, dict):
                    self.runbooks.append(rb)
                logger.info(f"Loaded runbook file: {f.name}")
            except Exception as e:
                logger.warning(f"Could not load runbook {f}: {e}")

    def _build_embeddings(self):
        """Build vector embeddings for all runbooks."""
        test_vec = _get_embedding("test")
        if test_vec is None:
            logger.info("Embed model not available, using keyword search for runbooks.")
            return

        self._embed_available = True
        for rb in self.runbooks:
            symptoms = rb.get("symptoms") or []
            text = f"{rb['title']} {rb.get('summary', '')} {' '.join(symptoms if isinstance(symptoms, list) else [])} {' '.join(rb.get('tags', []))}"
            vec = _get_embedding(text)
            if vec:
                self._embeddings[rb["id"]] = vec
        logger.info(f"Built embeddings for {len(self._embeddings)} runbooks.")

    def search(self, query: str, top_k: int = 3) -> list:
        """Find the most relevant runbooks for a given symptom/query."""
        if self._embed_available and self._embeddings:
            query_vec = _get_embedding(query)
            if query_vec:
                scored = []
                for rb in self.runbooks:
                    rb_vec = self._embeddings.get(rb["id"])
                    if rb_vec:
                        score = _cosine_similarity(query_vec, rb_vec)
                        scored.append((score, rb))
                scored.sort(key=lambda x: x[0], reverse=True)
                results = [rb for score, rb in scored[:top_k] if score > 0.4]
                if results:
                    return results

        # Fallback: keyword search
        scored = [(_keyword_score(query, rb), rb) for rb in self.runbooks]
        scored.sort(key=lambda x: x[0], reverse=True)
        return [rb for score, rb in scored[:top_k] if score > 0.1]

    def get_by_id(self, rb_id: str) -> Optional[dict]:
        for rb in self.runbooks:
            if rb["id"] == rb_id:
                return rb
        return None

    def add_runbook(self, runbook: dict):
        """Add a new runbook at runtime."""
        self.runbooks.append(runbook)
        if self._embed_available:
            symptoms = runbook.get("symptoms") or []
            text = f"{runbook['title']} {runbook.get('summary', '')} {' '.join(symptoms if isinstance(symptoms, list) else [])} {' '.join(runbook.get('tags', []))}"
            vec = _get_embedding(text)
            if vec:
                self._embeddings[runbook["id"]] = vec
