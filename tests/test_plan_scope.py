import pytest
from local_model_chat.molicoder.plan_scope import validate_plan_scope
from local_model_chat.molicoder.planner.schema import parse_plan


def plan(path):
    return parse_plan({'objective':'Fixture', 'tasks':[{'key':'one','title':'One','objective':'One',
                       'risk':'low','depends_on':[], 'expected_paths':[str(path)]}]})


def test_scope_allows_only_contained_paths(tmp_path):
    root=tmp_path/'project';root.mkdir()
    assert validate_plan_scope(plan(root/'inside.txt'),root).tasks[0].expected_paths == ('inside.txt',)
    for path in [tmp_path/'Messi', '../escape', 'C:/other/Messi', 'C:relative', '..\\escape']:
        with pytest.raises(ValueError):validate_plan_scope(plan(path),root)
