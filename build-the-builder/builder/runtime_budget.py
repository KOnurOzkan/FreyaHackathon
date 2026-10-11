"""Conservative prepublication bound for Freya's code_exec input size.

Saved live errors establish a 131072-byte combined code/data limit. The complete
sandbox envelope and its serializer are not exposed, so this is an engineering
allowance, not a claim to reproduce the server's exact byte calculation. The
compiler must also exercise realistic runtime state before relying on the
variable allowance. Passing this guard does not establish platform execution.
"""
import json

from .inputs import InvalidInput


SANDBOX_INPUT_LIMIT_BYTES = 131072
# Measured, not documented: the rejected A052 request carried about 17,900 bytes
# beyond its 112,335 code bytes and 2,250 variable bytes, and 99,238-byte code
# ran a 19-step live call (out/code-exec-size-observations-052.json). 18,432
# keeps a margin above that overhead; the variable allowance and slack remain.
PROTOCOL_RESERVE_BYTES = 18432
MINIMUM_VARIABLE_BUDGET_BYTES = 20480
SAFETY_SLACK_BYTES = 4096


class RuntimeBudgetError(InvalidInput):
    """The proposed source/state allowance cannot fit the conservative budget."""


def validate_runtime_budget(code, variable_budget_bytes=MINIMUM_VARIABLE_BUDGET_BYTES):
    """Return a size-only report, or reject without including code contents.

    JSON escaping is counted with ensure_ascii=True, including the surrounding
    string quotes. A larger measured state requires a larger variable budget;
    callers cannot silently reduce the default state allowance to fit source.
    No workflow, credential, source file, or network state is changed.
    """
    if not isinstance(code, str) or not code.strip():
        raise RuntimeBudgetError('Runtime source must be nonempty text; no source contents are included in this error')
    if type(variable_budget_bytes) is not int or variable_budget_bytes < MINIMUM_VARIABLE_BUDGET_BYTES:
        raise RuntimeBudgetError('Runtime variable allowance must be an integer of at least '
                                 + str(MINIMUM_VARIABLE_BUDGET_BYTES) + ' bytes')
    if variable_budget_bytes + PROTOCOL_RESERVE_BYTES + SAFETY_SLACK_BYTES >= SANDBOX_INPUT_LIMIT_BYTES:
        raise RuntimeBudgetError('Runtime variable allowance leaves no room for executable source')
    try:
        raw_bytes = len(code.encode('utf-8'))
        encoded_bytes = len(json.dumps(code, ensure_ascii=True).encode('utf-8'))
    except (UnicodeError, ValueError, TypeError):
        raise RuntimeBudgetError('Runtime source cannot be encoded as valid UTF-8/JSON text') from None
    code_limit = SANDBOX_INPUT_LIMIT_BYTES - variable_budget_bytes - PROTOCOL_RESERVE_BYTES - SAFETY_SLACK_BYTES
    if encoded_bytes > code_limit:
        raise RuntimeBudgetError('Runtime source exceeds the conservative sandbox budget: encoded code '
            + str(encoded_bytes) + ' bytes, maximum ' + str(code_limit) + '; reduce compiled source or static payload. '
            'Do not reduce the required state allowance or truncate replay history. The complete server protocol overhead is unknown.')
    reserved_total = encoded_bytes + variable_budget_bytes + PROTOCOL_RESERVE_BYTES + SAFETY_SLACK_BYTES
    return {'status': 'within_conservative_budget', 'scope': 'size_only_not_live_execution_validation',
            'limit_bytes': SANDBOX_INPUT_LIMIT_BYTES, 'raw_source_utf8_bytes': raw_bytes,
            'encoded_code_json_bytes': encoded_bytes, 'maximum_encoded_code_bytes': code_limit,
            'variable_allowance_bytes': variable_budget_bytes, 'protocol_reserve_bytes': PROTOCOL_RESERVE_BYTES,
            'protocol_overhead_exactly_known': False, 'safety_slack_bytes': SAFETY_SLACK_BYTES,
            'reserved_total_bytes': reserved_total, 'remaining_reserved_headroom_bytes': SANDBOX_INPUT_LIMIT_BYTES-reserved_total,
            'state_validation_required': 'Measure long conversation state, including platform variables and response envelopes; increase the allowance if needed.'}
