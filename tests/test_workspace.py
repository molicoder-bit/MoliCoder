import json
import pytest
from PySide6.QtCore import Qt
from local_model_chat.storage import ChatStore
from local_model_chat.ui.main_window import MainWindow
from local_model_chat.kandev.service import KandevServiceController
from local_model_chat.kandev.ui_config import configured_kandev_adapter


def test_workspace_keeps_modes_and_drafts_separate(qtbot, tmp_path):
    store=ChatStore(tmp_path/'ui.db')
    first=store.upsert_project('One',str(tmp_path/'one'),'python')
    second=store.upsert_project('Two',str(tmp_path/'two'),'python')
    window=MainWindow(store,start_ollama_check=False);qtbot.addWidget(window)
    window._load_project_list(select_id=first.id)
    window.composer.setPlainText('chat draft');window.build_request.setPlainText('build draft')
    window._set_view('build');window._set_view('chat')
    assert window.composer.toPlainText()=='chat draft'
    window._load_project_list(select_id=second.id)
    assert window.composer.toPlainText()==''
    window._load_project_list(select_id=first.id)
    assert window.build_request.toPlainText()=='build draft'
    assert window.findChild(__import__('PySide6.QtWidgets',fromlist=['QLabel']).QLabel,'brandLogo').pixmap().isNull() is False


def test_drawer_usage_and_compact_build_remain_reachable(qtbot,tmp_path):
    from local_model_chat.ui.workspace import open_drawer
    window=MainWindow(ChatStore(tmp_path/'ui.db'),start_ollama_check=False);qtbot.addWidget(window)
    window.resize(1020,680);window.show();window._set_view('build')
    assert window.build_view.widget().minimumHeight() >= 740
    open_drawer(window,'Performance')
    assert window.drawer.isVisible()
    assert window.context_label.isVisible()
    assert window.context_progress.isVisible()


def test_saved_project_kandev_profile_precedes_legacy_environment(tmp_path):
    store=ChatStore(tmp_path/'ui.db')
    profile=dict(endpoint='http://127.0.0.1:40000/',version='0.97.0',workspace_id='w',workflow_id='f',workflow_step_id='s',repository_id='r',agent_profile_id='a')
    store.set_setting('kandev.project.4',json.dumps(profile))
    adapter=configured_kandev_adapter(store,{},project_id=4)
    assert adapter.connection.endpoint==profile['endpoint']
    with pytest.raises(ValueError): configured_kandev_adapter(store,{},project_id=5)


def test_stop_refuses_foreign_or_reused_pid(tmp_path,monkeypatch):
    service=KandevServiceController(tmp_path);service.home.mkdir()
    service.receipt.write_text(json.dumps(dict(pid=22,identity='old',origin='http://127.0.0.1:38429/')))
    monkeypatch.setattr('local_model_chat.kandev.service.identity',lambda pid:'new')
    with pytest.raises(ValueError,match='ownership'):service.stop('http://127.0.0.1:38429/')


def test_stop_refuses_active_native_board_agent(tmp_path,monkeypatch):
    service=KandevServiceController(tmp_path);service.home.mkdir()
    service.receipt.write_text(json.dumps(dict(pid=22,identity='same',origin='http://127.0.0.1:38429/')))
    monkeypatch.setattr('local_model_chat.kandev.service.identity',lambda pid:'same')
    monkeypatch.setattr('local_model_chat.kandev.socket_rpc.call_kandev_socket',lambda *args:{'agents':[{'status':'running'}]})
    with pytest.raises(ValueError,match='active'):service.stop('http://127.0.0.1:38429/')


def test_start_refuses_unmanaged_binary(tmp_path,monkeypatch):
    service=KandevServiceController(tmp_path)
    monkeypatch.setattr('local_model_chat.kandev.service.get_json',lambda *args:None)
    with pytest.raises(ValueError,match='Install'):service.start('http://127.0.0.1:48998/')
