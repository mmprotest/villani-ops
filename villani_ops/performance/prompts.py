INVESTIGATOR_SYSTEM = "Return only JSON. You investigate coding tasks. Do not propose edits outside the repo."
INVESTIGATOR_USER = """Analyze this coding task and return JSON matching InvestigationResult.
Include a first-class validation_plan when reliable commands can be inferred from repository files or package scripts. The validation_plan shape is: {"commands":[{"cmd":"command string","required":true,"reason":"why relevant","timeout_seconds":null}],"notes":[],"success_criteria_mapping":[],"fallback":false,"source":"investigation"}. Mark optional commands with required=false. Do not hardcode a language; infer commands from the repo context. If no reliable command can be inferred, omit commands or mark fallback=true.
{context}"""
SELECTOR_SYSTEM = "Return only JSON. Select the best eligible coding candidate or reject all."
SELECTOR_USER = """Choose among reviewed candidates and return JSON with this shape:
{{
  "selected_attempt_id": "...",
  "decision": "select",
  "summary": "Why this candidate won in one or two sentences",
  "reasons": [
    "Specific reason based on review/result/evidence",
    "Specific reason alternatives were weaker"
  ],
  "rejected_attempts": ["attempt_001", "attempt_002"],
  "confidence": 0.0
}}
Choose only from existing candidate attempt ids. Do not invent attempt ids. Do not choose ineligible candidates. If choosing a winner, explain why it beat alternatives. Include at least one reason. If rejecting all, include meaningful reasons. Do not choose based on cost. Choose based on correctness, review result, acceptance eligibility, patch content, changed files, test evidence, failure evidence, risk, and minimal safe diff when correctness is tied.
{context}"""
