"""CLI client of the same request coordinator used by the desktop."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

from ..molicoder.projectstate import detect_project_kind
from ..ollama import OllamaClient
from ..storage import ChatStore
from .coordinator import RequestCoordinator


def main(argv=None):
    parser = argparse.ArgumentParser(description="Evaluate and execute a MoliCoder request with verified tools and fallback")
    parser.add_argument("request", nargs='?')
    parser.add_argument('--resume', metavar='REQUEST_ID', help='Reconcile and continue an interrupted request from this profile')
    parser.add_argument("--project", type=Path)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--model", default="molicoder:latest")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--local-only", action="store_true")
    parser.add_argument("--approve", action="store_true", help="Explicitly approve displayed bounded local operations in this invocation")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    store = ChatStore(args.database) if args.database else ChatStore()
    project = None
    if not args.request and not args.resume:
        parser.error('Provide a request or --resume REQUEST_ID')
    if args.resume:
        from .journal import RequestJournal
        saved = RequestJournal.load(store, args.resume)
        project = store.get_project(saved['project_id']) if saved['project_id'] else None
    if args.project:
        root = args.project.resolve()
        if not root.is_dir():
            parser.error("Project directory does not exist")
        project = store.upsert_project(root.name, str(root), detect_project_kind(root))
    def review(proposal):
        print("Local operation review:\n" + json.dumps(proposal, indent=2), file=sys.stderr, flush=True)
        return args.approve
    def progress(event):
        if event["phase"] in {"started", "fallback", "tool", "awaiting_review"}:
            print(json.dumps(event), file=sys.stderr, flush=True)
    result = RequestCoordinator(store, project, OllamaClient(args.ollama_url), preferred_model=args.model,
        allow_external=not args.local_only, on_event=progress, review=review, resume_from=args.resume).run(args.request or '')
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(asdict(result), indent=2), encoding="utf-8")
    print(result.content)
    return 0 if result.receipt["outcome"] == "completed" else 2
