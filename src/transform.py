"""Temporary TRMNL serverless pass-through used during Worker cutover.

TRMNL places parsed polling JSON in ``input["data"]``. The Worker already
returns the complete flat merge-variable contract, so no request-path data
processing remains here. Keep ``transform.py`` untouched as rollback.
"""


def run(input):
    data = input.get("data") if isinstance(input, dict) else None
    return data if isinstance(data, dict) else input
