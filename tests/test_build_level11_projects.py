"""Approved Build changes checked by independent application tests in Git fixtures."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from local_model_chat.molicoder.planner.schema import parse_plan
from local_model_chat.molicoder.providers import ProviderResult
from test_build_plan_approval import approve, fixture, git


def _unittest(path: Path) -> str:
    result = subprocess.run(
        [sys.executable, "-B", "-m", "unittest", "-q", "test_acceptance"],
        cwd=path, capture_output=True, text=True, timeout=15,
    )
    return "validation passed" if result.returncode == 0 else "validation failed: " + result.stderr


def _node(path: Path) -> str:
    result = subprocess.run(
        ["node", "test_frontend.js"], cwd=path,
        capture_output=True, text=True, timeout=15,
    )
    return "validation passed" if result.returncode == 0 else "validation failed: " + result.stderr


def _both(path: Path) -> str:
    first = _unittest(path)
    return first if not first.startswith("validation passed") else _node(path)


def _case(tmp_path, *, seed: dict[str, str], writes: dict[str, str], validate, request: str):
    root, store, _project, _old_provider, orchestrator = fixture(tmp_path)
    for name, content in seed.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "application and independent acceptance tests")

    class Planner:
        provider = "scripted"

        def plan(self, _packet):
            return parse_plan({"objective": request, "tasks": [{
                "key": "feature", "title": "Implement requested feature",
                "objective": request, "risk": "medium", "depends_on": [],
                "expected_paths": list(writes),
                "acceptance_criteria": ["Configured validation must pass"],
                "verification_checks": ["project-default"],
            }]})

    class Executor:
        name = "scripted"
        model = "fixture-model"

        def __init__(self):
            self.calls = 0

        def execute(self, worktree, _prompt, **_kwargs):
            self.calls += 1
            for name, content in writes.items():
                target = Path(worktree) / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            return ProviderResult("completed", "implemented", seconds=0.01)

        def cancel(self):
            pass

    executor = Executor()
    orchestrator.planner = Planner()
    orchestrator.provider = executor
    orchestrator.validate = validate
    orchestrator.validate_task = lambda path, _task: validate(path)
    orchestrator.max_task_attempts = 1
    prepared = orchestrator.run(request, mode="FRONTIER_ONLY")
    assert prepared.status == "awaiting_approval" and executor.calls == 0
    assert validate(root).startswith("validation failed")
    approve(store, prepared.run_id)
    result = orchestrator.execute_approved(prepared.run_id)
    assert result.status == "ready_to_apply", result.error
    assert validate(result.integration_path) == "validation passed"
    assert executor.calls == 1
    assert store.list_run_tasks(prepared.run_id)[0].status == "completed"
    assert store.get_task_completion(store.list_run_tasks(prepared.run_id)[0].id) is not None
    assert validate(root).startswith("validation failed")
    return root, result.integration_path


def test_s111_add_small_feature_to_existing_application(tmp_path):
    seed = {
        "app.py": "def greet(name):\n    return f'Hello, {name}!'\n",
        "test_acceptance.py": (
            "import unittest\nfrom app import greet\n"
            "class GreetingTests(unittest.TestCase):\n"
            "    def test_old_behavior(self):\n        self.assertEqual(greet('Ada'), 'Hello, Ada!')\n"
            "    def test_new_feature(self):\n        self.assertEqual(greet('Ada', excited=True), 'HELLO, ADA!')\n"
        ),
    }
    writes = {"app.py": (
        "def greet(name, excited=False):\n"
        "    text = f'Hello, {name}!'\n"
        "    return text.upper() if excited else text\n"
    )}
    _case(tmp_path, seed=seed, writes=writes, validate=_unittest,
          request="Add an excited greeting without changing the default greeting")


def test_s112_api_endpoint_validates_inputs_and_preserves_health_route(tmp_path):
    seed = {
        "api.py": "def application(environ, start_response):\n    start_response('200 OK', [])\n    return [b'healthy']\n",
        "test_acceptance.py": (
            "import io, json, unittest\nfrom api import application\n"
            "def call(path, body=b'', method='GET'):\n"
            "    statuses=[]\n"
            "    data=b''.join(application({'PATH_INFO':path,'REQUEST_METHOD':method,"
            "'CONTENT_LENGTH':str(len(body)),'wsgi.input':io.BytesIO(body)},"
            "lambda status,headers: statuses.append(status)))\n"
            "    return statuses[0], data\n"
            "class ApiTests(unittest.TestCase):\n"
            "    def test_health(self):\n        self.assertEqual(call('/health'), ('200 OK',b'healthy'))\n"
            "    def test_valid_sum(self):\n"
            "        self.assertEqual(call('/sum',b'{\"a\":2,\"b\":3}','POST'),('200 OK',b'{\"sum\": 5}'))\n"
            "    def test_bad_input(self):\n"
            "        self.assertEqual(call('/sum',b'{\"a\":\"x\",\"b\":3}','POST')[0],'400 Bad Request')\n"
            "    def test_wrong_method(self):\n"
            "        self.assertEqual(call('/sum')[0],'405 Method Not Allowed')\n"
        ),
    }
    writes = {"api.py": (
        "import json\n"
        "def application(environ, start_response):\n"
        "    path=environ.get('PATH_INFO','')\n"
        "    if path == '/health':\n"
        "        start_response('200 OK',[])\n        return [b'healthy']\n"
        "    if path != '/sum':\n"
        "        start_response('404 Not Found',[])\n        return [b'']\n"
        "    if environ.get('REQUEST_METHOD') != 'POST':\n"
        "        start_response('405 Method Not Allowed',[])\n        return [b'']\n"
        "    try:\n"
        "        length=int(environ.get('CONTENT_LENGTH','0'))\n"
        "        data=json.loads(environ['wsgi.input'].read(length))\n"
        "        a,b=data['a'],data['b']\n"
        "        if type(a) is not int or type(b) is not int: raise ValueError()\n"
        "    except (ValueError,KeyError,TypeError):\n"
        "        start_response('400 Bad Request',[])\n        return [b'']\n"
        "    start_response('200 OK',[])\n"
        "    return [json.dumps({'sum':a+b}).encode()]\n"
    )}
    _case(tmp_path, seed=seed, writes=writes, validate=_unittest,
          request="Add POST /sum with strict integer input validation")


_FRONTEND_TEST = (
    "const assert=require('node:assert/strict');\n"
    "const fs=require('node:fs');\n"
    "const vm=require('node:vm');\n"
    "const html=fs.readFileSync('index.html','utf8');\n"
    "assert.match(html,/<form[^>]+id=\"sum-form\"/);\n"
    "assert.match(html,/id=\"a\"/); assert.match(html,/id=\"b\"/);\n"
    "const nodes={a:{value:'2'},b:{value:'3'},result:{textContent:''},'sum-form':{addEventListener(_name,fn){this.submit=fn;}}};\n"
    "let requested; const document={getElementById:id=>nodes[id]};\n"
    "async function fetch(url,options){requested={url,options};return {json:async()=>({sum:5})};}\n"
    "vm.runInNewContext(fs.readFileSync('form.js','utf8'),{document,fetch,JSON,Number});\n"
    "assert.equal(typeof nodes['sum-form'].submit,'function');\n"
    "(async()=>{let prevented=false;await nodes['sum-form'].submit({preventDefault(){prevented=true;}});\n"
    "assert.equal(prevented,true);assert.equal(requested.url,'/sum');\n"
    "assert.equal(requested.options.method,'POST');\n"
    "assert.deepEqual(JSON.parse(requested.options.body),{a:2,b:3});\n"
    "assert.equal(nodes.result.textContent,'5');})().catch(e=>{console.error(e);process.exitCode=1;});\n"
)


_FRONTEND_HTML = (
    '<form id="sum-form"><input id="a" type="number"><input id="b" type="number">'
    '<button type="submit">Add</button></form><output id="result"></output>'
    '<script src="form.js"></script>\n'
)


_FRONTEND_JS = (
    "document.getElementById('sum-form').addEventListener('submit', async (event) => {\n"
    "  event.preventDefault();\n"
    "  const a = Number(document.getElementById('a').value);\n"
    "  const b = Number(document.getElementById('b').value);\n"
    "  const response = await fetch('/sum', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({a,b})});\n"
    "  const result = await response.json();\n"
    "  document.getElementById('result').textContent = String(result.sum);\n"
    "});\n"
)


def test_s113_frontend_form_calls_existing_api_and_displays_result(tmp_path):
    seed = {
        "api.py": "def sum_values(a,b):\n    return a+b\n",
        "test_frontend.js": _FRONTEND_TEST,
    }
    _case(tmp_path, seed=seed,
          writes={"index.html": _FRONTEND_HTML, "form.js": _FRONTEND_JS},
          validate=_node, request="Add a browser form that calls the existing sum API")


def test_s114_database_migration_and_application_query_agree(tmp_path):
    seed = {
        "migrations/001_init.sql": "CREATE TABLE contacts(id INTEGER PRIMARY KEY,name TEXT NOT NULL);\n",
        "app.py": "def list_contacts(db):\n    return db.execute('SELECT name FROM contacts ORDER BY id').fetchall()\n",
        "test_acceptance.py": (
            "import sqlite3, unittest\nfrom pathlib import Path\nfrom app import active_contacts\n"
            "class MigrationTests(unittest.TestCase):\n"
            "    def test_migration_and_active_query(self):\n"
            "        db=sqlite3.connect(':memory:')\n"
            "        for name in ('001_init.sql','002_add_status.sql'):\n"
            "            db.executescript((Path('migrations')/name).read_text())\n"
            "        db.execute(\"INSERT INTO contacts(name) VALUES ('Ada')\")\n"
            "        db.execute(\"INSERT INTO contacts(name,status) VALUES ('Bob','inactive')\")\n"
            "        self.assertEqual(active_contacts(db), ['Ada'])\n"
        ),
    }
    writes = {
        "migrations/002_add_status.sql": "ALTER TABLE contacts ADD COLUMN status TEXT NOT NULL DEFAULT 'active';\n",
        "app.py": (
            "def list_contacts(db):\n"
            "    return db.execute('SELECT name FROM contacts ORDER BY id').fetchall()\n"
            "def active_contacts(db):\n"
            "    return [row[0] for row in db.execute(\"SELECT name FROM contacts WHERE status='active' ORDER BY id\")]\n"
        ),
    }
    _case(tmp_path, seed=seed, writes=writes, validate=_unittest,
          request="Add contact status migration and query only active contacts")


def test_s115_refactor_preserves_public_pricing_behavior(tmp_path):
    seed = {
        "pricing.py": (
            "def total(price, quantity, discount=0):\n"
            "    if quantity < 0: raise ValueError('quantity')\n"
            "    if discount < 0 or discount > 1: raise ValueError('discount')\n"
            "    if quantity == 0: return 0\n"
            "    return round(price * quantity * (1-discount), 2)\n"
        ),
        "test_acceptance.py": (
            "import unittest\nfrom pricing import total\n"
            "class PricingTests(unittest.TestCase):\n"
            "    def test_regular(self):\n        self.assertEqual(total(10,3),30)\n"
            "    def test_discount(self):\n        self.assertEqual(total(10,3,.2),24)\n"
            "    def test_zero(self):\n        self.assertEqual(total(10,0),0)\n"
            "    def test_bad_quantity(self):\n"
            "        with self.assertRaises(ValueError): total(10,-1)\n"
            "    def test_bad_discount(self):\n"
            "        with self.assertRaises(ValueError): total(10,1,2)\n"
        ),
    }
    writes = {"pricing.py": (
        "def _checked_inputs(quantity, discount):\n"
        "    if quantity < 0: raise ValueError('quantity')\n"
        "    if not 0 <= discount <= 1: raise ValueError('discount')\n"
        "def total(price, quantity, discount=0):\n"
        "    _checked_inputs(quantity, discount)\n"
        "    return round(price * quantity * (1-discount), 2)\n"
    )}
    root, integrated = _case(tmp_path, seed=seed, writes=writes,
                             validate=lambda path: "validation passed" if _unittest(path) == "validation passed"
                             and "_checked_inputs" in (Path(path) / "pricing.py").read_text(encoding="utf-8")
                             else "validation failed", request="Refactor pricing without changing public behavior")
    assert (root / "pricing.py").read_text(encoding="utf-8") != (integrated / "pricing.py").read_text(encoding="utf-8")


def test_s116_full_stack_sum_feature_passes_backend_and_browser_contracts(tmp_path):
    seed = {
        "api.py": "def sum_values(a,b):\n    return a+b\n",
        "test_acceptance.py": (
            "import unittest\nfrom api import sum_values, handle_sum\n"
            "class FullStackTests(unittest.TestCase):\n"
            "    def test_old_function(self):\n        self.assertEqual(sum_values(2,3),5)\n"
            "    def test_endpoint(self):\n        self.assertEqual(handle_sum({'a':2,'b':3}),{'sum':5})\n"
            "    def test_bad_input(self):\n"
            "        with self.assertRaises(ValueError): handle_sum({'a':'x','b':3})\n"
        ),
        "test_frontend.js": _FRONTEND_TEST,
    }
    writes = {
        "api.py": (
            "def sum_values(a,b):\n    return a+b\n"
            "def handle_sum(data):\n"
            "    a,b=data['a'],data['b']\n"
            "    if type(a) is not int or type(b) is not int: raise ValueError('integer inputs required')\n"
            "    return {'sum':sum_values(a,b)}\n"
        ),
        "index.html": _FRONTEND_HTML,
        "form.js": _FRONTEND_JS,
    }
    _case(tmp_path, seed=seed, writes=writes, validate=_both,
          request="Add validated sum endpoint and working browser form")


def test_s117_cross_module_timezone_bug_requires_feedback_guided_repair(tmp_path):
    root, store, _project, _old_provider, orchestrator = fixture(tmp_path)
    seed = {
        "parser.py": (
            "from datetime import datetime, timezone\n"
            "def parse_timestamp(value):\n"
            "    return datetime.fromisoformat(value[:19]).replace(tzinfo=timezone.utc)\n"
        ),
        "service.py": (
            "from datetime import timezone\nfrom parser import parse_timestamp\n"
            "def utc_day(value):\n"
            "    return parse_timestamp(value).astimezone(timezone.utc).date().isoformat()\n"
        ),
        "test_acceptance.py": (
            "import unittest\nfrom service import utc_day\n"
            "class TimezoneTests(unittest.TestCase):\n"
            "    def test_positive_offset_crosses_previous_day(self):\n"
            "        self.assertEqual(utc_day('2024-01-01T00:30:00+02:00'),'2023-12-31')\n"
            "    def test_negative_offset_crosses_next_day(self):\n"
            "        self.assertEqual(utc_day('2024-01-01T23:30:00-02:00'),'2024-01-02')\n"
            "    def test_zulu_time(self):\n"
            "        self.assertEqual(utc_day('2024-01-01T12:00:00Z'),'2024-01-01')\n"
        ),
    }
    for name, content in seed.items():
        (root / name).write_text(content, encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "timezone bug and regression tests")
    assert _unittest(root).startswith("validation failed")

    class Planner:
        provider = "scripted"

        def plan(self, _packet):
            return parse_plan({"objective": "Fix UTC day conversion", "tasks": [{
                "key": "fix", "title": "Fix timezone conversion",
                "objective": "Preserve ISO 8601 timezone offsets across parser and service",
                "risk": "medium", "depends_on": [],
                "expected_paths": ["parser.py", "service.py"],
            }]})

    class RepairExecutor:
        name = "scripted"
        model = "fixture-model"

        def __init__(self):
            self.calls = 0

        def execute(self, worktree, prompt, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                (Path(worktree) / "service.py").write_text(
                    "from parser import parse_timestamp\n"
                    "def utc_day(value):\n"
                    "    return parse_timestamp(value).date().isoformat()\n", encoding="utf-8",
                )
            else:
                assert "validation failed" in prompt
                (Path(worktree) / "parser.py").write_text(
                    "from datetime import datetime\n"
                    "def parse_timestamp(value):\n"
                    "    return datetime.fromisoformat(value.replace('Z','+00:00'))\n",
                    encoding="utf-8",
                )
                (Path(worktree) / "service.py").write_text(
                    "from datetime import timezone\nfrom parser import parse_timestamp\n"
                    "def utc_day(value):\n"
                    "    return parse_timestamp(value).astimezone(timezone.utc).date().isoformat()\n",
                    encoding="utf-8",
                )
            return ProviderResult("completed", "attempt made", seconds=0.01)

        def cancel(self):
            pass

    executor = RepairExecutor()
    orchestrator.planner = Planner()
    orchestrator.provider = executor
    orchestrator.validate = _unittest
    orchestrator.validate_task = lambda path, _task: _unittest(path)
    orchestrator.max_task_attempts = 2
    prepared = orchestrator.run("Fix UTC day conversion", mode="FRONTIER_ONLY")
    approve(store, prepared.run_id)
    result = orchestrator.execute_approved(prepared.run_id)
    assert result.status == "ready_to_apply", result.error
    assert executor.calls == 2 and _unittest(result.integration_path) == "validation passed"
    assert _unittest(root).startswith("validation failed")
    assert any(event.kind == "repair_started" for event in store.list_events(prepared.run_id))
    assert store.list_run_tasks(prepared.run_id)[0].attempts == 2


def test_s118_conflicting_requirement_during_execution_stops_unapproved_successor(tmp_path):
    root, store, _project, _old_provider, orchestrator = fixture(tmp_path)

    class Planner:
        provider = "scripted"

        def plan(self, _packet):
            return parse_plan({"objective": "Create two positive outputs", "tasks": [
                {"key": "first", "title": "First", "objective": "Write positive first.txt",
                 "risk": "low", "depends_on": [], "expected_paths": ["first.txt"]},
                {"key": "second", "title": "Second", "objective": "Write positive second.txt",
                 "risk": "low", "depends_on": ["first"], "expected_paths": ["second.txt"]},
            ]})

    class Executor:
        name = "scripted"
        model = "fixture-model"

        def __init__(self):
            self.calls = []

        def execute(self, worktree, prompt, **_kwargs):
            key = "first" if "(first)" in prompt else "second"
            self.calls.append(key)
            (Path(worktree) / f"{key}.txt").write_text(f"positive {key}\n", encoding="utf-8")
            if key == "first":
                current = store.get_latest_plan_record(orchestrator._active_run_id)
                conflicting = dict(current["payload"])
                conflicting["tasks"] = [dict(task) for task in current["payload"]["tasks"]]
                conflicting["tasks"][1]["objective"] = "Delete second.txt instead"
                try:
                    orchestrator.revise_plan(orchestrator._active_run_id, current["id"], {
                        "objective": conflicting["objective"], "tasks": conflicting["tasks"],
                    })
                except ValueError as exc:
                    assert "pending plan" in str(exc)
                else:
                    raise AssertionError("running plan was edited through the review API")
                store.save_plan(orchestrator._active_run_id, "external_conflict", "scripted", conflicting)
            return ProviderResult("completed", "done", seconds=0.01)

        def cancel(self):
            pass

    executor = Executor()
    orchestrator.planner = Planner()
    orchestrator.provider = executor
    orchestrator.validate_task = lambda path, task: (
        "validation passed" if (Path(path) / f"{task.key}.txt").read_text(encoding="utf-8") == f"positive {task.key}\n"
        else "validation failed"
    )
    orchestrator.validate = lambda path: (
        "validation passed" if all((Path(path) / f"{key}.txt").is_file() for key in ("first", "second"))
        else "validation failed"
    )
    prepared = orchestrator.run("Create two positive outputs", mode="FRONTIER_ONLY")
    approve(store, prepared.run_id)
    result = orchestrator.execute_approved(prepared.run_id)
    assert result.status == "needs_attention" and "Plan approval changed" in result.error
    assert executor.calls == ["first"]
    assert store.list_run_tasks(prepared.run_id)[0].status == "completed"
    assert store.list_run_tasks(prepared.run_id)[1].status != "completed"
    assert (result.integration_path / "first.txt").read_text(encoding="utf-8") == "positive first\n"
    assert not (root / "first.txt").exists() and not (root / "second.txt").exists()
