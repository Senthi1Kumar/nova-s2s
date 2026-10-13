"""SessionLease: one conversation across both front doors (LAN realtime, LiveKit)."""
import threading

from nova_hailo.web.session_lease import SessionLease


def test_first_caller_wins_and_second_sees_the_holder():
    lease = SessionLease()
    first = lease.try_acquire("realtime")
    assert first is not None
    assert lease.try_acquire("livekit") is None
    assert lease.holder == "realtime"


def test_release_frees_the_lease_for_the_other_door():
    lease = SessionLease()
    held = lease.try_acquire("livekit")
    held.release()
    assert lease.holder is None
    assert lease.try_acquire("realtime") is not None


def test_release_is_idempotent_and_a_stale_release_cannot_free_a_new_holder():
    lease = SessionLease()
    old = lease.try_acquire("realtime")
    old.release()
    new = lease.try_acquire("livekit")
    old.release()  # late double-release from the first session's finally block
    assert lease.holder == "livekit"
    new.release()
    assert lease.holder is None


def test_context_manager_releases_on_exit():
    lease = SessionLease()
    with lease.try_acquire("realtime"):
        assert lease.holder == "realtime"
    assert lease.holder is None


def test_concurrent_acquire_admits_exactly_one():
    lease = SessionLease()
    barrier = threading.Barrier(16)
    won: list = []

    def race(i):
        barrier.wait()
        got = lease.try_acquire(f"t{i}")
        if got is not None:
            won.append(got)

    threads = [threading.Thread(target=race, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(won) == 1
