from .base import ExecutionPolicy, BackendSelection
from .performance import PerformanceExecutionPolicy
from .cheap import CheapExecutionPolicy
from .balanced import BalancedExecutionPolicy
from .quality import QualityExecutionPolicy

def policy_for_mode(mode: str):
    return {'performance':PerformanceExecutionPolicy,'cheap':CheapExecutionPolicy,'balanced':BalancedExecutionPolicy,'quality':QualityExecutionPolicy}[mode]()
__all__=['ExecutionPolicy','BackendSelection','PerformanceExecutionPolicy','CheapExecutionPolicy','BalancedExecutionPolicy','QualityExecutionPolicy','policy_for_mode']
