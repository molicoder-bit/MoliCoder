"""Host-issued evidence and guarded project tools shared by request runtimes."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
from pathlib import Path

from ..directory_queries import list_directory
from ..molicoder.tools import ToolCall, ToolDispatcher
from ..repository_analysis import RepositoryIndex


class ToolSession:
    WRITES = {"write_file", "replace_in_file", "create_directory", "run_check", "run_command"}

    def __init__(self, root, journal, *, cancelled, review, emit):
        self.root = Path(root).resolve()
        self.journal, self.cancelled, self.review, self.emit = journal, cancelled, review, emit
        self.dispatcher = ToolDispatcher(self.root, validation=lambda: "Use an approved named check")
        self.index = RepositoryIndex(self.root, cancelled)
        self.evidence = {}
        self.approved_paths = set()
        self.snapshots = {}
        self.writes = {}
        self.calls = {}

    def path(self, value):
        if not isinstance(value, str) or not value:
            raise ValueError("A path is required")
        # Natural references are resolved by evaluation; typed tool paths are literal.
        literal = Path(value)
        literal = literal if literal.is_absolute() else self.root / literal
        # Inspect the spelling before resolve() erases a link or junction component.
        import os
        literal = Path(os.path.abspath(literal))
        try:
            parts = literal.relative_to(self.root).parts
        except ValueError:
            raise PermissionError('Path is outside the project scope') from None
        cursor = self.root
        for part in parts:
            cursor /= part
            if cursor.is_symlink() or cursor.is_junction():
                raise PermissionError("Link/junction operations require an explicit independent scope")
        return self.dispatcher._resolve(value)

    @staticmethod
    def fingerprint(path):
        if not path.is_file():
            return None
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        return digest.hexdigest()

    def restore(self, events):
        """Reconcile completed mutations against current disk; never replay a process."""
        started = {e['payload']['call']['call_id']: e['payload']['call'] for e in events if e['kind'] == 'tool_started'}
        finished = set()
        for event in events:
            if event['kind'] != 'tool_completed':
                continue
            payload = event['payload']
            call = payload.get('call')
            if call:
                finished.add(call['call_id'])
            name = payload.get('tool')
            if name not in self.WRITES:
                continue  # Read evidence is regenerated, since sources can change while closed.
            if name in {'run_command', 'run_check'}:
                raise PermissionError('An earlier process ran. Review its recorded outcome before starting a new request; it will not be repeated automatically.')
            target = self.path(payload['arguments']['path'])
            digest = payload.get('file_sha256')
            if name == 'create_directory':
                if not target.is_dir():
                    raise PermissionError('A previously created directory changed since interruption')
            elif not target.is_file() or self.fingerprint(target) != digest:
                raise PermissionError('A previously written file changed since interruption; review the preserved request before continuing')
            self.writes[str(target)] = digest
            evidence_id = self.journal.event('tool_completed', dict(payload, restored_from=event['id']))
            self.evidence[evidence_id] = payload
            if call:
                self.calls[call['call_id']] = (call, {'evidence_id': evidence_id, **payload})
        if any(call['name'] in self.WRITES and key not in finished for key, call in started.items()):
            raise PermissionError('A local operation was interrupted without a recorded result. Inspect its actual effects before retrying; it will not be repeated automatically.')

    def source_evidence(self):
        def has_text(payload):
            values = payload['result'] if isinstance(payload['result'], list) else [payload['result']]
            return any(isinstance(v, dict) and v.get('text') for v in values)
        return {key: payload for key, payload in self.evidence.items()
            if payload['tool'] in {'search_code', 'read_file'} and has_text(payload)}

    def verify_citations(self, citations, message):
        from ..repository_analysis import Source
        import re
        fresh = RepositoryIndex(self.root, self.cancelled); fresh.load()
        verified = []
        for citation in citations:
            payload = self.source_evidence().get(citation['evidence_id'])
            if payload is None:
                raise ValueError('Citation needs actual source evidence')
            values = payload['result'] if isinstance(payload['result'], list) else [payload['result']]
            sources = [Source(**{k: v[k] for k in ('path', 'start', 'end', 'text', 'sha256', 'scope')}) for v in values]
            if not any(s.path == citation['path'] and fresh.digests.get(s.path) == s.sha256 for s in sources):
                raise ValueError('Cited source changed since inspection')
            # A literal data file can contain a useful value shorter than the
            # repository analysis service's five-character code-quote floor.
            quote, line, path = citation['quote'], citation['line'], citation['path']
            if (0 < len(quote.strip()) < 5 and Path(path).suffix.lower() in {'.txt', '.json', '.csv', '.md', '.ini'}
                    and path in fresh.files and 1 <= line <= len(fresh.files[path])
                    and fresh.files[path][line - 1].strip() == quote.strip()
                    and any(s.path == path and s.start <= line <= s.end for s in sources)):
                verified.append({'path': path, 'line': line, 'quote': quote, 'sha256': fresh.digests[path]})
                continue
            verified.extend(fresh.validate_citations([citation], sources, re.findall(r'`([A-Za-z_]\w*)`', message)))
        if not verified:
            raise ValueError('Repository explanation needs an actual source quote')
        return verified

    def _source_metadata(self, value):
        values = value if isinstance(value, list) else [value]
        for source in values:
            if not isinstance(source, dict) or not {'path', 'start', 'end', 'sha256'} <= source.keys():
                continue
            path = self.path(source['path'])
            if path.stat().st_size > 1_000_000:
                continue
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != source['sha256']:
                raise ValueError('Source changed during inspection')
            source.update(raw_excerpt=b''.join(raw.splitlines(keepends=True)[source['start']-1:source['end']]).decode('utf-8')[:10000],
                newline_style='CRLF' if b'\r\n' in raw else 'LF' if b'\n' in raw else 'none',
                ends_with_newline=raw.endswith((b'\r\n', b'\n', b'\r')))
        return value

    def dispatch(self, call, spec):
        if self.cancelled():
            raise InterruptedError("Request cancelled")
        name, args, call_id = call["name"], call["arguments"], call["call_id"]
        if call_id in self.calls:
            old_call, result = self.calls[call_id]
            if old_call != call:
                raise ValueError("A tool call ID cannot be reused with different arguments")
            return result
        target = self.path(args["path"]) if "path" in args else None
        if name in self.WRITES:
            if spec["intent"] != "action":
                raise PermissionError("This request is read-only; development belongs in the approved Build workflow")
            checks = spec["acceptance_checks"]
            if name not in {"run_check", "run_command"}:
                if any(part in {'.git', '.molicoder', '.venv'} for part in target.relative_to(self.root).parts):
                    raise PermissionError('Internal project state is outside the local action scope')
                if self.journal.project_id is not None:
                    from ..molicoder.validate import assess_architectural_invariants
                    assessment = assess_architectural_invariants(self.journal.store, self.journal.project_id,
                        (str(target.relative_to(self.root)).replace('\\', '/'),))
                    if assessment.conflicts or assessment.review_required:
                        raise PermissionError('The operation requires review of an approved project decision')
                permitted = {self.path(c["target"]) for c in checks if c["kind"] in {
                    "file_equals", "file_contains", "file_exists", "directory_exists"}}
                if target not in permitted:
                    raise PermissionError("Write has no corresponding acceptance criterion")
                if name == "write_file":
                    expected = [c["expected"] for c in checks if c["kind"] == "file_equals" and self.path(c["target"]) == target]
                    if expected and any(args["content"] != value for value in expected):
                        raise ValueError("Proposed contents differ from the evaluated acceptance criteria")
            import json
            key = str(target) if target else "command:" + json.dumps(args, sort_keys=True)
            before = self.fingerprint(target) if target else None
            if key not in self.approved_paths:
                self.emit("awaiting_review", {"message": "Review the proposed local operation"})
                if not self.review({"objective": spec["objective"], "call": call, "checks": checks,
                                    "project_root": str(self.root), "before_sha256": before}):
                    raise PermissionError("Operation was not approved")
                self.approved_paths.add(key)
                self.snapshots[key] = before
                self.journal.event("action_approved", {"call": call, "before_sha256": before})
            if target and self.fingerprint(target) != self.snapshots[key]:
                raise PermissionError("File changed since review; review the new operation")
        self.emit("tool", {"message": name, "arguments": args})
        self.journal.event("tool_started", {"call": call})
        if name == "list_directory":
            listing = list_directory(target, cancelled=self.cancelled)
            if listing.outcome != "completed":
                raise FileNotFoundError(listing.content)
            result = asdict(listing)
        elif name == "read_file":
            if args["end"] < args["start"]:
                raise ValueError("Read line ranges in increasing order")
            self.index = RepositoryIndex(self.root, self.cancelled)
            self.index.load()
            if target.is_file() and target.stat().st_size == 0:
                result = {'path': target.relative_to(self.root).as_posix(), 'start': 1, 'end': 0,
                    'text': '', 'sha256': self.fingerprint(target), 'scope': '', 'raw_excerpt': '', 'bytes': 0}
            else:
                result = self._source_metadata(self.index.read(str(target.relative_to(self.root)), args["start"], min(args["end"], args["start"] + 159)))
        elif name == "search_code":
            self.index.load()
            result = self._source_metadata(self.index.search(args["query"], 5))
        elif name == "project_status":
            import subprocess
            process = subprocess.run(["git", "status", "--short"], cwd=self.root, capture_output=True,
                text=True, timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            result = {"project_root": str(self.root), "git": process.stdout[:10000],
                      "exit_code": process.returncode, "error": process.stderr[:2000]}
        elif name == "create_directory":
            target.mkdir(exist_ok=False)
            result = {"path": str(target), "exists": target.is_dir()}
            self.writes[str(target)] = None
        elif name in {"write_file", "replace_in_file"}:
            if name == "write_file":
                if target.exists() and str(target) not in self.writes:
                    raise PermissionError("Use replace_in_file for an existing file; its old text is an exact precondition")
                data = args["content"].encode("utf-8")
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as handle:
                    handle.write(data)
                if target.read_bytes() != data:
                    raise ValueError("File bytes differ from the requested write")
            else:
                self.dispatcher.dispatch(ToolCall(name, args))
            digest = self.fingerprint(target)
            self.writes[str(target)] = digest
            self.snapshots[str(target)] = digest
            result = {"path": str(target), "sha256": digest, "bytes": target.stat().st_size}
        elif name == "run_command":
            from ..molicoder.validate import ProjectValidator, ValidationCommand
            argv = args['argv']
            if any('\x00' in part for part in argv) or not argv[0].strip():
                raise ValueError('Invalid process arguments')
            validator = ProjectValidator(self.root, [ValidationCommand('requested-command', tuple(argv), 30)],
                should_cancel=self.cancelled)
            code, output = validator._run_command(validator.commands[0])
            result = {'argv': argv, 'cwd': str(self.root), 'exit_code': code, 'output': output[:16000]}
            self.journal.event('command_result', result)
            if code:
                # Commands may have partly changed the project. Never repeat one via
                # model fallback without reconciling those effects first.
                raise PermissionError('Command failed; inspect its recorded outcome before retrying: ' + str(code) + '\n' + output[:4000])
        elif name == "run_check":
            from ..molicoder.runtime import build_validator
            validator = build_validator(self.root)
            commands = [c for c in validator.commands if c.name == args["check_id"]]
            if len(commands) != 1:
                raise ValueError("Unknown project check; select one from the project validation configuration")
            from ..molicoder.validate import ProjectValidator
            report = ProjectValidator(self.root, commands, should_cancel=self.cancelled).run()
            result = {"passed": report.passed, "summary": report.summary()}
            if not report.passed:
                raise ValueError("Project check failed: " + report.summary())
        else:
            raise ValueError("Unsupported tool")
        if self.cancelled():
            raise InterruptedError("Cancelled; operation outcome preserved in tool journal")
        payload = {"tool": name, "arguments": args, "result": result, "call": call}
        if target and target.is_file():
            payload["file_sha256"] = self.fingerprint(target)
        evidence_id = self.journal.event("tool_completed", payload)
        self.evidence[evidence_id] = payload
        output = {"evidence_id": evidence_id, **payload}
        self.calls[call_id] = (call, output)
        return output

    def verify(self, spec, refs):
        if not set(refs) <= self.evidence.keys():
            raise ValueError("Result refers to evidence that the host did not issue")
        if spec["intent"] != "conversation" and not refs:
            raise ValueError("Project answers and actions require actual tool evidence")
        for evidence in self.evidence.values():
            if "file_sha256" in evidence:
                path = self.path(evidence["arguments"]["path"])
                expected = self.writes.get(str(path), evidence["file_sha256"])
                if self.fingerprint(path) != expected:
                    raise ValueError("Source file changed after inspection")
        results = []
        for check in spec["acceptance_checks"]:
            kind = check["kind"]
            if kind == "semantic_review":
                if spec["intent"] == "action":
                    raise ValueError("Semantic development checks require the Build workflow")
                results.append(dict(check, status="review_required"))
                continue
            if kind == "tool_evidence":
                passed = bool(refs) and any(self.evidence[ref]["tool"] == check["expected"] for ref in refs)
            else:
                path = self.path(check["target"])
                passed = path.is_dir() if kind == "directory_exists" else path.is_file()
                if passed and kind in {"file_equals", "file_contains"}:
                    actual = path.read_bytes()
                    expected = check["expected"].encode("utf-8")
                    passed = actual == expected if kind == "file_equals" else expected in actual
            results.append(dict(check, status="passed" if passed else "failed"))
            if not passed:
                raise ValueError("Acceptance check failed: " + check["id"])
        if spec["intent"] == "action" and not spec["acceptance_checks"]:
            raise ValueError("An action requires explicit acceptance checks")
        return results
