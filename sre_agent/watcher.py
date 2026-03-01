"""
Kubernetes event watcher — auto-triggers SRE investigations on Warning events.

Runs as a background thread. Streams cluster-wide events via the Kubernetes
watch API and enqueues investigations for actionable Warning events.

Deduplication: the same service/namespace combo is only investigated once per
WATCH_COOLDOWN_SECONDS (default 900 = 15 min) to prevent alert storms.
"""
import os
import re
import time
import logging
import threading
from typing import Callable

from kubernetes import client as k8s_client, watch as k8s_watch

from .config import config

logger = logging.getLogger("sre_agent.watcher")

# ─────────────────────────────────────────────────────────────
# Tuning
# ─────────────────────────────────────────────────────────────

# Warning event reasons that are worth investigating → default severity
TRIGGER_REASONS: dict[str, str] = {
    "Unhealthy":         "P2",   # liveness/readiness/startup probe failed
    "BackOff":           "P2",   # CrashLoopBackOff
    "OOMKilling":        "P1",   # kernel OOM killer fired
    "Failed":            "P2",   # generic failure (FailedCreate, etc.)
    "FailedMount":       "P2",   # volume mount failure
    "FailedScheduling":  "P3",   # pod can't be scheduled
    "Evicted":           "P2",   # pod evicted (disk/memory pressure)
    "NodeNotReady":      "P1",   # node flipped NotReady
    "KillContainer":     "P2",   # container killed
    "NetworkNotReady":   "P1",   # CNI failure
}

# Namespaces where Warning events are normal noise — skip them
_IGNORE_NS: set[str] = {"kube-system", "kube-public", "kube-node-lease", "sre-agent"}

# Strip replicaset-hash + pod-hash suffix from pod names to get the workload name
# e.g. rook-ceph-osd-1-864fd9bc5c-74tpg → rook-ceph-osd-1
_RS_HASH_RE = re.compile(r"-[a-z0-9]{9,10}-[a-z0-9]{5}$")
# e.g. rook-ceph-osd-1-74tpg (DaemonSet pods have only one hash)
_POD_HASH_RE = re.compile(r"-[a-z0-9]{5}$")

# How long (seconds) before re-investigating the same service after a trigger
COOLDOWN = int(os.getenv("WATCH_COOLDOWN_SECONDS", "900"))


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _extract_workload(obj_name: str, obj_kind: str) -> str:
    """Strip pod hash suffixes so we get the owning workload name."""
    if obj_kind == "Pod":
        stripped = _RS_HASH_RE.sub("", obj_name)
        if stripped == obj_name:
            stripped = _POD_HASH_RE.sub("", obj_name)
        return stripped
    return obj_name


# ─────────────────────────────────────────────────────────────
# Watcher loop
# ─────────────────────────────────────────────────────────────

def run_event_watcher(enqueue_fn: Callable, stop_event: threading.Event):
    """
    Blocking loop — intended to run in a background thread.

    Args:
        enqueue_fn: callable(service, namespace, error_type, severity, alert_source)
                    that queues a new investigation.
        stop_event: threading.Event — set it to stop the loop gracefully.
    """
    from kubernetes import config as k8s_config
    if config.kubernetes.in_cluster:
        k8s_config.load_incluster_config()
    else:
        k8s_config.load_kube_config(
            config_file=config.kubernetes.kubeconfig,
            context=config.kubernetes.context,
        )

    v1 = k8s_client.CoreV1Api()
    w = k8s_watch.Watch()

    # Cooldown tracker: "{ns}/{service}" → epoch seconds of last trigger
    last_triggered: dict[str, float] = {}

    logger.info(
        f"Event watcher started — monitoring Warning events across all namespaces "
        f"(cooldown={COOLDOWN}s, reasons={list(TRIGGER_REASONS)})"
    )

    while not stop_event.is_set():
        try:
            for raw in w.stream(
                v1.list_event_for_all_namespaces,
                field_selector="type=Warning",
                timeout_seconds=300,   # reconnect every 5 min to stay fresh
            ):
                if stop_event.is_set():
                    break

                evt = raw.get("object")
                if evt is None:
                    continue

                reason = evt.reason or ""
                if reason not in TRIGGER_REASONS:
                    continue

                involved = evt.involved_object
                ns = (involved.namespace or "default").strip()
                obj_name = (involved.name or "unknown").strip()
                obj_kind = (involved.kind or "").strip()

                if ns in _IGNORE_NS:
                    continue

                service = _extract_workload(obj_name, obj_kind)
                key = f"{ns}/{service}"
                now = time.time()

                if now - last_triggered.get(key, 0) < COOLDOWN:
                    logger.debug(f"[watcher] Skipping {key} (cooldown active)")
                    continue

                last_triggered[key] = now
                severity = TRIGGER_REASONS[reason]
                msg = (evt.message or "")[:200]
                error_type = f"{reason}: {msg}" if msg else reason

                logger.info(
                    f"[watcher] Auto-trigger: service={service} ns={ns} "
                    f"reason={reason} severity={severity}"
                )
                try:
                    enqueue_fn(
                        service=service,
                        namespace=ns,
                        error_type=error_type,
                        severity=severity,
                        alert_source="k8s-event-watcher",
                    )
                except Exception as exc:
                    logger.error(f"[watcher] Failed to enqueue incident: {exc}")

        except Exception as exc:
            if stop_event.is_set():
                break
            logger.warning(f"[watcher] Stream error, reconnecting in 15s: {exc}")
            time.sleep(15)

    logger.info("Event watcher stopped.")
