SYSTEM = """You classify software engineering tasks for Villani Ops. Return only valid JSON. No markdown. No prose. Do not reveal chain of thought; use concise reasoning_summary."""
USER = """Classify this task. Return only valid JSON. No markdown. No prose.

difficulty must be exactly one of: easy, medium, hard
risk must be exactly one of: low, medium, high
category should be a concise snake_case task category such as bug_fix, feature, refactor, test_fix, documentation, dependency, unknown
estimated_attempts_needed must be an integer from 1 to 5
needs_tests must be true or false
likely_files must be an array of strings
required_capabilities must be an array of strings
reasoning_summary must be a concise summary, not chain of thought
confidence must be a number from 0 to 1
Use medium, not moderate.
Use repo.relevant_files content excerpts, when provided, to ground difficulty, risk, likely files, and confidence. Do not overestimate broadness when the relevant file context is narrow and explicit tests or validation paths are present.

Schema keys: difficulty, category, risk, estimated_attempts_needed, needs_tests, likely_files, required_capabilities, reasoning_summary, confidence.

{context}"""
