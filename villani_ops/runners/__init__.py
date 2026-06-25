from villani_ops.runners.villani_code import VillaniCodeRunner

def runner_for_name(name: str):
    normalized=name.replace('_','-')
    if normalized == 'villani-code':
        return VillaniCodeRunner()
    supported='villani-code'
    raise ValueError(f"Unsupported runner '{name}'. Supported runner: {supported}. Future adapters exist for claude-code, pi, aider, and codex but are not enabled yet.")
__all__=['VillaniCodeRunner','runner_for_name']
