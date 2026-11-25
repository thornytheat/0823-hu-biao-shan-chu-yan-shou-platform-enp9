from app.supplement.service import *  # noqa: F401,F403
from app.supplement.service import (  # noqa: F401
    apply_supplement,
    present_supplement,
    recover_supplement,
    resume_supplement,
)

supplement = apply_supplement
supplement_run = apply_supplement
add_supplement = apply_supplement
create_supplement = apply_supplement
record_supplement = apply_supplement
register_supplement = apply_supplement
submit_supplement = apply_supplement
process_supplement = apply_supplement
retry_supplement = resume_supplement
recover = recover_supplement

__all__ = [
    'apply_supplement', 'supplement', 'supplement_run', 'add_supplement',
    'create_supplement', 'record_supplement', 'register_supplement',
    'submit_supplement', 'process_supplement', 'resume_supplement',
    'retry_supplement', 'recover_supplement', 'present_supplement',
]
