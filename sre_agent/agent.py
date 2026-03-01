"""
SRE Agent Core - ReAct Loop
Implements Think → Act → Observe cycles using any OpenAI-compatible LLM with tool calling.
"""
import json
import logging
import time
from datetime import datetime
from typing import Optional
from openai import OpenAI

from .config import config
from .tools import TOOL_DEFINITIONS, execute_tool

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# System prompt
# ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an active SRE (Site Reliability Engineer) agent embedded in an HCI platform.
Your job is to investigate service failures and produce structured diagnostic reports.

## Investigation Protocol

When given an incident, follow this process strictly:
1. **Assess** - Understand what failed: service name, error type, affected namespace
2. **Logs first** - Always check logs for the failing service before anything else
3. **Correlate** - Check pod status, events, and metrics to confirm or expand the picture
4. **Hypothesize** - Form a specific root cause hypothesis based on evidence
5. **Verify** - Use more tools to confirm or refute your hypothesis
6. **Report** - Produce a structured JSON report (see output format below)

## Investigation Principles
- Use tools systematically. Don't guess — gather evidence first.
- Follow the RED method: check Request rate, Error rate, and Duration (latency).
- Check for common failure patterns: OOMKilled, CrashLoopBackOff, ImagePullBackOff, pending pods, node pressure.
- For HCI platform incidents, also check VM status and node health.
- If a fix requires destructive action (delete pod, restart service), call escalate_to_human instead.
- Stop investigating after 10 tool calls and produce your best report with available evidence.

## Output Format (REQUIRED at the end)

When you have finished investigating, respond ONLY with this JSON block (no other text):

```json
{
  "incident_id": "string",
  "severity": "P1|P2|P3",
  "title": "short one-line description",
  "affected_service": "string",
  "affected_namespace": "string",
  "investigation_summary": "2-3 sentence narrative of what you found",
  "root_cause": "specific technical cause, or 'unknown' if unclear",
  "evidence": ["list", "of", "key", "observations"],
  "timeline": ["timestamp: event description"],
  "recommended_actions": [
    {"action": "description", "priority": "immediate|short-term|long-term", "safe_to_automate": true}
  ],
  "runbook_refs": ["list of relevant runbook IDs"],
  "auto_resolved": false,
  "requires_escalation": false,
  "escalation_reason": null,
  "confidence": "high|medium|low"
}
```

Never output anything after the JSON block.
"""


# ─────────────────────────────────────────────────────────────
# Agent
# ─────────────────────────────────────────────────────────────

class SREAgent:
    def __init__(self, runbook_searcher=None):
        """
        Args:
            runbook_searcher: Optional RunbookSearcher instance for RAG lookups.
        """
        self.runbook_searcher = runbook_searcher
        self._client = OpenAI(
            base_url=config.llm.base_url,
            api_key=config.llm.api_key,
            timeout=config.llm.timeout,
        )

    def investigate(self, incident: dict) -> dict:
        """
        Run a full ReAct investigation for an incident.

        Args:
            incident: dict with keys like:
                - service (str): service name
                - namespace (str): k8s namespace
                - error_type (str): brief error description
                - alert_source (str): who triggered this
                - extra_context (dict): any additional metadata

        Returns:
            Parsed incident report dict.
        """
        start_time = time.time()
        incident_id = incident.get("incident_id", f"inc-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}")
        incident["incident_id"] = incident_id

        logger.info(f"[{incident_id}] Starting investigation for service={incident.get('service')}")

        # Enrich with runbook context if available
        runbook_context = ""
        if self.runbook_searcher:
            symptom = f"{incident.get('service')} {incident.get('error_type', '')}".strip()
            runbooks = self.runbook_searcher.search(symptom, top_k=2)
            if runbooks:
                runbook_context = "\n\nRelevant runbooks for context:\n" + "\n".join(
                    f"- [{r['id']}] {r['title']}: {r.get('summary') or (r.get('symptoms') or [''])[0]}"
                    for r in runbooks
                )

        user_message = (
            f"Investigate this incident:\n\n"
            f"{json.dumps(incident, indent=2, default=str)}"
            f"{runbook_context}"
        )

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ]

        tool_call_count = 0
        max_steps = config.llm.max_agent_steps
        final_report = None

        for step in range(max_steps):
            logger.debug(f"[{incident_id}] Agent step {step + 1}/{max_steps}")

            try:
                response = self._client.chat.completions.create(
                    model=config.llm.model,
                    messages=messages,
                    tools=TOOL_DEFINITIONS,
                    temperature=0.1,
                )
            except Exception as e:
                logger.error(f"[{incident_id}] LLM call failed: {e}")
                return self._error_report(incident_id, incident, str(e))

            msg = response.choices[0].message
            # Append assistant message (preserves tool_calls for OpenAI protocol)
            messages.append(msg)

            # Agent chose to call tools
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    fn_name = tc.function.name
                    fn_args = json.loads(tc.function.arguments)

                    tool_call_count += 1
                    logger.info(f"[{incident_id}] Tool: {fn_name}({json.dumps(fn_args, default=str)})")

                    result = execute_tool(fn_name, fn_args)

                    # tool_call_id is required by the OpenAI protocol
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(result, default=str)[:4000],
                    })

                    if fn_name == "escalate_to_human" and result.get("status") == "ok":
                        logger.warning(f"[{incident_id}] Agent requested human escalation: {fn_args.get('reason')}")

            else:
                # No tool calls → agent is producing final output
                final_text = (msg.content or "").strip()
                final_report = self._parse_report(final_text, incident_id, incident)
                break

        else:
            # Hit max steps without a final answer
            logger.warning(f"[{incident_id}] Max steps reached, forcing report generation.")
            messages.append({
                "role": "user",
                "content": "You've gathered enough data. Now produce the final JSON report immediately."
            })
            try:
                response = self._client.chat.completions.create(
                    model=config.llm.model,
                    messages=messages,
                    temperature=0.0,
                )
                final_report = self._parse_report(
                    (response.choices[0].message.content or "").strip(), incident_id, incident
                )
            except Exception as e:
                final_report = self._error_report(incident_id, incident, f"Forced report failed: {e}")

        duration = round(time.time() - start_time, 1)
        if final_report:
            final_report["investigation_duration_s"] = duration
            final_report["tool_calls_made"] = tool_call_count
            final_report.setdefault("incident_id", incident_id)

        logger.info(f"[{incident_id}] Investigation complete in {duration}s ({tool_call_count} tool calls)")
        return final_report or self._error_report(incident_id, incident, "No report generated")

    def _parse_report(self, text: str, incident_id: str, incident: dict) -> dict:
        """Extract JSON report from agent output."""
        start = text.find("{")
        end = text.rfind("}") + 1
        if start != -1 and end > start:
            json_str = text[start:end]
            try:
                report = json.loads(json_str)
                report.setdefault("incident_id", incident_id)
                report.setdefault("affected_service", incident.get("service", "unknown"))
                report.setdefault("raw_agent_output", text if len(text) < 200 else None)
                return report
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse report JSON: {e}. Raw: {text[:300]}")

        return {
            "incident_id": incident_id,
            "severity": "P2",
            "title": "Investigation complete (unstructured output)",
            "affected_service": incident.get("service", "unknown"),
            "root_cause": "unknown",
            "evidence": [],
            "investigation_summary": text[:1000],
            "recommended_actions": [],
            "confidence": "low",
            "parse_error": True,
        }

    def _error_report(self, incident_id: str, incident: dict, reason: str) -> dict:
        return {
            "incident_id": incident_id,
            "severity": "P2",
            "title": "Investigation failed",
            "affected_service": incident.get("service", "unknown"),
            "root_cause": "unknown",
            "evidence": [f"Agent error: {reason}"],
            "investigation_summary": f"The SRE agent encountered an error during investigation: {reason}",
            "recommended_actions": [
                {"action": "Manual investigation required", "priority": "immediate", "safe_to_automate": False}
            ],
            "requires_escalation": True,
            "escalation_reason": reason,
            "confidence": "low",
            "error": reason,
        }
