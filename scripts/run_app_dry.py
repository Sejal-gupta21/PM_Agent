import sys
import os
import json
import logging

sys.path.insert(0, os.getcwd())

from billing_deviation.billing_orchestrator import BillingDeviationOrchestrator

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def run():
    orch = BillingDeviationOrchestrator()

    # Monkeypatch fetcher methods to avoid external ADO calls
    def fake_fetch_work_items(iteration_path):
        # Return a non-empty placeholder list
        return [{"id": 58495, "fields": {"System.Title": "Mock WI", "System.State": "Active"}}]

    def fake_extract_effort_data(work_items):
        # Simulate aggregated effort matching the user's example
        return {
            'total_completed_work': 3324.0,
            'by_area': {},
        }

    orch.fetcher.fetch_work_items_by_iteration = fake_fetch_work_items
    orch.fetcher.extract_effort_data = fake_extract_effort_data

    # Provide billing targets explicitly to get target=2000
    billing_targets = {'Default': 2000.0}

    result = orch.generate_billing_deviation_report(iteration_path='@CurrentIteration', billing_targets=billing_targets, recipient_email=None)

    if result.get('success'):
        out_dir = os.path.join(os.getcwd(), 'outputs')
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, 'billing_deviation_report_dryrun.html')
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(result.get('html_report', ''))
        logger.info(f"Dry-run report saved to: {path}")
        print(path)
    else:
        logger.error(f"Report generation failed: {result}")

if __name__ == '__main__':
    run()
