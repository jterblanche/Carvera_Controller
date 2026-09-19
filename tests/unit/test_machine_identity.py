from carveracontroller.machine.identity import (
    MAX_NAME_BYTES,
    ControllerIdentity,
    load_or_create_identity,
    set_name,
    trim_name,
)


class FakeStore:
    """In-memory IdentityStore double for tests."""

    def __init__(self, **initial):
        self._data = dict(initial)

    def get(self, key):
        return self._data.get(key)

    def set(self, key, value):
        self._data[key] = value


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


def test_load_or_create_identity_creates_and_persists_once():
    store = FakeStore()

    identity = load_or_create_identity(store)

    assert isinstance(identity.id, int)
    assert 0 <= identity.id < 2**64
    assert identity.name
    assert store.get("controller_id") == str(identity.id)
    assert store.get("controller_name") == identity.name


def test_load_or_create_identity_is_stable_across_calls():
    store = FakeStore()

    first = load_or_create_identity(store)
    second = load_or_create_identity(store)

    assert first == second


def test_load_or_create_identity_reads_persisted_values():
    store = FakeStore(controller_id="42", controller_name="Shop Laptop")

    identity = load_or_create_identity(store)

    assert identity.id == 42
    assert identity.name == "Shop Laptop"


def test_load_or_create_identity_recovers_from_corrupt_persisted_id():
    store = FakeStore(controller_id="not-a-number", controller_name="Shop Laptop")

    identity = load_or_create_identity(store)

    assert isinstance(identity.id, int)
    assert identity.name == "Shop Laptop"
    assert store.get("controller_id") == str(identity.id)


def test_load_or_create_identity_regenerates_a_negative_persisted_id():
    # A hand-edited or corrupted config value that parses as an int but
    # can't fit the hello frame's 8-byte unsigned id field.
    store = FakeStore(controller_id="-1", controller_name="Shop Laptop")

    identity = load_or_create_identity(store)

    assert 0 <= identity.id < 2**64
    assert store.get("controller_id") == str(identity.id)


def test_load_or_create_identity_regenerates_an_oversized_persisted_id():
    store = FakeStore(controller_id=str(2**64), controller_name="Shop Laptop")

    identity = load_or_create_identity(store)

    assert 0 <= identity.id < 2**64
    assert store.get("controller_id") == str(identity.id)


def test_load_or_create_identity_keeps_the_boundary_values():
    store_low = FakeStore(controller_id="0", controller_name="A")
    store_high = FakeStore(controller_id=str(2**64 - 1), controller_name="B")

    assert load_or_create_identity(store_low).id == 0
    assert load_or_create_identity(store_high).id == 2**64 - 1


def test_controller_identity_trims_name_on_construction():
    identity = ControllerIdentity(id=1, name="x" * 50)
    assert len(identity.name.encode("utf-8")) <= MAX_NAME_BYTES


def test_set_name_persists_trimmed_name_and_keeps_id():
    store = FakeStore(controller_id="7", controller_name="Old Name")

    identity = set_name(store, "New Name")

    assert identity.id == 7
    assert identity.name == "New Name"
    assert store.get("controller_name") == "New Name"


def test_set_name_regenerates_an_out_of_range_persisted_id():
    store = FakeStore(controller_id="-5", controller_name="Old Name")

    identity = set_name(store, "New Name")

    assert 0 <= identity.id < 2**64
    assert store.get("controller_id") == str(identity.id)


def test_set_name_trims_an_overlong_name():
    store = FakeStore()

    identity = set_name(store, "x" * 50)

    assert len(identity.name.encode("utf-8")) <= MAX_NAME_BYTES
    assert store.get("controller_name") == identity.name
