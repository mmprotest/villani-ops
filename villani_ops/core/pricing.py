from .backend import Backend

def estimate_cost(input_tokens: int, output_tokens: int, backend: Backend) -> float:
    return (input_tokens / 1_000_000 * backend.input_cost_per_million) + (output_tokens / 1_000_000 * backend.output_cost_per_million)
