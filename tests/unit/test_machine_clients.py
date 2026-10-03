from carveracontroller.machine.clients import ClientRow, row_display_text, rows_for_display, truncate_name
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


# -- truncate_name / row_display_text: the status drop-down's rows (ticket
# #173) -- a wrapped second line reads as a separate controller, so a long
# name is ellipsised to one line instead.


def test_truncate_name_leaves_short_names_untouched():
    assert truncate_name("Office PC") == "Office PC"


def test_truncate_name_exactly_at_the_limit_is_unchanged():
    name = "A" * 20
    assert truncate_name(name, max_len=20) == name


def test_truncate_name_ellipsises_names_over_the_limit():
    # 31 bytes is the wire format's own cap (protocols.handshake).
    long_name = "A" * 31
    result = truncate_name(long_name, max_len=20)
    assert result == "A" * 19 + "…"
    assert len(result) == 20


def test_row_display_text_plain_other_controller():
    row = ClientRow(name="Workshop Laptop", has_control=False, is_self=False)
    assert row_display_text(row, you_suffix=" (you)", control_suffix=" — in control") == "Workshop Laptop"


def test_row_display_text_marks_self_and_control():
    row = ClientRow(name="Office PC", has_control=True, is_self=True)
    text = row_display_text(row, you_suffix=" (you)", control_suffix=" — in control")
    assert text == "Office PC (you) — in control"


def test_row_display_text_marks_control_without_self():
    row = ClientRow(name="Workshop Laptop", has_control=True, is_self=False)
    text = row_display_text(row, you_suffix=" (you)", control_suffix=" — in control")
    assert text == "Workshop Laptop — in control"


def test_row_display_text_truncates_long_names_before_appending_suffixes():
    row = ClientRow(name="A" * 31, has_control=True, is_self=True)
    text = row_display_text(row, you_suffix=" (you)", control_suffix=" — in control", max_name_len=20)
    assert text == ("A" * 19 + "…" + " (you)" + " — in control")
