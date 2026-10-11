from carveracontroller.machine.clients import (
    ClientRow,
    entries_with_holder,
    holder_from_client_list,
    row_display_text,
    rows_for_display,
    truncate_name,
)
from carveracontroller.protocols.handshake import LINK_USB, LINK_WIFI, ClientEntry

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
        ClientRow(name="Office PC", has_control=True, is_self=True, link="WiFi"),
        ClientRow(name="Workshop Laptop", has_control=False, is_self=False, link="WiFi"),
    )


def test_rows_for_display_marks_self():
    rows = rows_for_display(_entries(), own_id=OTHER_ID)

    assert rows[1].is_self is True
    assert rows[0].is_self is False


def test_rows_for_display_empty():
    assert rows_for_display((), own_id=SELF_ID) == ()


# -- link labels: how each controller is connected, and its IP address
# when two WiFi controllers show the same name.


def _links(entries, own_id=SELF_ID):
    return [(row.name, row.link) for row in rows_for_display(entries, own_id)]


def test_rows_show_usb_and_wifi_for_the_same_name():
    entries = (
        ClientEntry(id=1, name="PC", link=LINK_WIFI, has_control=False, ip="192.168.1.23"),
        ClientEntry(id=2, name="PC", link=LINK_USB, has_control=False),
    )

    assert _links(entries) == [("PC", "USB"), ("PC", "WiFi")]


def test_rows_show_the_ip_when_two_wifi_controllers_share_a_name():
    entries = (
        ClientEntry(id=1, name="PC", link=LINK_WIFI, has_control=False, ip="192.168.1.57"),
        ClientEntry(id=2, name="PC", link=LINK_WIFI, has_control=False, ip="192.168.1.23"),
        ClientEntry(id=3, name="PC", link=LINK_USB, has_control=False),
    )

    assert _links(entries) == [("PC", "USB"), ("PC", "WiFi - 192.168.1.23"), ("PC", "WiFi - 192.168.1.57")]


def test_rows_show_no_ip_for_wifi_controllers_with_different_names():
    entries = (
        ClientEntry(id=1, name="Office PC", link=LINK_WIFI, has_control=False, ip="192.168.1.23"),
        ClientEntry(id=2, name="Shop PC", link=LINK_WIFI, has_control=False, ip="192.168.1.57"),
    )

    assert _links(entries) == [("Office PC", "WiFi"), ("Shop PC", "WiFi")]


def test_rows_show_the_ip_when_names_only_differ_past_the_shown_length():
    entries = (
        ClientEntry(id=1, name="A" * 25 + "1", link=LINK_WIFI, has_control=False, ip="10.0.0.1"),
        ClientEntry(id=2, name="A" * 25 + "2", link=LINK_WIFI, has_control=False, ip="10.0.0.2"),
    )

    assert sorted(link for _, link in _links(entries)) == ["WiFi - 10.0.0.1", "WiFi - 10.0.0.2"]


def test_rows_show_wifi_alone_when_the_machine_sends_no_ip():
    # Firmware from before the client list carried addresses.
    entries = (
        ClientEntry(id=1, name="PC", link=LINK_WIFI, has_control=False),
        ClientEntry(id=2, name="PC", link=LINK_WIFI, has_control=False),
    )

    assert _links(entries) == [("PC", "WiFi"), ("PC", "WiFi")]


def test_rows_show_no_link_for_an_unknown_link_value():
    entries = (ClientEntry(id=1, name="PC", link=7, has_control=False),)

    assert _links(entries) == [("PC", "")]


def test_rows_keep_the_controlling_entry_first_whatever_its_link():
    entries = (
        ClientEntry(id=1, name="PC", link=LINK_USB, has_control=False),
        ClientEntry(id=2, name="PC", link=LINK_WIFI, has_control=True, ip="192.168.1.23"),
    )

    rows = rows_for_display(entries, own_id=1)

    assert [(row.link, row.has_control, row.is_self) for row in rows] == [("WiFi", True, False), ("USB", False, True)]


def test_row_display_text_puts_the_link_after_the_name():
    row = ClientRow(name="PC", has_control=True, is_self=True, link="USB")
    text = row_display_text(row, you_suffix=" (you)", control_suffix=" — Has Control")
    assert text == "PC (USB) (you) — Has Control"


def test_row_display_text_with_the_ip():
    row = ClientRow(name="PC", has_control=False, is_self=False, link="WiFi - 192.168.1.23")
    text = row_display_text(row, you_suffix=" (you)", control_suffix=" — Has Control")
    assert text == "PC (WiFi - 192.168.1.23)"


def test_row_display_text_truncates_the_name_but_not_the_link():
    row = ClientRow(name="A" * 31, has_control=False, is_self=False, link="WiFi - 10.0.0.1")
    text = row_display_text(row, you_suffix=" (you)", control_suffix=" — Has Control", max_name_len=20)
    assert text == "A" * 19 + "… (WiFi - 10.0.0.1)"


# -- truncate_name / row_display_text: the status drop-down's rows -- a
# wrapped second line reads as a separate controller, so a long name is
# ellipsised to one line instead.


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
    assert row_display_text(row, you_suffix=" (you)", control_suffix=" — Has Control") == "Workshop Laptop"


def test_row_display_text_marks_self_and_control():
    row = ClientRow(name="Office PC", has_control=True, is_self=True)
    text = row_display_text(row, you_suffix=" (you)", control_suffix=" — Has Control")
    assert text == "Office PC (you) — Has Control"


def test_row_display_text_marks_control_without_self():
    row = ClientRow(name="Workshop Laptop", has_control=True, is_self=False)
    text = row_display_text(row, you_suffix=" (you)", control_suffix=" — Has Control")
    assert text == "Workshop Laptop — Has Control"


def test_row_display_text_truncates_long_names_before_appending_suffixes():
    row = ClientRow(name="A" * 31, has_control=True, is_self=True)
    text = row_display_text(row, you_suffix=" (you)", control_suffix=" — Has Control", max_name_len=20)
    assert text == ("A" * 19 + "…" + " (you)" + " — Has Control")


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


def test_entries_with_holder_marks_only_the_new_holder():
    marked = entries_with_holder(_entries(), OTHER_ID)

    assert [(entry.id, entry.has_control) for entry in marked] == [(OTHER_ID, True), (SELF_ID, False)]


def test_entries_with_holder_nobody_clears_every_mark():
    marked = entries_with_holder(_entries(), 0)

    assert not any(entry.has_control for entry in marked)


def test_entries_with_holder_missing_from_the_list_marks_nobody():
    # The holder can be a controller whose joined event has not been
    # followed by a fresh client list yet; no row may keep a stale mark.
    marked = entries_with_holder(_entries(), 99)

    assert not any(entry.has_control for entry in marked)


def test_entries_with_holder_keeps_everything_else_and_the_order():
    marked = entries_with_holder(_entries(), OTHER_ID)

    assert [(entry.id, entry.name, entry.link) for entry in marked] == [
        (entry.id, entry.name, entry.link) for entry in _entries()
    ]


def test_entries_with_holder_agreeing_with_the_list_returns_an_equal_list():
    assert entries_with_holder(_entries(), SELF_ID) == _entries()
