"""A deliberately bounded OpenAPI reader, schema checker and Freya tool emitter."""
from copy import deepcopy
from datetime import date, datetime
import math
import re
from urllib.parse import urlsplit

from .inputs import InvalidInput

TOOL_NAMES = frozenset({"verify_identity", "get_account", "create_promise", "add_note",
                        "schedule_callback", "transfer_to_agent", "close_call"})
SCHEMA_KEYS = {"type", "properties", "required", "additionalProperties", "items", "enum",
               "description", "title", "default", "example", "examples", "format", "pattern",
               "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "minLength",
               "maxLength", "minItems", "maxItems", "nullable", "readOnly", "writeOnly", "deprecated"}


def check_schema(schema, where="schema"):
    if not isinstance(schema, dict) or set(schema) - SCHEMA_KEYS:
        raise InvalidInput(f"{where}: unsupported schema keyword or shape")
    kind = schema.get("type")
    if not isinstance(kind, str) or kind not in {"object", "array", "string", "integer", "number", "boolean"}:
        raise InvalidInput(f"{where}: an explicit supported type is required")
    if schema.get("nullable"):
        raise InvalidInput(f"{where}: nullable fields are not supported by this Freya emitter")
    if kind == "object":
        props, required = schema.get("properties", {}), schema.get("required", [])
        if not isinstance(props, dict) or not isinstance(required, list) or not all(isinstance(x, str) for x in required) or not set(required) <= set(props):
            raise InvalidInput(f"{where}: invalid properties or required fields")
        additional = schema.get("additionalProperties", True)
        if not isinstance(additional, bool):
            raise InvalidInput(f"{where}: schema-valued additionalProperties is unsupported")
        for key, value in props.items():
            check_schema(value, f"{where}.{key}")
    if kind == "array":
        check_schema(schema.get("items"), f"{where}[]")
    if "enum" in schema and (not isinstance(schema["enum"], list) or not schema["enum"]):
        raise InvalidInput(f"{where}: enum must be a nonempty list")
    for key in ("minLength", "maxLength", "minItems", "maxItems"):
        if key in schema and (type(schema[key]) is not int or schema[key] < 0):
            raise InvalidInput(f"{where}: invalid length constraint")
    for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
        if key in schema:
            value = schema[key]
            if key.startswith("exclusive") and type(value) is bool:
                if value and ("minimum" if key == "exclusiveMinimum" else "maximum") not in schema:
                    raise InvalidInput(f"{where}: exclusive flag needs its numeric bound")
            elif type(value) not in (int, float) or not math.isfinite(value):
                raise InvalidInput(f"{where}: invalid numeric constraint")
    if schema.get("minimum", -math.inf) > schema.get("maximum", math.inf):
        raise InvalidInput(f"{where}: reversed numeric bounds")
    if schema.get("format") not in {None, "date", "date-time", "int32", "int64", "float", "double"}:
        raise InvalidInput(f"{where}: unsupported format")
    if "pattern" in schema:
        try:
            re.compile(schema["pattern"])
        except (re.error, TypeError) as exc:
            raise InvalidInput(f"{where}: invalid pattern") from exc


def validate_value(value, schema, where="body"):
    kind = schema["type"]
    # JSON Schema integers include numbers with no fractional component. In
    # particular, the money adapter's 360.0 remains valid for an integer amount;
    # Python booleans and non-finite values never count as JSON numbers.
    numeric = type(value) in (int, float) and math.isfinite(value)
    valid = {"object": isinstance(value, dict), "array": isinstance(value, list),
             "string": isinstance(value, str), "boolean": type(value) is bool,
             "integer": numeric and (type(value) is int or value.is_integer()),
             "number": numeric}[kind]
    if not valid:
        raise InvalidInput(f"{where}: wrong value type")
    if "enum" in schema and value not in schema["enum"]:
        raise InvalidInput(f"{where}: value outside supplied enum")
    if kind == "object":
        props = schema.get("properties", {})
        if set(schema.get("required", [])) - set(value):
            raise InvalidInput(f"{where}: required field missing")
        if schema.get("additionalProperties") is False and set(value) - set(props):
            raise InvalidInput(f"{where}: unknown field")
        for key in value.keys() & props.keys():
            validate_value(value[key], props[key], f"{where}.{key}")
    if kind == "array":
        for item in value:
            validate_value(item, schema["items"], f"{where}[]")
    if kind in {"number", "integer"}:
        for keyword, invalid in (("minimum", lambda a, b: a < b), ("maximum", lambda a, b: a > b)):
            if keyword in schema and invalid(value, schema[keyword]):
                raise InvalidInput(f"{where}: numeric bound exceeded")
        for keyword, bound, invalid in (("exclusiveMinimum", "minimum", lambda a, b: a <= b),
                                         ("exclusiveMaximum", "maximum", lambda a, b: a >= b)):
            limit = schema.get(keyword)
            if limit is not None and limit is not False:
                limit = schema.get(bound) if limit is True else limit
                if limit is None or invalid(value, limit):
                    raise InvalidInput(f"{where}: exclusive numeric bound exceeded")
    if kind in {"string", "array"}:
        suffix = "Length" if kind == "string" else "Items"
        if len(value) < schema.get("min" + suffix, 0) or len(value) > schema.get("max" + suffix, math.inf):
            raise InvalidInput(f"{where}: length bound exceeded")
    if kind == "string":
        if "pattern" in schema and not re.search(schema["pattern"], value):
            raise InvalidInput(f"{where}: pattern mismatch")
        try:
            if schema.get("format") == "date":
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                    raise ValueError()
                date.fromisoformat(value)
            if schema.get("format") == "date-time":
                if "T" not in value:
                    raise ValueError()
                datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise InvalidInput(f"{where}: invalid date format") from exc


class Contracts:
    def __init__(self, spec):
        self.spec = deepcopy(spec)
        if not isinstance(spec, dict) or not str(spec.get("openapi", "")).startswith(("3.0.", "3.1.")):
            raise InvalidInput("Expected OpenAPI 3.0 or 3.1")
        servers = spec.get("servers", [])
        if len(servers) != 1:
            raise InvalidInput("This version requires exactly one explicit API server")
        self.base_url = servers[0].get("url", "").rstrip("/")
        parsed = urlsplit(self.base_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or "{" in self.base_url:
            raise InvalidInput("API server must be a literal HTTPS URL without credentials")
        if spec.get("security"):
            raise InvalidInput("Authenticated runtime bank APIs are unsupported")
        self.operations = {}
        for path, item in spec.get("paths", {}).items():
            if not path.startswith("/") or "{" in path or "?" in path or "#" in path or "/../" in path:
                raise InvalidInput("Unsupported API path")
            operation = item.get("post")
            if operation is None:
                raise InvalidInput("Only the seven POST bank tools are supported")
            if item.get("parameters") or item.get("servers") or operation.get("parameters") or operation.get("servers") or operation.get("security"):
                raise InvalidInput("Path/query parameters, server overrides or authentication need an explicit adapter")
            name = operation.get("operationId") or path.rsplit("/", 1)[-1]
            if name not in TOOL_NAMES or name in self.operations:
                raise InvalidInput("Unexpected or duplicate bank tool name")
            try:
                request = self.resolve(operation["requestBody"])["content"]["application/json"]["schema"]
            except (KeyError, TypeError) as exc:
                raise InvalidInput(f"{name}: JSON request schema missing") from exc
            check_schema(request, name)
            if request["type"] != "object":
                raise InvalidInput("Bank request must be an object")
            responses = self.resolve(operation.get("responses", {}))
            for response in responses.values():
                schema = response.get("content", {}).get("application/json", {}).get("schema")
                if schema is not None:
                    check_schema(schema, f"{name}.response")
            self.operations[name] = {"url": self.base_url + path, "request": request,
                                     "responses": responses, "description": operation.get("summary", name)}
        if set(self.operations) != TOOL_NAMES:
            raise InvalidInput("The API must supply all seven bank tools")

    def resolve(self, value, seen=()):
        if isinstance(value, list):
            return [self.resolve(v, seen) for v in value]
        if not isinstance(value, dict):
            return value
        if "$ref" in value:
            reference = value["$ref"]
            if len(value) != 1 or not isinstance(reference, str) or not reference.startswith("#/") or reference in seen:
                raise InvalidInput("Only nonrecursive local references without siblings are supported")
            target = self.spec
            try:
                for part in reference[2:].split("/"):
                    target = target[part.replace("~1", "/").replace("~0", "~")]
            except (KeyError, TypeError) as exc:
                raise InvalidInput("Unresolved local OpenAPI reference") from exc
            return self.resolve(target, seen + (reference,))
        return {k: self.resolve(v, seen) for k, v in value.items()}

    def validate_request(self, name, body):
        if name not in self.operations:
            raise InvalidInput("Unknown bank tool")
        validate_value(body, self.operations[name]["request"])

    def bind_request(self, name, roles, binding):
        """Map semantic roles to supplied field names; never guess renamed fields."""
        if set(roles) - set(binding) or len(set(binding.values())) != len(binding):
            raise InvalidInput("Incomplete or ambiguous field binding")
        properties = self.operations[name]["request"]["properties"]
        if not set(binding.values()) <= set(properties):
            raise InvalidInput("Field binding does not match the supplied API")
        body = {binding[role]: value for role, value in roles.items()}
        self.validate_request(name, body)
        return body

    def freya_tools(self):
        """Unbound tool templates, not a runnable agent. No practice fields baked in."""
        tools = []
        for name, operation in sorted(self.operations.items()):
            body = deepcopy(operation["request"])
            # OpenAPI defaults are hints, not authorization to bake live call arguments.
            def strip_defaults(value):
                if isinstance(value, dict):
                    value.pop("default", None)
                    for child in value.values():
                        strip_defaults(child)
                elif isinstance(value, list):
                    for child in value:
                        strip_defaults(child)
            strip_defaults(body)
            # 30 s: a slow Freya round trip (live 11 Oct, PUB-04/09/10) delivered bank answers after 12 s,
            # so the old limit discarded successful 200s and the call aborted. No retries: writes
            # must never repeat.
            tools.append({"type": "function", "config": {"maxRetries": 0, "timeoutMs": 30000},
                          "function": {"name": name, "handler": "__function__:api_call",
                                       "description": operation["description"], "parameters": {
                                           "type": "object", "properties": {
                                               "url": {"type": "string", "default": operation["url"]},
                                               "method": {"type": "string", "default": "POST"},
                                               "headers": {"type": "object", "properties": {
                                                   "Content-Type": {"type": "string", "default": "application/json"}}},
                                               "body": body}}}})
        return tools
