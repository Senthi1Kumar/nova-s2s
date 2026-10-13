"""Echo guard: a transcript made of what Nova said in the last seconds is her own voice."""
from nova_hailo.web.echo_guard import EchoGuard


class Clock:
    t = 100.0

    def __call__(self):
        return self.t


def guard(*clauses, clock=None):
    g = EchoGuard(clock=clock or Clock())
    for c in clauses:
        g.spoken(c)
    return g


def test_a_clause_heard_back_is_echo():
    g = guard("Navigating to work.", "About 9 minutes.")
    assert g.is_echo("navigating to work")
    assert g.is_echo("to work about 9 minutes")       # spans two clauses


def test_an_interrupted_reply_still_counts():
    g = guard("The nearest charger is on Hamilton Avenue, about", )
    assert g.is_echo("the nearest charger is on hamilton")


def test_a_topical_follow_up_is_not_echo():
    g = guard("The Tesla Model 3 has a range of about 500 kilometres.")
    assert not g.is_echo("what is the range of the tesla model 3")
    assert not g.is_echo("and how long does it take to charge")


def test_short_words_are_never_echo():
    g = guard("Yes.", "Done.")
    assert not g.is_echo("yes") and not g.is_echo("stop")


def test_only_recent_speech_counts():
    clock = Clock()
    g = guard("Navigating to work.", clock=clock)
    clock.t += 21
    assert not g.is_echo("navigating to work")


def test_only_the_last_eight_clauses_are_kept():
    g = guard("Navigating to work.", *[f"Clause number {i} here." for i in range(8)])
    assert not g.is_echo("navigating to work")
    assert g.is_echo("clause number 7 here")
