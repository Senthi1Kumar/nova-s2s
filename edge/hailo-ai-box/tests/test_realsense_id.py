import pytest

from aibox.backends.realsense_id import status_name, validate_user_id


def test_validate_user_id():
    assert validate_user_id('aibox_demo') == 'aibox_demo'
    with pytest.raises(ValueError):
        validate_user_id('../bad')
    with pytest.raises(ValueError):
        validate_user_id('x' * 31)


def test_status_name_handles_enum_and_string():
    class Status:
        def __str__(self):
            return 'AuthenticateStatus.Success'

    assert status_name(Status()) == 'Success'
    assert status_name('EnrollStatus.NoFaceDetected') == 'NoFaceDetected'
