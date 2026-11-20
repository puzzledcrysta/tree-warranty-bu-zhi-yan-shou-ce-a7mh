import json
from app.modules.runs.service import get
from app.modules.settle.service import plan
from .decisions import timeline


def bundle(run_id):
    history = timeline(run_id)
    run = get(run_id)
    # The export carries source line evidence, the applicable decision version and signed amounts,
    # including signoffs from earlier periods for the same keys.
    return json.dumps({'manifest':run['manifest'],'counts':run['counts'],'results':run['accepted'],
                       'quarantine':run['quarantine'],'suppressed':run['suppressed'],
                       'reconciliation':run['book'],'review':history,
                       'signoffs':plan(run_id)['signoffs']},ensure_ascii=False,indent=2)
