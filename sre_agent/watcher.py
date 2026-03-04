"""
Kubernetes event watcher — auto-triggers SRE investigations on Warning events.

Runs as a background thread. Streams cluster-wide events via the Kubernetes
watch API and enqueues investigations for actionable Warning events.

Deduplication: the same service/namespace combo is only investigated once per
WATCH_COOLDOWN_SECONDS (default 900 = 15 min) to prevent alert storms.

Grouping: events in the same namespace are buffered for WATCH_GROUP_WINDOW
seconds (default 30). If >=3 services fire in that window, they are dispatched
as a single grouped investigation (likely cascading failure). P1 events flush
the group immediately.
"""
import collections
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

# P1 reasons bypass grouping window — dispatch immediately
_P1_REASONS = {r for r, s in TRIGGER_REASONS.items() if s == "P1"}

# Namespaces where Warning events are normal noise — skip them
_IGNORE_NS: set[str] = {"kube-system", "kube-public", "kube-node-lease", "sre-agent"}

# Strip replicaset-hash + pod-hash suffix from pod names to get the workload name
# e.g. rook-ceph-osd-1-864fd9bc5c-74tpg → rook-ceph-osd-1
_RS_HASH_RE = re.compile(r"-[a-z0-9]{9,10}-[a-z0-9]{5}$")
# e.g. rook-ceph-osd-1-74tpg (DaemonSet pods have only one hash)
_POD_HASH_RE = re.compile(r"-[a-z0-9]{5}$")

# How long (seconds) before re-investigating the same service after a trigger
COOLDOWN = int(os.getenv("WATCH_COOLDOWN_SECONDS", "900"))

# Grouping window — collect events for this many seconds before dispatching
GROUP_WINDOW = int(os.getenv("WATCH_GROUP_WINDOW", "30"))

# Severity ranking for picking worst severity in a group
_SEV_RANK = {"P1": 0, "P2": 1, "P3": 2}

# Threshold: if this many or more distinct services fire in the same namespace
# within the grouping window, treat it as a cascading failure
_GROUP_THRESHOLD = 3


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


def _worst_severity(events: list[dict]) -> str:
    """Return the worst (lowest rank number) severity from a list of events."""
    return min((e["severity"] for e in events), key=lambda s: _SEV_RANK.get(s, 9))


def _dispatch_group(
    group: list[dict],
    enqueue_fn: Callable,
    last_triggered: dict[str, float],
    now: float,
):
    """
    Dispatch a buffered group of events in one namespace.
    If >=_GROUP_THRESHOLD services, dispatch as a single grouped investigation.
    Otherwise dispatch individually per service.
    """
    if not group:
        return

    ns = group[0]["namespace"]

    # Deduplicate by service
    by_service: dict[str, list] = collections.defaultdict(list)
    for e in group:
        by_service[e["service"]].append(e)

    if len(by_service) >= _GROUP_THRESHOLD:
        # Cascading failure — single grouped investigation
        combined_error = "; ".join(
            f"{svc}: {evts[0]['error_type']}" for svc, evts in list(by_service.items())[:5]
        )
        key = f"{ns}/_grouped"
        if now - last_triggered.get(key, 0) >= COOLDOWN:
            last_triggered[key] = now
            sev = _worst_severity(group)
            logger.info(
                f"[watcher] Grouped {len(group)} events across {len(by_service)} services "
                f"in {ns} -> severity={sev}"
            )
            try:
                enqueue_fn(
                    service=f"multiple ({len(by_service)} services)",
                    namespace=ns,
                    error_type=combined_error,
                    severity=sev,
                    alert_source="k8s-event-watcher-grouped",
                )
            except Exception as exc:
                logger.error(f"[watcher] Failed to enqueue grouped incident: {exc}")
    else:
        # Few services — dispatch individually (original behavior)
        for service, evts in by_service.items():
            key = f"{ns}/{service}"
            if now - last_triggered.get(key, 0) >= COOLDOWN:
                last_triggered[key] = now
                worst = min(evts, key=lambda e: _SEV_RANK.get(e["severity"], 9))
                logger.info(
                    f"[watcher] Auto-trigger: service={service} ns={ns} "
                    f"reason={worst['reason']} severity={worst['severity']}"
                )
                try:
                    enqueue_fn(
                        service=service,
                        namespace=ns,
                        error_type=worst["error_type"],
                        severity=worst["severity"],
                        alert_source="k8s-event-watcher",
                    )
                except Exception as exc:
                    logger.error(f"[watcher] Failed to enqueue incident: {exc}")


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

    # Event grouping buffer: namespace → list of event dicts
    pending: dict[str, list[dict]] = collections.defaultdict(list)

    logger.info(
        f"Event watcher started — monitoring Warning events across all namespaces "
        f"(cooldown={COOLDOWN}s, group_window={GROUP_WINDOW}s, reasons={list(TRIGGER_REASONS)})"
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

                now = time.time()

                # Flush stale groups whose window has elapsed
                stale = [
                    ns for ns, evts in pending.items()
                    if evts and (now - min(e["timestamp"] for e in evts)) >= GROUP_WINDOW
                ]
                for ns in stale:
                    _dispatch_group(pending.pop(ns), enqueue_fn, last_triggered, now)

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
                severity = TRIGGER_REASONS[reason]
                msg = (evt.message or "")[:200]
                error_type = f"{reason}: {msg}" if msg else reason

                event_dict = {
                    "service": service,
                    "namespace": ns,
                    "error_type": error_type,
                    "severity": severity,
                    "reason": reason,
                    "timestamp": now,
                }

                # P1 events flush the group immediately to avoid delay
                if reason in _P1_REASONS:
                    pending[ns].append(event_dict)
                    _dispatch_group(pending.pop(ns), enqueue_fn, last_triggered, now)
                else:
                    pending[ns].append(event_dict)

        except Exception as exc:
            if stop_event.is_set():
                break
            logger.warning(f"[watcher] Stream error, reconnecting in 15s: {exc}")
            time.sleep(15)

    # Flush remaining on shutdown
    now = time.time()
    for ns, evts in pending.items():
        if evts:
            _dispatch_group(evts, enqueue_fn, last_triggered, now)

    logger.info("Event watcher stopped.")
