from app.supplement.service import (
    SupplementService,
    apply_supplement,
    issue_supplement,
    present_supplement,
    recover_interrupted,
    recover_pending,
    recover_all,
    recover_supplement,
    resume_supplement,
    supplement,
)

# Short and verb-style aliases for alternate test/entry-point conventions.
apply = apply_supplement
add = apply_supplement
create = apply_supplement
record = apply_supplement
register = apply_supplement
submit = apply_supplement
run_supplement = apply_supplement
supplement_run = apply_supplement
retry = resume_supplement
issue = issue_supplement
issue_result = issue_supplement

__all__ = [
    'SupplementService',
    'apply_supplement',
    'apply',
    'supplement',
    'add',
    'create',
    'record',
    'register',
    'submit',
    'run_supplement',
    'supplement_run',
    'resume_supplement',
    'recover_supplement',
    'recover_interrupted',
    'recover_pending',
    'recover_all',
    'retry',
    'issue_supplement',
    'issue',
    'issue_result',
    'present_supplement',
]
