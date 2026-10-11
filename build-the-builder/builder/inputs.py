"""Read explicit UTF-8 inputs and retain verifiable source locations."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path


class InvalidInput(ValueError):
    """An input cannot be handled safely by this version of the core."""


def run_fingerprints(paths):
    """An in-memory snapshot; paths/contents are not copied into prompts or traces."""
    return {"inputs": {str(Path(path).resolve()): hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in paths},
            "builder": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in sorted(Path(__file__).parent.glob("*.py"))}}


def require_unchanged(snapshot):
    if run_fingerprints(snapshot["inputs"]) != snapshot:
        raise InvalidInput("Run inputs or builder source files changed during generation; start a new complete run")


def read_json(path):
    def unique(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise InvalidInput("Duplicate JSON property")
            out[key] = value
        return out
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"),
                          object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(InvalidInput("Non-finite JSON number")))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise InvalidInput("Expected a UTF-8 JSON file") from exc


@dataclass(frozen=True)
class Document:
    id: str
    name: str
    sha256: str
    text: str

    @classmethod
    def read(cls, path, document_id):
        path = Path(path)
        if path.suffix.lower() not in {".md", ".txt"}:
            raise InvalidInput("Briefs must be explicit .md or .txt files")
        raw = path.read_bytes()
        if not raw or len(raw) > 2_000_000:
            raise InvalidInput("Brief must contain between 1 and 2,000,000 bytes")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeError as exc:
            raise InvalidInput("Brief must use UTF-8") from exc
        if not text.strip():
            raise InvalidInput("Brief is empty")
        return cls(document_id, path.name, hashlib.sha256(raw).hexdigest(), text)

    def manifest(self):
        return {"id": self.id, "name": self.name, "sha256": self.sha256,
                "lines": len(self.text.splitlines())}


def read_documents(paths):
    resolved = [Path(p).resolve() for p in paths]
    if not resolved or len(set(resolved)) != len(resolved):
        raise InvalidInput("Supply at least one brief, with no duplicate paths")
    return [Document.read(p, f"document_{i + 1}") for i, p in enumerate(resolved)]


def validate_evidence(reference, documents):
    """A citation proves source membership, not that an extracted rule is correct."""
    required = {"document", "sha256", "start_line", "end_line", "quote"}
    if not isinstance(reference, dict) or set(reference) != required:
        raise InvalidInput("Evidence needs a document, hash, line range and exact quote")
    document = next((d for d in documents if d.id == reference["document"]), None)
    if document is None or document.sha256 != reference["sha256"]:
        raise InvalidInput("Evidence refers to a missing or changed document")
    start, end = reference["start_line"], reference["end_line"]
    if type(start) is not int or type(end) is not int or not 1 <= start <= end <= len(document.text.splitlines()):
        raise InvalidInput("Evidence line range is invalid")
    quote = reference["quote"]
    if not isinstance(quote, str) or not quote.strip() or quote not in "\n".join(document.text.splitlines()[start - 1:end]):
        raise InvalidInput("Evidence quote does not match its source lines")
