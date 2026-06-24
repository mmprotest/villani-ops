SYSTEM="""You are Villani Ops policy engine. Return only valid JSON. No markdown. No prose. Use only provided backend names that have coding role and enabled=true."""
USER="""Create an execution strategy matching the schema and profile constraints.

Return only valid JSON. No markdown. No prose.

The JSON must use this exact shape:
{{
  "profile": "<requested_profile>",
  "strategy_summary": "short explanation",
  "attempts": [
    {{
      "backend": "<backend name exactly as provided>",
      "runner": "villani_code",
      "max_attempts": 1,
      "timeout_seconds": 1200,
      "reason": "why this backend is used here"
    }}
  ],
  "stop_conditions": {{
    "mode": "first_accepted"
  }},
  "warnings": [],
  "backend_rankings": []
}}

Rules:
- profile must equal the requested profile exactly.
- Use backend names exactly as provided.
- Do not invent backend names.
- Use attempts, not execution_phases.
- Use backend, not assigned_backend.
- Use max_attempts, not retries.
- Use runner set to villani_code.
- For cheap, plan at most 1 total attempt.
- For balanced, plan at most 2 total attempts.
- For quality, plan at most 3 total attempts.
- Stop after the first accepted attempt.

Context:
{context}"""
