import pytest
from local_model_chat.ui.styles import THEMES
from local_model_chat.storage import ChatStore
from local_model_chat.ui.main_window import MainWindow


def luminance(color):
    channels = [int(color[i:i+2],16)/255 for i in (1,3,5)]
    linear = [c/12.92 if c <= .04045 else ((c+.055)/1.055)**2.4 for c in channels]
    return sum(c*w for c,w in zip(linear,(.2126,.7152,.0722)))


def contrast(a,b):
    high,low=sorted((luminance(a),luminance(b)),reverse=True)
    return (high+.05)/(low+.05)


@pytest.mark.parametrize('theme', THEMES)
def test_text_and_interaction_contrast(theme):
    t=THEMES[theme]
    for surface in ('canvas','panel','raised','input','selected','code'):
        for ink in ('text','muted'):
            assert contrast(t[ink],t[surface]) >= 4.5, (theme,ink,surface)
    for ink in ('accent','magenta','success','warning','error'):
        for surface in ('canvas','panel'):
            assert contrast(t[ink],t[surface]) >= 4.5, (theme,ink,surface)
    assert contrast(t['on_accent'],t['accent']) >= 4.5
    assert contrast(t['error'],t['error_bg']) >= 4.5
    # The Build blocker briefly fades to 90% over the containing panel.
    def faded(color):
        channels = [round(.9 * int(color[i:i+2],16) + .1 * int(t['panel'][i:i+2],16)) for i in (1,3,5)]
        return '#' + ''.join(f'{c:02x}' for c in channels)
    assert contrast(faded(t['error']),faded(t['error_bg'])) >= 4.5
    for surface in ('canvas','panel','input'):
        assert contrast(t['border'],t[surface]) >= 3


@pytest.mark.parametrize('saved', ['matrix','dark','default','invalid'])
def test_old_dark_preferences_restore_default_without_losing_drafts(qtbot,tmp_path,saved):
    store=ChatStore(tmp_path/'theme.db');store.set_setting('ui.theme',saved)
    w=MainWindow(store,start_ollama_check=False);qtbot.addWidget(w)
    assert w.theme_selector.currentText()=='Default'
    assert w.theme_selector.currentData()=='default'
    w.composer.setPlainText('Keep this draft')
    w.theme_selector.setCurrentIndex(1)
    assert store.get_setting('ui.theme')=='light'
    w.theme_selector.setCurrentIndex(0)
    assert store.get_setting('ui.theme')=='default'
    assert w.composer.toPlainText()=='Keep this draft'
