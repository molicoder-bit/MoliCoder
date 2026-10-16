"""CLI entry point for the same repository question service used by desktop Chat."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

from .inference.policy import InferencePolicy
from .inference.registry import ProviderRegistry
from .molicoder.projectstate import detect_project_kind
from .ollama import OllamaClient
from .repository_analysis import RepositoryAnalysis
from .storage import ChatStore


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='Ask a repository question with automatic evidence-checked model fallback.')
    parser.add_argument('question')
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--model', default='molicoder:latest')
    parser.add_argument('--database', type=Path, help='Use an isolated profile or existing app database')
    parser.add_argument('--report', type=Path)
    parser.add_argument('--local-only', action='store_true', help='Allow local model fallback only')
    parser.add_argument('--ollama-url', default='http://127.0.0.1:11434')
    args = parser.parse_args(argv)
    root = args.project.resolve()
    if not root.is_dir():
        parser.error('Project folder does not exist')
    store = ChatStore(args.database) if args.database else ChatStore()
    project = store.upsert_project(root.name, str(root), detect_project_kind(root))
    def progress(event):
        if event.get('phase') == 'started':
            print(f"Inspecting repository: {event['provider']} / {event['model']}", file=sys.stderr, flush=True)
        elif event.get('phase') == 'fallback':
            print(f"Trying another route: {event.get('error', 'unavailable')}", file=sys.stderr, flush=True)

    service = RepositoryAnalysis(store, project, OllamaClient(args.ollama_url), preferred_model=args.model,
        policy=InferencePolicy(ProviderRegistry(store)), allow_external=not args.local_only, on_route=progress)
    result = service.answer(args.question)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(asdict(result), indent=2), encoding='utf-8')
    print(result.content)
    return 0 if result.metrics.done_reason == 'repository_answer' else 2
