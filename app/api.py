from flask import Blueprint, Response, current_app, jsonify, request

from app.business import DomainIssue
from app.evidence.render import artifact
from app.inputs import read_input, read_supplement_request
from app.modules.pipeline import run
from app.review.service import execute, review
from app.supplement.service import (
    apply_supplement,
    issue_supplement,
    present_supplement,
    resume_supplement,
)
from app.audit import reverify_snapshot

api = Blueprint('api',__name__,url_prefix='/api')
STAGES = {'intake':1,'prepare':2,'calculate':3,'policy':4,'balance':5}


@api.post('/stages/<stage>')
def preview(stage):
    if stage not in STAGES:
        raise DomainIssue('STAGE_UNKNOWN')
    return jsonify(run(read_input(),STAGES[stage]))


@api.route('/runs',methods=['GET','POST'])
def runs():
    store = current_app.extensions['runs']
    if request.method == 'GET':
        return jsonify(items=store.list())
    return jsonify(execute(store,read_input()))


@api.route('/runs/<run_id>', methods=['GET','PATCH'])
def batch(run_id):
    store = current_app.extensions['runs']
    if request.method == 'GET':
        result = store.load(run_id)
        lineage = _lineage(store, result)
        if lineage:
            result['lineage'] = lineage
            result['original_run_id'] = lineage['original_run_id']
            result['current_run_id'] = lineage['current_run_id']
            result['head_run_id'] = lineage['head_run_id']
        return jsonify(result)
    return jsonify(review(store,run_id,request.get_json(silent=True)))


@api.get('/runs/<run_id>/evidence')
def export(run_id):
    report = current_app.extensions['runs'].load(run_id)
    return Response(artifact(report),mimetype='application/json', headers={
        'Content-Disposition':f'attachment; filename="review-{report["id"][:12]}.json"',
        'Cache-Control':'no-store',
    })


@api.route('/runs/<run_id>/verify', methods=['GET', 'POST'])
@api.route('/runs/<run_id>/reverify', methods=['GET', 'POST'])
def verify_run(run_id):
    return jsonify(reverify_snapshot(current_app.extensions['runs'], run_id))


@api.route('/audit/runs/<run_id>/verify', methods=['GET', 'POST'])
@api.route('/audit/<run_id>', methods=['GET'])
def audit_verify_run(run_id):
    return jsonify(reverify_snapshot(current_app.extensions['runs'], run_id))


def _public_supplement(operation):
    item = dict(operation)
    item.pop('request_data', None)
    return item


def _lineage(store, run):
    report = run.get('report') or {}
    supplement = report.get('supplement') or {}
    root_id = supplement.get('original_run_id')
    if not root_id:
        chain = store.version_chain_for_run(run['id'])
        if not chain:
            return None
        root_id = chain[0]['root_run_id']
    versions = store.list_versions(root_id)
    head_run_id = versions[-1]['run_id'] if versions else run['id']
    return {
        'original_run_id': root_id,
        'head_run_id': head_run_id,
        'current_run_id': head_run_id,
        'requested_run_id': run['id'],
        'versions': versions,
        'supplements': [_public_supplement(item) for item in store.list_supplements(root_id)],
    }


@api.route('/runs/<run_id>/versions', methods=['GET', 'POST'])
def run_versions(run_id):
    store = current_app.extensions['runs']
    if request.method == 'POST':
        return jsonify(_create_supplement(run_id))
    run = store.load(run_id)
    lineage = _lineage(store, run) or {
        'original_run_id': run_id,
        'current_run_id': run_id,
        'versions': [],
        'supplements': store.list_supplements(run_id),
    }
    root_id = lineage['original_run_id']
    lineage['original'] = store.load(root_id)
    lineage['requested_run'] = run
    lineage['current'] = store.load(lineage['current_run_id'])
    return jsonify(lineage)


def _create_supplement(run_id=None):
    store = current_app.extensions['runs']
    data, key, options = read_supplement_request()
    body_run_id = options.pop('run_id', None)
    target_id = run_id or body_run_id
    if not target_id:
        raise DomainIssue('RUN_ID_REQUIRED')
    return apply_supplement(store, target_id, data, key, **options)


@api.post('/runs/<run_id>/supplement')
@api.post('/runs/<run_id>/supplements')
@api.post('/runs/<run_id>/correction')
@api.post('/runs/<run_id>/corrections')
@api.post('/runs/<run_id>/backfill')
@api.post('/runs/<run_id>/backfills')
@api.post('/runs/<run_id>/adjustment')
@api.post('/runs/<run_id>/adjustments')
@api.post('/runs/<run_id>/late-deletes')
@api.post('/runs/<run_id>/late-records')
@api.post('/runs/<run_id>/deletions')
@api.post('/runs/<run_id>/exclusions')
@api.post('/runs/<run_id>/additions')
@api.post('/runs/<run_id>/records')
@api.post('/runs/<run_id>/revisions')
@api.put('/runs/<run_id>/supplement')
@api.put('/runs/<run_id>/supplements')
def run_supplement(run_id):
    return jsonify(_create_supplement(run_id))


@api.route('/runs/<run_id>/supplements/<key>', methods=['POST', 'PUT'])
def put_run_supplement(run_id, key):
    data, request_key, options = read_supplement_request()
    options.pop('run_id', None)
    return jsonify(apply_supplement(current_app.extensions['runs'], run_id, data,
                                    request_key or key, **options))


@api.post('/runs/<run_id>/supplements/<key>/retry')
@api.post('/runs/<run_id>/supplements/<key>/resume')
def retry_run_supplement_key(run_id, key):
    store = current_app.extensions['runs']
    operation = store.find_supplement(run_id, key)
    if operation is None:
        raise DomainIssue('SUPPLEMENT_NOT_FOUND')
    return jsonify(resume_supplement(store, operation['id']))


@api.post('/supplements')
@api.post('/corrections')
@api.post('/backfills')
@api.post('/adjustments')
@api.post('/deletions')
@api.post('/exclusions')
@api.post('/late-records')
def supplements_collection():
    return jsonify(_create_supplement())


@api.get('/supplements')
def supplements_list():
    base_run_id = request.args.get('run_id') or request.args.get('base_run_id')
    items = [_public_supplement(item)
             for item in current_app.extensions['runs'].list_supplements(base_run_id)]
    return jsonify(items=items)


@api.get('/issuances')
def issuances_list():
    supplement_id = request.args.get('supplement_id')
    return jsonify(items=current_app.extensions['runs'].list_issuances(supplement_id))


@api.get('/runs/<run_id>/issuances')
def run_issuances(run_id):
    store = current_app.extensions['runs']
    store.load(run_id)
    items = [item for item in store.list_issuances() if item['run_id'] == run_id]
    return jsonify(items=items)


@api.get('/supplements/<supplement_id>')
def supplement_detail(supplement_id):
    store = current_app.extensions['runs']
    return jsonify(present_supplement(store, supplement_id))


@api.route('/supplements/<supplement_id>', methods=['POST', 'PUT'])
def supplement_upsert_or_retry(supplement_id):
    store = current_app.extensions['runs']
    try:
        data, key, options = read_supplement_request()
    except DomainIssue as error:
        if error.code != 'SUPPLEMENT_REQUIRED' or request.method != 'POST':
            raise
        return jsonify(resume_supplement(store, supplement_id))
    run_id = options.pop('run_id', None) or request.args.get('run_id')
    if run_id:
        return jsonify(apply_supplement(store, run_id, data, key or supplement_id, **options))
    return jsonify(resume_supplement(store, supplement_id))


@api.post('/supplements/<supplement_id>/resume')
@api.post('/supplements/<supplement_id>/recover')
@api.post('/supplements/<supplement_id>/retry')
def supplement_resume(supplement_id):
    return jsonify(resume_supplement(current_app.extensions['runs'], supplement_id))


@api.post('/supplements/<supplement_id>/issue')
def supplement_issue(supplement_id):
    body = request.get_json(silent=True) if request.is_json else request.form
    body = body or {}
    approve = body.get('approve')
    if isinstance(approve, str):
        approve = approve.lower() in ('1', 'true', 'yes', 'on')
    return jsonify(issue_supplement(
        current_app.extensions['runs'], supplement_id,
        status=body.get('status') or body.get('decision'),
        note=body.get('note'),
        approve=approve))


# Common singular spelling aliases.
@api.post('/supplement')
@api.put('/supplement')
def supplement_singular_collection():
    return jsonify(_create_supplement())


@api.route('/supplement/<supplement_id>', methods=['GET', 'POST', 'PUT'])
def supplement_singular_detail(supplement_id):
    store = current_app.extensions['runs']
    if request.method == 'GET':
        return jsonify(present_supplement(store, supplement_id))
    try:
        data, key, options = read_supplement_request()
        run_id = options.pop('run_id', None) or request.args.get('run_id')
        if run_id:
            return jsonify(apply_supplement(store, run_id, data, key or supplement_id, **options))
    except DomainIssue as error:
        if error.code != 'SUPPLEMENT_REQUIRED':
            raise
    return jsonify(resume_supplement(store, supplement_id))


@api.post('/supplement/<supplement_id>/resume')
@api.post('/supplement/<supplement_id>/recover')
def supplement_singular_resume(supplement_id):
    return jsonify(resume_supplement(current_app.extensions['runs'], supplement_id))
