from villani_ops.runners.villani_code import VillaniCodeRunner, VillaniCodeAdapter
from villani_ops.runners.base import UnsupportedRunnerAdapter

def runner_for_name(name: str):
    normalized=name.replace('_','-')
    if normalized == 'villani-code': return VillaniCodeAdapter()
    if normalized in {'claude-code','pi','aider','codex'}: return UnsupportedRunnerAdapter(normalized)
    raise ValueError(f"Unsupported runner '{name}'. Supported runner: villani-code.")
__all__=['VillaniCodeRunner','VillaniCodeAdapter','runner_for_name']
