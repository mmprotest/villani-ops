from villani_ops.execution_policies.cheap import CheapExecutionPolicy, _strongest
from villani_ops.execution_policies.base import BackendSelection
class BalancedExecutionPolicy(CheapExecutionPolicy):
    mode='balanced'
    def select_backend(self, **kwargs):
        node=kwargs['node']; conf=kwargs.get('confidence'); backends=kwargs['backends']
        if node.kind in {'investigate','review','select','verify'} or conf is None or conf < .8:
            n,_=_strongest(backends); return BackendSelection(backend_name=n, reason='Balanced mode preferred a capable backend for ambiguous/high-impact orchestration node.', escalated=conf is not None and conf < .8)
        return super().select_backend(**kwargs)
