import json


def artifact(run):
    report = run['report']
    packet = {
        'schema': 'review-evidence/1',
        'manifest': report['manifest'],
        'balance': report['balance'],
        'cases': report['cases'],
        'intake': report['intake'],
        'review': {key:run[key] for key in ('status','revision','note','reviewed')},
    }
    for key in ('supplement', 'changes', 'marks', 'markers', 'dangling_records',
                'missing_position_deletes', 'marked_records', 'flagged_records',
                'current_balance', 'annotations'):
        if key in report:
            packet[key] = report[key]
    return (json.dumps(packet, ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode()
