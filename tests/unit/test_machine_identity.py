from carveracontroller.machine.identity import (
    MAX_NAME_BYTES,
    ControllerIdentity,
    load_identity,
    set_name,
    trim_name,
    with_id,
)


class FakeStore:
    """In-memory IdentityStore double for tests."""

    def __init__(self, **initial):
        self._data = dict(initial)

    def get(self, key):
        return self._data.get(key)

    def set(self, key, value):
        self._data[key] = value

    def remove(self, key):
        self._data.pop(key, None)


def test_trim_name_leaves_short_names_alone():
    assert trim_name("Office PC") == "Office PC"


def test_trim_name_caps_at_max_bytes():
    name = "x" * 50
    trimmed = trim_name(name)
    assert len(trimmed.encode("utf-8")) <= MAX_NAME_BYTES


def test_trim_name_never_splits_a_multibyte_character():
    # Each euro sign is 3 bytes in UTF-8; 11 of them is 33 bytes, over the 31 limit.
    name = "€" * 11
    trimmed = trim_name(name)
    assert len(trimmed.encode("utf-8")) <= MAX_NAME_BYTES
    # Every remaining character must still decode cleanly (no partial bytes).
    trimmed.encode("utf-8").decode("utf-8")


def test_load_identity_takes_its_id_from_the_computer_part():
    store = FakeStore(controller_name="Shop Laptop")

    identity = load_identity(store, computer_id=0x1122334455667788, launch=0x99)

    assert identity == ControllerIdentity(id=0x1122334455667788, name="Shop Laptop", launch=0x99)


def test_load_identity_creates_and_persists_a_default_name():
    store = FakeStore()

    identity = load_identity(store, computer_id=5, launch=6)

    assert identity.name
    assert store.get("controller_name") == identity.name


def test_first_start_after_the_upgrade_drops_the_stored_random_id():
    """The id is no longer kept in the settings file; the old one is removed
    so a copied settings file carries nothing. The name stays."""
    store = FakeStore(controller_id="42", controller_name="Shop Laptop", other="kept")

    identity = load_identity(store, computer_id=7, launch=8)

    assert identity.id == 7
    assert identity.name == "Shop Laptop"
    assert store.get("controller_id") is None
    assert "controller_id" not in store._data
    assert store.get("other") == "kept"


def test_a_copied_settings_file_does_not_copy_the_id():
    copied = {"controller_id": "42", "controller_name": "Shop Laptop"}

    here = load_identity(FakeStore(**copied), computer_id=1001, launch=1)
    there = load_identity(FakeStore(**copied), computer_id=2002, launch=2)

    assert here.id != there.id


def test_identity_keeps_its_launch_part_when_the_name_changes():
    store = FakeStore(controller_name="Old Name")
    identity = load_identity(store, computer_id=7, launch=0xABC)

    renamed = set_name(store, "New Name", identity)

    assert renamed == ControllerIdentity(id=7, name="New Name", launch=0xABC)


def test_with_id_keeps_the_name_and_launch_part():
    identity = ControllerIdentity(id=1, name="A", launch=2)

    assert with_id(identity, 3) == ControllerIdentity(id=3, name="A", launch=2)


def test_controller_identity_trims_name_on_construction():
    identity = ControllerIdentity(id=1, name="x" * 50)
    assert len(identity.name.encode("utf-8")) <= MAX_NAME_BYTES


def test_set_name_persists_trimmed_name_and_keeps_id():
    store = FakeStore(controller_name="Old Name")

    identity = set_name(store, "New Name", ControllerIdentity(id=7, name="Old Name"))

    assert identity.id == 7
    assert identity.name == "New Name"
    assert store.get("controller_name") == "New Name"


def test_set_name_trims_an_overlong_name():
    store = FakeStore()

    identity = set_name(store, "x" * 50, ControllerIdentity(id=7, name="A"))

    assert len(identity.name.encode("utf-8")) <= MAX_NAME_BYTES
    assert store.get("controller_name") == identity.name


def test_set_name_falls_back_to_the_default_name_when_blank():
    store = FakeStore()

    identity = set_name(store, "", ControllerIdentity(id=7, name="A"))

    assert identity.name
