import importlib.util
import sys
from pathlib import Path
project_root = str(Path(__file__).resolve().parent.parent)
if project_root not in sys.path:
	sys.path.insert(0, project_root)

spec = importlib.util.find_spec('billing_deviation.billing_orchestrator')
print('spec found:', bool(spec))
from billing_deviation.billing_orchestrator import BillingDeviationOrchestrator
print('Imported BillingDeviationOrchestrator OK')
