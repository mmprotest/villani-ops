INVESTIGATOR_SYSTEM = "Return only JSON. You investigate coding tasks. Do not propose edits outside the repo."
INVESTIGATOR_USER = """Analyze this coding task and return JSON matching InvestigationResult.\n{context}"""
SELECTOR_SYSTEM = "Return only JSON. Select the best eligible coding candidate or reject all."
SELECTOR_USER = """Choose among reviewed candidates. Select only acceptance_eligible candidates. Choose based on correctness, review result, acceptance eligibility, patch content, changed files, test evidence, failure evidence, risk, and minimal safe diff when correctness is tied.\n{context}"""
