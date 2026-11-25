from flask import request

from app.business import DomainIssue
from app.config import MAX_FILE, ROOT

SAMPLES = {'dirty': 'incoming.csv', 'clean': 'ready.csv'}


def read_input():
    sample = request.form.get('sample')
    if sample is not None:
        if sample not in SAMPLES:
            raise DomainIssue('SAMPLE_UNKNOWN')
        return (ROOT/'sample_data'/SAMPLES[sample]).read_bytes()
    uploaded = request.files.get('file')
    if uploaded is None or not uploaded.filename:
        raise DomainIssue('FILE_REQUIRED')
    value = uploaded.stream.read(MAX_FILE+1)
    if len(value)>MAX_FILE:
        raise DomainIssue('FILE_TOO_LARGE')
    return value


def _limited_upload(uploaded):
    value = uploaded.stream.read(MAX_FILE + 1)
    if len(value) > MAX_FILE:
        raise DomainIssue('FILE_TOO_LARGE')
    return value


def read_supplement_request():
    """Return ``(data, idempotency_key, options)`` for supplement requests."""
    key = request.headers.get('Idempotency-Key') or request.headers.get('X-Idempotency-Key')
    options = {}

    if request.is_json:
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise DomainIssue('REVIEW_FIELDS')
        key = key or body.get('idempotency_key') or body.get('key') \
            or body.get('supplement_id') or body.get('request_id') \
            or body.get('client_token') or body.get('token')
        options['note'] = body.get('note') or body.get('review_note') or body.get('comment')
        options['auto_issue'] = body.get('auto_issue', True)
        options['mode'] = body.get('mode', 'append')
        run_id = body.get('run_id') or body.get('base_run_id')
        if body.get('records') is not None:
            return {'records': body['records']}, key, {**options, 'run_id': run_id}
        for field in ('csv', 'content', 'data', 'text'):
            if body.get(field) is not None:
                return body[field], key, {**options, 'run_id': run_id}
        raise DomainIssue('SUPPLEMENT_REQUIRED')

    if request.data and request.mimetype in ('text/csv', 'application/csv', 'text/plain'):
        return request.data, key or request.args.get('key'), options

    key = key or request.form.get('idempotency_key') or request.form.get('key') \
        or request.form.get('supplement_id') or request.form.get('request_id') \
        or request.form.get('client_token') or request.form.get('token')
    options['note'] = request.form.get('note') or request.form.get('review_note') or request.form.get('comment')
    options['mode'] = request.form.get('mode', 'append')
    if 'auto_issue' in request.form:
        options['auto_issue'] = request.form.get('auto_issue') not in ('0', 'false', 'False', '')

    sample = request.form.get('sample') or request.form.get('supplement_sample')
    if sample is not None:
        if sample not in SAMPLES:
            raise DomainIssue('SAMPLE_UNKNOWN')
        return (ROOT/'sample_data'/SAMPLES[sample]).read_bytes(), key, options

    uploaded = request.files.get('file') or request.files.get('supplement') \
        or request.files.get('csv') or request.files.get('patch') \
        or request.files.get('deletions') or request.files.get('data_file')
    if uploaded is not None and uploaded.filename:
        return _limited_upload(uploaded), key, options

    for field in ('csv', 'content', 'payload', 'data'):
        if request.form.get(field):
            return request.form[field], key, options

    raise DomainIssue('SUPPLEMENT_REQUIRED')
