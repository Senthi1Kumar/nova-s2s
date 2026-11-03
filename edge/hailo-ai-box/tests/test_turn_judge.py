from aibox.voice.turn_judge import TimeoutJudge, TurnQuestions


async def test_timeout_judge_recognizes_holding_and_completed_speech():
    judge = TimeoutJudge()
    holding = await judge.judge(
        {"partial_transcript": "hold on let me type something"}, TurnQuestions.all()
    )
    complete = await judge.judge(
        {"partial_transcript": "what is the weather today"}, TurnQuestions.all()
    )
    assert holding["holding_floor"] > 0.8
    assert complete["turn_complete"] > 0.7
    assert complete["holding_floor"] < 0.2


async def test_timeout_judge_distinguishes_backchannel_interruption_and_side_talk():
    judge = TimeoutJudge()
    backchannel = await judge.judge({"partial_transcript": "right"}, TurnQuestions.all())
    interrupt = await judge.judge({"partial_transcript": "wait wait let me stop you"}, TurnQuestions.all())
    side = await judge.judge({"partial_transcript": "hey can you hold my coffee please"}, TurnQuestions.all())
    assert backchannel["taking_floor"] < 0.2
    assert interrupt["taking_floor"] > 0.8
    assert side["addressed_to_agent"] < 0.3
