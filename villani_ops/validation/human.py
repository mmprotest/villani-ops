from .base import ValidationResult

class HumanValidator:
    name = "human"
    def validate(self, accepted: bool, summary: str = "Human override") -> ValidationResult:
        return ValidationResult(passed=accepted, score=1.0 if accepted else 0.0, summary=summary, validator=self.name)
