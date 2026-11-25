from app.supplement.service import *  # noqa: F401,F403
from app.supplement.service import (  # noqa: F401
    _idempotent_review,
    annotate_and_assess,
    apply_supplement,
    merge_sources,
    present_supplement,
    record_line_numbers,
    resume_supplement,
    run_current_version,
)


def add(*args, **kwargs):
    return apply_supplement(*args, **kwargs)


def create(*args, **kwargs):
    return apply_supplement(*args, **kwargs)
