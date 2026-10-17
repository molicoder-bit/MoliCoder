import json
import pytest

from local_model_chat.orchestration.coordinator import RequestCoordinator
from local_model_chat.storage import ChatStore
from test_request_orchestration import Client, evaluation, service, turn, request_payload


@pytest.mark.parametrize('selected', [False, True])
def test_project_identity_is_answered_from_app_state_without_inference(tmp_path, selected):
    store = ChatStore(tmp_path / 'profile.db')
    project = store.upsert_project('Harbor Notes', str(tmp_path), 'python') if selected else None
    client = Client([])
    result = RequestCoordinator(store, project, client).run('what project are we on')
    assert result.receipt['outcome'] == 'completed'
    assert ('Harbor Notes' if selected else 'General chat') in result.content
    assert client.calls == []


def test_clarification_asks_a_human_question_instead_of_rendering_a_field_name(tmp_path):
    spec = evaluation('clarification', checks=[])
    spec.update(objective='Create a note file', missing_information=['filename'])
    coordinator, client, root = service(tmp_path, [spec,
        turn('needs_clarification', message='What should the note file be called?', unmet_criteria=['filename'])])
    result = coordinator.run('Create a note file')
    assert result.receipt['outcome'] == 'awaiting_input'
    assert result.content == 'What should the note file be called?'
    assert len(client.calls) == 2
    assert result.receipt['attempts'][0]['status'] == 'awaiting_input'
    assert not list(root.iterdir())


def test_bare_clarification_label_is_rejected_before_fallback(tmp_path):
    spec = evaluation('clarification', checks=[])
    spec.update(objective='Create a note file', missing_information=['filename'])
    coordinator, client, root = service(tmp_path, [spec,
        turn('needs_clarification', message='filename'),
        turn('needs_clarification', message='What should the note file be called?')])
    result = coordinator.run('Create a note file')
    assert result.receipt['outcome'] == 'awaiting_input'
    assert result.content != 'filename'
    assert [a['status'] for a in result.receipt['attempts']] == ['failed', 'awaiting_input']
    assert client.calls[-1][0] != client.calls[0][0]


def test_bad_observation_criteria_are_corrected_before_answering(tmp_path):
    guessed = evaluation('investigation', checks=[{'id': 'purpose', 'kind': 'file_contains',
        'target': 'README.md', 'expected': 'This project does a guessed thing'}])
    valid = evaluation('investigation', checks=[{'id': 'read', 'kind': 'tool_evidence',
        'target': '.', 'expected': 'list_directory'}])
    def answer(messages):
        context = request_payload(messages)
        refs = [k for k,v in context['completed_tools'].items() if v['tool'] == 'list_directory']
        return turn('answer_ready', message='The folder is empty.', refs=refs)
    coordinator, client, root = service(tmp_path, [guessed, valid, answer])
    result = coordinator.run('What is in this workspace right now?')
    assert result.receipt['outcome'] == 'completed'
    assert 'HOST CONTRACT ERROR' in json.dumps(client.calls[1][1])
    assert result.receipt['attempts'][0]['status'] == 'completed'


def test_incomplete_introduction_is_corrected_in_same_conversation(tmp_path):
    spec = evaluation('conversation', checks=[])
    spec.update(objective='Give a list comprehension example', required_capabilities=[])
    coordinator, client, root = service(tmp_path, [spec,
        turn('answer_ready', message="Here is a Python list comprehension example:"),
        turn('answer_ready', message='squares = [n * n for n in range(3)]')])
    result = coordinator.run('Show me an example of a list comprehension; do not create files.')
    assert result.receipt['outcome'] == 'completed'
    assert 'for n' in result.content and len(result.receipt['attempts']) == 1
    assert 'HOST ANSWER INCOMPLETE' in json.dumps(client.calls[-1][1])
    assert not list(root.iterdir())


def test_followup_retrieval_uses_recent_dialogue_and_reads_actual_overview(tmp_path):
    spec = evaluation('investigation', checks=[{'id':'source','kind':'tool_evidence','target':'.','expected':'search_code'}])
    def evaluated(messages):
        context = request_payload(messages)['context']
        assert 'notes.py' in json.dumps(context['relevant_sources'])
        assert '.strip()' in json.dumps(context['relevant_sources'])
        assert 'Fresh project description' in json.dumps(context['project_overview'])
        return spec
    def answer(messages):
        value = request_payload(messages)
        sources = [(key, e) for key,e in value['completed_tools'].items() if e['tool']=='search_code']
        key = sources[0][0]
        return turn('answer_ready', message='It strips whitespace from titles.', refs=[key],
            citations=[{'evidence_id':key,'path':'notes.py','line':2,'quote':'return [note["title"].strip() for note in notes]'}])
    coordinator, _, root = service(tmp_path, [evaluated, answer])
    (root/'README.md').write_text('Fresh project description')
    (root/'notes.py').write_text('def active_titles(notes):\n    return [note["title"].strip() for note in notes]\n')
    result = coordinator.run('Does it also clean up the strings?', [
        {'role':'user','content':'Where is the active-title behavior?'},
        {'role':'assistant','content':'Look at active_titles in notes.py.'}])
    assert result.receipt['outcome']=='completed'
    assert 'notes.py:2' in result.content


def test_directory_answer_does_not_need_an_unrelated_readme_citation(tmp_path):
    spec = evaluation('investigation', checks=[{'id':'directory','kind':'tool_evidence','target':'.','expected':'list_directory'}])
    def answer(messages):
        value = request_payload(messages)
        key = next(k for k,e in value['completed_tools'].items() if e['tool']=='list_directory')
        return turn('answer_ready', message='The directory contains README.md and notes.py.', refs=[key])
    coordinator, _, root = service(tmp_path, [spec, answer])
    (root/'README.md').write_text('Overview is context, not proof of directory entries')
    (root/'notes.py').write_text('value=1')
    result = coordinator.run('Could you enumerate the entries in this workspace?')
    assert result.receipt['outcome']=='completed'
    assert 'Sources:' not in result.content


def test_conversation_keeps_latest_user_turn_plain_and_preserves_history(tmp_path):
    spec = evaluation('conversation', checks=[])
    spec.update(objective='Recall an idea name', required_capabilities=[])
    def answer(messages):
        assert messages[-1] == {'role':'user','content':'What did I call my idea?'}
        assert {'role':'user','content':'Call it Docklight.'} in messages
        assert any('APPLICATION CONTEXT' in m['content'] for m in messages if m['role']=='system')
        return turn('answer_ready', message='You called it Docklight.')
    coordinator, _, _ = service(tmp_path, [spec,answer])
    result = coordinator.run('What did I call my idea?', [{'role':'user','content':'Call it Docklight.'},
        {'role':'assistant','content':'Docklight it is.'}])
    assert result.receipt['outcome']=='completed' and 'Docklight' in result.content


def test_conversation_grammar_cannot_propose_tools_and_evaluation_names_real_tools():
    from local_model_chat.orchestration.contracts import EVALUATION_SCHEMA, decode, turn_schema
    with pytest.raises(ValueError):
        decode(json.dumps(turn('needs_tools', calls=[{'call_id':'x','name':'write_file',
            'arguments':{'path':'unexpected.txt','content':'unexpected'}}])), turn_schema(allow_tools=False))
    invalid = evaluation('investigation', checks=[{'id':'read','kind':'tool_evidence',
        'target':'.','expected':'project'}])
    with pytest.raises(ValueError):
        decode(json.dumps(invalid), EVALUATION_SCHEMA)


def test_failed_answer_verification_gets_one_repair_without_weakening_checks(tmp_path):
    spec = evaluation('investigation', checks=[{'id':'actual-read','kind':'tool_evidence',
        'target':'notes.py','expected':'read_file'}])
    def premature(messages):
        value=request_payload(messages)
        key=next(k for k,e in value['completed_tools'].items() if e['tool']=='search_code')
        return turn('answer_ready',message='The value is one.',refs=[key],
            citations=[{'evidence_id':key,'path':'notes.py','line':1,'quote':'value = 1'}])
    def checked(messages):
        result=json.loads(next(m['content'].split(': ',1)[1] for m in reversed(messages)
            if m['content'].startswith('HOST TOOL RESULT')))
        return turn('answer_ready',message='The value is one.',refs=[result['evidence_id']],
            citations=[{'evidence_id':result['evidence_id'],'path':'notes.py','line':1,'quote':'value = 1'}])
    coordinator, client, root = service(tmp_path,[spec,premature,
        turn('needs_tools',calls=[{'call_id':'read','name':'read_file','arguments':{'path':'notes.py','start':1,'end':1}}]),checked])
    (root/'notes.py').write_text('value = 1')
    result=coordinator.run('What value is in notes.py?')
    assert result.receipt['outcome']=='completed'
    assert result.receipt['evaluation']['acceptance_checks']==spec['acceptance_checks']
    assert result.receipt['attempts'][0]['verification_correction'] is True
    assert any('HOST VERIFICATION FAILED' in json.dumps(messages) for _,messages in client.calls)
    assert len(result.receipt['attempts'])==1


def test_semantically_unanswered_local_reply_falls_back_even_with_valid_quotes(tmp_path):
    from local_model_chat.domain import GenerationMetrics
    from local_model_chat.ollama import ChatResponse
    spec=evaluation('investigation',checks=[{'id':'source','kind':'tool_evidence','target':'notes.py','expected':'search_code'}])
    def answer(messages):
        value=request_payload(messages)
        key=next(k for k,e in value['completed_tools'].items() if e['tool']=='search_code')
        return turn('answer_ready',message=('Cannot determine the behavior from this context.' if not value['previous_attempts'] else 'The missing key raises KeyError.'),refs=[key],
            citations=[{'evidence_id':key,'path':'notes.py','line':1,'quote':'title = note["title"]'}])
    coordinator, _, root=service(tmp_path,[])
    class ReviewedClient(Client):
        def chat(self,model,messages,**kwargs):
            if 'addresses_request' in kwargs.get('response_format',{}).get('properties',{}):
                self.calls.append((model,messages))
                return ChatResponse({'content':json.dumps({'addresses_request':model!='molicoder:latest',
                    'supported':True,'reason':'First candidate did not answer the current question'})},GenerationMetrics())
            return super().chat(model,messages,**kwargs)
    coordinator.client=ReviewedClient([spec,answer,answer])
    coordinator.catalog.client=coordinator.client
    (root/'notes.py').write_text('title = note["title"]')
    result=coordinator.run('What happens if title is missing?')
    assert result.receipt['outcome']=='completed'
    assert [a['status'] for a in result.receipt['attempts']]==['failed','completed']
    assert result.receipt['attempts'][0]['answer_review']['addresses_request'] is False
    assert result.receipt['attempts'][1]['answer_review']['addresses_request'] is True


def test_investigation_failures_do_not_disable_a_working_conversation_model(tmp_path):
    from local_model_chat.orchestration.catalog import CapabilityCatalogService
    coordinator, client, _=service(tmp_path,[])
    catalog=CapabilityCatalogService(coordinator.store,client)
    catalog.refresh(discover_runtimes=False)
    route=catalog.routes(None,'molicoder:latest',allow_external=False)[0][0]
    for _ in range(3): catalog.record_outcome(route,'failed',spec=evaluation('investigation'),error='Answer review rejected unresolved behavior')
    conversation={'intent':'conversation','complexity':'low'}
    assert catalog.routes(None,'molicoder:latest',allow_external=False,spec=conversation)[0][0].model=='molicoder:latest'
    routes,excluded=catalog.routes(None,'molicoder:latest',allow_external=False,spec=evaluation('investigation'))
    assert all(r.model!='molicoder:latest' for r in routes)
    assert any('task-family' in e['reason'] for e in excluded)
    catalog.record_outcome(route,'completed',spec=conversation)
    assert all(r.model!='molicoder:latest' for r in catalog.routes(None,'molicoder:latest',allow_external=False,spec=evaluation('investigation'))[0])


def test_known_project_destination_does_not_require_repeated_clarification(tmp_path):
    from test_request_orchestration import write_call
    coordinator,client,root=service(tmp_path,[evaluation(),
        turn('needs_clarification',message='Which directory should I create the file in?'),
        turn('needs_tools',calls=[write_call()])],review=lambda _:True)
    result=coordinator.run('Create Iworked.txt with the text yeay')
    assert result.receipt['outcome']=='completed'
    assert (root/'Iworked.txt').read_bytes()==b'yeay'
    assert len(result.receipt['attempts'])==1
    assert result.receipt['attempts'][0]['context_correction'] is True
    assert 'HOST REQUEST READY' in json.dumps(client.calls[-1][1])
