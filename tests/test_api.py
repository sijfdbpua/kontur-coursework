"""Isolated API tests: independent sessions model two connected devices."""
import csv
import io
import sqlite3
from contextlib import closing
import pytest
from app import create_app, seed_demo

@pytest.fixture(scope='session')
def seeded_database(tmp_path_factory):
    path = tmp_path_factory.mktemp('seed') / 'demo.sqlite'
    seed_demo(path)
    return path

@pytest.fixture
def app(tmp_path, seeded_database):
    path = tmp_path / 'test.sqlite'
    # Seed data may still be in WAL; copy through SQLite, not the main file alone.
    with closing(sqlite3.connect(seeded_database)) as source:
        with closing(sqlite3.connect(path)) as destination:
            source.backup(destination)
    return create_app({'TESTING': True, 'SECRET_KEY': 'test-only-key', 'DATABASE': str(path)})

def login(app, username='dispatcher', password='demo-dispatch-2026'):
    client = app.test_client()
    token = client.get('/api/session').json['csrf_token']
    response = client.post('/api/login', json={'username': username, 'password': password}, headers={'X-CSRF-Token': token})
    assert response.status_code == 200
    client.csrf = response.json['csrf_token']
    return client

def mutate(client, method, url, values):
    return getattr(client, method)(url, json=values, headers={'X-CSRF-Token': client.csrf})

def new_ticket(client, **overrides):
    values = {'title': 'Проверочная заявка', 'description': 'Описание неисправности', 'asset_id': 1, 'priority': 'normal'}
    values.update(overrides)
    response = mutate(client, 'post', '/api/tickets', values)
    assert response.status_code == 201, response.json
    return response.json['ticket']

def patch(client, ticket, **changes):
    return mutate(client, 'patch', f"/api/tickets/{ticket['id']}", {'version': ticket['version'], **changes})

def test_anonymous_cannot_read(app):
    client = app.test_client()
    assert client.get('/api/session').json['user'] is None
    for url in ['/api/tickets', '/api/tickets/1', '/api/meta', '/api/export.csv']:
        assert client.get(url).status_code == 401

def test_csrf_required_for_login_and_mutations(app):
    client = app.test_client()
    assert client.post('/api/login', json={'username': 'dispatcher', 'password': 'demo-dispatch-2026'}).status_code == 403
    client = login(app)
    assert client.post('/api/tickets', json={}).status_code == 403
    assert client.post('/api/logout', json={}, headers={'X-CSRF-Token': 'incorrect'}).status_code == 403

def test_login_rotation_and_logout(app):
    client = app.test_client()
    old = client.get('/api/session').json['csrf_token']
    result = client.post('/api/login', json={'username': 'dispatcher', 'password': 'demo-dispatch-2026'}, headers={'X-CSRF-Token': old})
    assert result.json['csrf_token'] != old
    client.csrf = result.json['csrf_token']
    assert result.json['user']['role'] == 'dispatcher'
    assert mutate(client, 'post', '/api/logout', {}).status_code == 200
    assert client.get('/api/tickets').status_code == 401

def test_login_throttle(app):
    client = app.test_client()
    token = client.get('/api/session').json['csrf_token']
    for _ in range(10):
        assert client.post('/api/login', json={'username': 'missing', 'password': 'wrong'}, headers={'X-CSRF-Token': token}).status_code == 401
    assert client.post('/api/login', json={'username': 'missing', 'password': 'wrong'}, headers={'X-CSRF-Token': token}).status_code == 429

def test_origin_rejected(app):
    client = login(app)
    assert client.post('/api/logout', json={}, headers={'X-CSRF-Token': client.csrf, 'Origin': 'https://evil.example'}).status_code == 403
    assert client.get('/api/session').json['user'] is not None

def test_two_devices_full_workflow(app):
    dispatcher = login(app)
    technician = login(app, 'tech', 'demo-tech-2026')
    ticket = new_ticket(dispatcher)
    assert ticket['status'] == 'new'
    ticket = patch(dispatcher, ticket, assignee_id=2).json['ticket']
    assert ticket['status'] == 'assigned'
    device_view = technician.get(f"/api/tickets/{ticket['id']}").json['ticket']
    assert device_view == ticket
    ticket = patch(technician, device_view, status='in_progress').json['ticket']
    assert mutate(technician, 'post', f"/api/tickets/{ticket['id']}/comments", {'body': 'Проверяю подключение.'}).status_code == 201
    assert patch(technician, ticket, status='done').status_code == 400
    ticket = patch(technician, ticket, status='done', resolution='Заменён неисправный кабель.').json['ticket']
    assert ticket['resolution'] == 'Заменён неисправный кабель.'
    ticket = patch(dispatcher, ticket, status='closed').json['ticket']
    assert ticket['status'] == 'closed' and ticket['version'] == 5
    events = dispatcher.get(f"/api/tickets/{ticket['id']}").json['events']
    assert any(e['kind'] == 'resolution' and e['actor_name'] == 'Михаил Волков' for e in events)
    assert any(e['body'] == 'Проверяю подключение.' for e in events)

def test_reopen_retains_assignment_and_preserves_history(app):
    client = login(app)
    url = '/api/tickets/6'
    original = client.get(url).json
    results = [event for event in original['events'] if event['kind'] == 'resolution']
    assert len(results) == 1
    ticket = patch(client, original['ticket'], priority='high').json['ticket']
    assert ticket['status'] == 'done'
    ticket = patch(client, ticket, status='closed').json['ticket']
    reopened = patch(client, ticket, status='assigned').json['ticket']
    assert reopened['assignee_id'] == ticket['assignee_id'] and reopened['resolution'] == ''
    assert [event for event in client.get(url).json['events'] if event['kind'] == 'resolution'] == results

def test_optimistic_conflict_preserves_newer_data(app):
    first, second = login(app), login(app)
    original = new_ticket(first)
    updated = patch(first, original, priority='high').json['ticket']
    assert patch(second, original, priority='low').status_code == 409
    assert first.get(f"/api/tickets/{original['id']}").json['ticket'] == updated
    assert len(first.get(f"/api/tickets/{original['id']}").json['events']) == 2

def test_technician_only_own_tickets(app):
    client = login(app, 'tech', 'demo-tech-2026')
    rows = client.get('/api/tickets').json['tickets']
    assert rows and all(row['assignee_id'] == 2 for row in rows)
    assert client.get('/api/tickets/3').status_code == 404
    assert mutate(client, 'post', '/api/tickets/3/comments', {'body': 'Чужая заявка'}).status_code == 404
    assert mutate(client, 'patch', '/api/tickets/3', {'version': 1, 'status': 'done', 'resolution': 'x'}).status_code == 404

def test_technician_cannot_create_export_assign_or_prioritize(app):
    client = login(app, 'tech', 'demo-tech-2026')
    assert mutate(client, 'post', '/api/tickets', {}).status_code == 403
    assert client.get('/api/export.csv').status_code == 403
    ticket = client.get('/api/tickets/2').json['ticket']
    assert patch(client, ticket, assignee_id=3).status_code == 403
    assert patch(client, ticket, priority='low').status_code == 403

@pytest.mark.parametrize('ticket_id,status', [(1, 'closed'), (2, 'done'), (4, 'closed'), (7, 'done')])
def test_dispatcher_cannot_skip_lifecycle(app, ticket_id, status):
    client = login(app)
    ticket = client.get(f'/api/tickets/{ticket_id}').json['ticket']
    assert patch(client, ticket, status=status).status_code == 400
    assert client.get(f'/api/tickets/{ticket_id}').json['ticket']['version'] == ticket['version']

@pytest.mark.parametrize('values', [{'title': ''}, {'title': 'x' * 161}, {'title': []}, {'description': None}, {'asset_id': True}, {'asset_id': 999999}, {'asset_id': 2**100}, {'priority': []}, {'priority': 'urgent'}, {'assignee_id': 1}, {'assignee_id': '2'}])
def test_creation_validation(app, values):
    client = login(app)
    data = {'title': 'Тест', 'description': 'Описание', 'asset_id': 1, 'priority': 'normal', **values}
    assert mutate(client, 'post', '/api/tickets', data).status_code == 400
    assert len(client.get('/api/tickets').json['tickets']) == 12

def test_bad_patch_does_not_partially_change_data(app):
    client = login(app)
    ticket = client.get('/api/tickets/2').json['ticket']
    assert patch(client, ticket, priority='low', status='closed').status_code == 400
    assert client.get('/api/tickets/2').json['ticket'] == ticket

def test_assignment_constraints(app):
    client = login(app)
    ticket = client.get('/api/tickets/4').json['ticket']
    assert patch(client, ticket, assignee_id=3).status_code == 400
    ticket = client.get('/api/tickets/2').json['ticket']
    result = patch(client, ticket, assignee_id=None).json['ticket']
    assert result['assignee_id'] is None and result['status'] == 'new'
    assert patch(client, result, assignee_id=3, status='assigned').status_code == 400

def test_comments_append_without_changing_edit_version(app):
    client = login(app)
    ticket = client.get('/api/tickets/2').json['ticket']
    assert mutate(client, 'post', '/api/tickets/2/comments', {'body': '  '}).status_code == 400
    assert mutate(client, 'post', '/api/tickets/2/comments', {'body': 'Примечание'}).status_code == 201
    assert patch(client, ticket, priority='high').status_code == 200
    assert mutate(client, 'delete', '/api/tickets/2', {}).status_code == 405

def test_csv_formula_injection_and_unicode(app):
    client = login(app)
    new_ticket(client, title='=HYPERLINK("https://example.com")')
    response = client.get('/api/export.csv')
    assert response.status_code == 200 and response.data.startswith(b'\xef\xbb\xbf')
    rows = list(csv.reader(io.StringIO(response.data.decode('utf-8-sig')), delimiter=';'))
    assert rows[0][1] == 'Название' and rows[-1][1].startswith("'=HYPERLINK")
    assert 'attachment' in response.headers['Content-Disposition']

def test_persistence_on_restart_and_seed_refuses_overwrite(app):
    client = login(app)
    ticket = new_ticket(client)
    with pytest.raises(ValueError):
        seed_demo(app.config['DATABASE'])
    restarted = create_app({'TESTING': True, 'SECRET_KEY': 'test-only-key', 'DATABASE': app.config['DATABASE']})
    assert login(restarted).get(f"/api/tickets/{ticket['id']}").json['ticket']['title'] == ticket['title']

def test_passwords_hashed_and_foreign_keys_enforced(app):
    with sqlite3.connect(app.config['DATABASE']) as db:
        value = db.execute('SELECT password_hash FROM users WHERE id=1').fetchone()[0]
        assert value != 'demo-dispatch-2026' and value.startswith('scrypt:')
        db.execute('PRAGMA foreign_keys=ON')
        with pytest.raises(sqlite3.IntegrityError):
            db.execute('UPDATE tickets SET asset_id=9999 WHERE id=1')

def test_json_shape_unknown_fields_and_size(app):
    client = login(app)
    assert client.get('/api/tickets/' + str(2**100)).status_code == 400
    assert mutate(client, 'post', '/api/tickets', []).status_code == 400
    assert mutate(client, 'post', '/api/tickets', {'unexpected': 1}).status_code == 400
    assert mutate(client, 'post', '/api/tickets', {'description': 'x' * 40000}).status_code == 413

def test_security_headers(app):
    response = app.test_client().get('/api/session')
    assert response.headers['Cache-Control'] == 'no-store'
    assert response.headers['X-Content-Type-Options'] == 'nosniff'
    assert response.headers['X-Frame-Options'] == 'DENY'
    assert 'HttpOnly' in response.headers['Set-Cookie'] and 'SameSite=Lax' in response.headers['Set-Cookie']
