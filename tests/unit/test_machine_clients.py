from carveracontroller.machine.clients import ClientRow, holder_from_client_list, rows_for_display
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


def test_holder_from_client_list_names_the_entry_that_has_control():
    assert holder_from_client_list(_entries()) == (SELF_ID, "Office PC")


def test_holder_from_client_list_names_someone_else():
    entries = (
        ClientEntry(id=OTHER_ID, name="Workshop Laptop", link=0, has_control=True),
        ClientEntry(id=SELF_ID, name="Office PC", link=0, has_control=False),
    )
    assert holder_from_client_list(entries) == (OTHER_ID, "Workshop Laptop")


def test_holder_from_client_list_is_nobody_when_no_entry_has_control():
    entries = (
        ClientEntry(id=OTHER_ID, name="Workshop Laptop", link=0, has_control=False),
        ClientEntry(id=SELF_ID, name="Office PC", link=0, has_control=False),
    )
    assert holder_from_client_list(entries) == (0, "")


def test_holder_from_client_list_is_nobody_for_an_empty_list():
    # Also covers old firmware's client-list reply, which never sets
    # has_control true for anyone -- indistinguishable from "nobody holds
    # it" and from an empty list.
    assert holder_from_client_list(()) == (0, "")
