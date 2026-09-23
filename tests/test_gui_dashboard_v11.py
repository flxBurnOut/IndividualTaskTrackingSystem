import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtWidgets import QApplication,QPushButton
from management.core import Core
from management.gui_dashboard import DashboardPage
from test_ux_workflows_v2 import QueuedCoreBridge,wait


def test_home_route_and_past_warnings_start_closed(tmp_path):
    app=QApplication.instance() or QApplication([])
    core=Core(tmp_path);bridge=QueuedCoreBridge(core);opened=[]
    page=DashboardPage(bridge,on_today=opened.append);page.show();page.refresh()
    try:
        wait(app,lambda:bridge.pending==0)
        assert '星期' in page.date.text()
        assert not page.past_box.isVisible() and not page.past_toggle.isChecked()
        next(b for b in page.findChildren(QPushButton) if b.text()=='查看今天').click()
        assert opened==[page.result['date']]
        assert core.query('list',type='task')['total']==0
    finally:page.close();page.deleteLater();app.processEvents()
