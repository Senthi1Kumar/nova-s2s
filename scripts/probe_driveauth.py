"""One-shot probe: what does DriveAuth decide with audio_np=None under mocks?

Run: DRIVEAUTH_USE_MOCK=1 uv run python scripts/probe_driveauth.py
"""
import tempfile

import numpy as np

from driveauth import DriveAuth


def main() -> None:
    store = tempfile.mkdtemp()
    auth = DriveAuth.load(store_dir=store, use_mock_matchers=True)
    for label, audio in [("none", None), ("zeros", np.zeros(16_000, dtype=np.float32))]:
        for amount, known in [(50.0, True), (60_000.0, True)]:
            r = auth.authenticate(
                audio_np=audio, tier_hint="payment",
                amount=amount, beneficiary_known=known,
            )
            print(f"audio={label:5s} amount={amount:>8} known={known} -> "
                  f"{r.decision.value:18s} trust={r.trust_score:.3f} "
                  f"risk={r.risk_score:.3f} tier={r.tier} rule={r.policy_rule}")


if __name__ == "__main__":
    main()
