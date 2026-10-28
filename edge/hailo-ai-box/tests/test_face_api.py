import pytest
from fastapi.testclient import TestClient

from aibox.app import create_app
from aibox.config import Settings
from aibox.face_roster import FaceRoster, new_user_id, status_text
from aibox.fakes import make_fake_services


@pytest.fixture
def svc(tmp_path):
    return make_fake_services(Settings(fake=True, face_roster=str(tmp_path / "roster.json")))


@pytest.fixture
def client(svc):
    return TestClient(create_app(svc))


def enroll(client, name, role, **extra):
    return client.post('/v1/face/enroll', json={'name': name, 'role': role, **extra})


def test_enroll_authenticate_delete_flow(client):
    assert client.get('/v1/face/users').json()['users'] == []
    r = enroll(client, 'Alice', 'driver')
    assert r.status_code == 200, r.text
    body = r.json()
    assert body['enrolled'] is True and body['name'] == 'Alice' and body['role'] == 'driver'
    uid = body['user_id']
    users = client.get('/v1/face/users').json()['users']
    assert [(u['name'], u['role']) for u in users] == [('Alice', 'driver')]
    assert users[0]['enrolled_at'] and users[0]['user_id'] == uid

    auth = client.post('/v1/face/authenticate').json()
    assert auth['authenticated'] is True
    assert (auth['name'], auth['role']) == ('Alice', 'driver')
    assert auth['message'] == 'Authenticated: Alice · Driver'
    assert 'embedding' not in auth and 'faceprint' not in auth

    assert client.delete(f'/v1/face/users/{uid}').json()['deleted'] is True
    assert client.get('/v1/face/users').json()['users'] == []
    after = client.post('/v1/face/authenticate').json()
    assert after['authenticated'] is False
    assert after['message'] == status_text('Forbidden')


def test_same_face_is_a_duplicate(client):
    assert enroll(client, 'Alice', 'driver').status_code == 200
    r = enroll(client, 'Bob', 'passenger')
    assert r.status_code == 409
    assert r.json()['error'] == {'code': 'duplicate', 'message': 'Already enrolled as Alice (driver)'}
    assert len(client.get('/v1/face/users').json()['users']) == 1


def test_same_name_and_role_is_a_duplicate(client, svc):
    assert enroll(client, 'Bob', 'passenger').status_code == 200
    svc.face_auth.present = 'face-B'
    r = enroll(client, 'bob', 'passenger')
    assert r.status_code == 409
    assert r.json()['error']['code'] == 'duplicate'
    assert 'Bob' in r.json()['error']['message'] or 'bob' in r.json()['error']['message']
    assert enroll(client, 'Bob', 'driver').status_code == 200   # same name, other role is fine


def test_only_one_driver_needs_confirm(client, svc):
    assert enroll(client, 'Alice', 'driver').status_code == 200
    svc.face_auth.present = 'face-B'
    r = enroll(client, 'Carol', 'driver')
    assert r.status_code == 409 and r.json()['error']['code'] == 'driver_exists'
    assert 'Alice' in r.json()['error']['message']
    ok = enroll(client, 'Carol', 'driver', confirm=True)
    assert ok.status_code == 200 and ok.json()['replaced']
    users = client.get('/v1/face/users').json()['users']
    assert [(u['name'], u['role']) for u in users] == [('Carol', 'driver')]


def test_passengers_do_not_need_confirm_and_delete_all(client, svc):
    assert enroll(client, 'Alice', 'driver').status_code == 200
    svc.face_auth.present = 'face-B'
    assert enroll(client, 'Bob', 'passenger').status_code == 200
    svc.face_auth.present = 'face-C'
    assert enroll(client, 'Cy', 'passenger').status_code == 200
    users = client.get('/v1/face/users').json()['users']
    assert [u['role'] for u in users] == ['driver', 'passenger', 'passenger']
    r = client.delete('/v1/face/users').json()
    assert r['deleted'] is True and r['count'] == 3
    assert client.get('/v1/face/users').json()['users'] == []
    assert client.delete('/v1/face/users/nope').status_code == 404


def test_sensor_only_user_is_listed_as_unassigned(client, svc):
    import asyncio
    asyncio.run(svc.face_auth.enroll('legacy'))
    users = client.get('/v1/face/users').json()['users']
    assert users[0]['name'] == 'legacy' and users[0]['role'] == 'unassigned'
    auth = client.post('/v1/face/authenticate').json()
    assert auth['authenticated'] and auth['name'] == 'legacy'


def test_enroll_validation(client):
    assert enroll(client, '', 'driver').status_code == 400
    assert enroll(client, 'x' * 40, 'driver').status_code == 400
    assert enroll(client, 'Alice', 'pilot').status_code == 400


def test_enroll_failure_is_readable(client, svc):
    svc.face_auth.present = None
    r = enroll(client, 'Alice', 'driver')
    assert r.status_code == 200 and r.json()['enrolled'] is False
    assert r.json()['message'] == status_text('NoFaceDetected')
    assert client.get('/v1/face/users').json()['users'] == []


def test_face_unavailable_returns_503(svc):
    svc.face_auth = None
    client = TestClient(create_app(svc))
    response = client.post('/v1/face/authenticate')
    assert response.status_code == 503
    assert response.json()['error']['code'] == 'http_503'


def test_status_text_and_ids():
    assert status_text('Spoof') == 'Spoof suspected - remove glasses / avoid backlight'
    assert status_text('FaceIsNotFrontal') == 'Look straight at the camera'
    assert status_text('SomethingNew') == 'Something New'
    uid = new_user_id('Álvaro Müller 9')
    assert uid[0].isalpha() and len(uid) <= 30
    assert new_user_id('123') .startswith('u123_')


def test_roster_persists_atomically(tmp_path):
    path = str(tmp_path / 'sub' / 'r.json')
    FaceRoster(path).add('a_1', 'Alice', 'driver')
    again = FaceRoster(path)
    assert again.get('a_1')['name'] == 'Alice'
    assert again.find('ALICE', 'driver') == 'a_1' and again.drivers() == ['a_1']
    assert again.remove('a_1') and not again.remove('a_1')
