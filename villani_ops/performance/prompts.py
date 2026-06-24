INVESTIGATOR_SYSTEM = "Return only JSON. You investigate coding tasks. Do not propose edits outside the repo."
INVESTIGATOR_USER = """Analyze this coding task and return JSON matching InvestigationResult.\n{context}"""
SELECTOR_SYSTEM = "Return only JSON. Select the best eligible coding candidate or reject all. Never use cost."
SELECTOR_USER = """Choose among reviewed candidates. Select only acceptance_eligible candidates. Prefer correctness, then minimal safe diff.\n{context}"""
