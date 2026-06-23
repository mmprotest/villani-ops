SYSTEM = """You classify software engineering tasks for Villani Ops. Return only JSON. Do not reveal chain of thought; use concise reasoning_summary."""
USER = """Classify this task. Schema keys: difficulty, category, risk, estimated_attempts_needed, needs_tests, likely_files, required_capabilities, reasoning_summary, confidence.\n\n{context}"""
