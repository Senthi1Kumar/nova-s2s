"""nova.card: what a display shows for an answer, a route, results or a delegated result."""
from nova_hailo.web.cards import delivery_card_events, turn_card

NAV_RESULTS = "Vehicle.Private.Nova.Nav.LastResults"


def test_web_answer_card_titles_with_the_question_and_splits_sentences():
    card = turn_card("who won the match yesterday?", "India won by 6 wickets. Kohli scored 84.",
                     "web_search")
    assert card == {"kind": "answer", "title": "who won the match yesterday?",
                    "lines": ["India won by 6 wickets.", "Kohli scored 84."], "items": []}


def test_workspace_answers_are_titled_by_source():
    assert turn_card("what's on tomorrow", "Two meetings.", "check_calendar")["title"] == "Calendar"
    assert turn_card("any mail", "Three new emails.", "check_email")["title"] == "Email"


def test_no_card_for_chat_plain_car_actions_or_an_empty_reply():
    assert turn_card("hello", "Hi there.", None) is None
    assert turn_card("set the temperature to 23", "Driver side set to 23 degrees.", "vehicle",
                     trace={"calls": [{"device": "hvac", "api": "set_temperature"}]}) is None
    assert turn_card("who won", "", "web_search") is None


def test_research_card_waits_for_the_delivered_result():
    assert turn_card("research EV batteries", "This may take a moment.", "deep_research") is None


def test_navigation_turn_becomes_a_route_card():
    trace = {"calls": [{"device": "navigation", "api": "navigate_to",
                        "args": {"destination": "work"}, "status": "success"}]}
    card = turn_card("take me to work", "Navigating to work. About 8 minutes.", "vehicle", trace=trace)
    assert card["kind"] == "route" and card["title"] == "work"
    assert card["lines"] == ["Navigating to work.", "About 8 minutes."]


def test_nearby_search_becomes_a_results_card():
    trace = {"calls": [{"device": "navigation", "api": "find_nearby", "status": "success"}],
             "diff": [{"path": NAV_RESULTS, "before": [], "after": [
                 {"name": "Hilltop Cafe", "lat": 1, "lon": 2, "category": "coffee"},
                 {"name": "Blue Door Coffee", "lat": 1, "lon": 2, "category": "coffee"}]}]}
    card = turn_card("find coffee", "Hilltop Cafe is closest.", "vehicle", trace=trace)
    assert card["kind"] == "results"
    assert card["items"] == [{"title": "Hilltop Cafe", "sub": "coffee"},
                             {"title": "Blue Door Coffee", "sub": "coffee"}]


def test_media_turn_becomes_a_list_card():
    trace = {"calls": [{"device": "media", "api": "play_media", "args": {"query": "jazz"},
                        "status": "success"}]}
    card = turn_card("play some jazz", "Playing jazz.", "vehicle", trace=trace)
    assert card["kind"] == "list" and card["title"] == "Now playing"


def test_a_delegated_turn_gets_a_planning_card():
    card = turn_card("make it comfortable", "On it. I'll tell you when that's done.", "delegate")
    assert card["kind"] == "answer" and card["title"] == "Planning"


def test_delivered_delegated_result_lists_what_ran():
    item = {"kind": "delegate", "speak": "Hazards on. Volume 25.", "traces": [{
        "router": "backend", "gate": ["execute", "execute"],
        "calls": [{"device": "lights", "api": "set_hazards", "status": "success"},
                  {"device": "media", "api": "set_volume", "status": "success"}]}]}
    events = delivery_card_events(item, 9)
    assert events == [{"type": "nova.card", "generation_id": 9, "kind": "answer", "title": "Done",
                       "lines": ["Hazards on.", "Volume 25."],
                       "items": [{"title": "lights · set_hazards", "sub": "success"},
                                 {"title": "media · set_volume", "sub": "success"}]}]


def test_a_delegated_confirmation_is_titled_as_a_question():
    item = {"kind": "delegate", "speak": "We're doing 100 km/h. Open the sunroof anyway?",
            "traces": [{"gate": ["confirm_first"], "calls": []}]}
    assert delivery_card_events(item, 3)[0]["title"] == "Needs your answer"


def test_delivered_research_card_titles_with_the_question():
    item = {"job_id": "j1", "user_text": "EV battery recycling", "speak": "Three methods dominate."}
    assert delivery_card_events(item, 4) == [{
        "type": "nova.card", "generation_id": 4, "kind": "answer",
        "title": "EV battery recycling", "lines": ["Three methods dominate."], "items": []}]


def test_an_announcement_is_a_reminder_card():
    item = {"kind": "announce", "speak": "You've set it to 19 degrees, that's quite cool. Want me to "
            "turn on your seat heating?", "traces": []}
    card = delivery_card_events(item, 5)[0]
    assert card["title"] == "Reminder" and card["items"] == []
