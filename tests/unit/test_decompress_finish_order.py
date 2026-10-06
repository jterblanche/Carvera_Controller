"""updateCompressProgress must end the decompress wait before it schedules
the popup's close.

The "Decompressing" popup is opened by a delayed clock callback that only
opens it while the wait is still live (decompstatus). updateCompressProgress
runs on the monitorSerial thread; if it scheduled progressFinish first and
cleared decompstatus afterwards, the UI thread could run that finish and
then the delayed open in between, see the wait still live, and open a
popup whose close has already run.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from carveracontroller.main import Makera


def test_wait_is_ended_before_the_popup_close_is_scheduled(monkeypatch):
    host = SimpleNamespace(
        fileCompressionBlocks=2,
        decompstatus=True,
        pending_decompress_callback=None,
        progressUpdate=MagicMock(),
        progressFinish=MagicMock(),
        file_popup=SimpleNamespace(refresh_machine=MagicMock()),
    )
    status_when_finish_scheduled = []

    def schedule_once(callback, timeout=0, *args, **kwargs):
        if callback is host.progressFinish:
            status_when_finish_scheduled.append(host.decompstatus)

    monkeypatch.setattr("carveracontroller.main.Clock.schedule_once", schedule_once)

    Makera.updateCompressProgress(host, 2)

    assert status_when_finish_scheduled == [False]
    assert host.decompstatus is False
