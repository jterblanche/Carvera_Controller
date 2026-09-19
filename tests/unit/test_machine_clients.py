from carveracontroller.machine.clients import ClientRow, rows_for_display
from carveracontroller.protocols.handshake import ClientEntry

SELF_ID = 1
OTHER_ID = 2


def _entries():
    return (
        ClientEntry(id=OTHER_ID, name="Workshop Laptop", link=0, has_control=False),
        ClientEntry(id=SELF_ID, name="Office PC", link=0, has_control=True),
    )


def test_rows_for_display_puts_the_controlling_entry_first():
    rows = rows_for_display(_entries(), own_id=SELF_ID)

    assert rows == (
        ClientRow(name="Office PC", has_control=True, is_self=True),
        ClientRow(name="Workshop Laptop", has_control=False, is_self=False),
    )


def test_rows_for_display_marks_self():
    rows = rows_for_display(_entries(), own_id=OTHER_ID)

    assert rows[1].is_self is True
    assert rows[0].is_self is False


def test_rows_for_display_empty():
    assert rows_for_display((), own_id=SELF_ID) == ()
