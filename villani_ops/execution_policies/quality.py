from villani_ops.execution_policies.cheap import CheapExecutionPolicy, _strongest
from villani_ops.execution_policies.base import BackendSelection
class QualityExecutionPolicy(CheapExecutionPolicy):
    mode='quality'
    def select_backend(self, **kwargs):
        node=kwargs['node']; conf=kwargs.get('confidence'); backends=kwargs['backends']
        if node.kind != 'code' or conf is None or conf < .95:
            n,_=_strongest(backends); return BackendSelection(backend_name=n, reason='Quality mode stayed close to performance and escalated aggressively.', escalated=conf is not None and conf < .95)
        return super().select_backend(**kwargs)
