from app.supplement.service import (
    apply_supplement,
    present_supplement as get_version,
    resume_supplement,
)

create_version = apply_supplement
add_version = apply_supplement
supplement_version = apply_supplement
retry_version = resume_supplement
recover_version = resume_supplement

__all__ = [
    'apply_supplement', 'create_version', 'add_version',
    'supplement_version', 'get_version', 'resume_supplement',
    'retry_version', 'recover_version',
]
