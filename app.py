"""Контур: учебная система обслуживания оборудования (Flask + SQLite)."""
from __future__ import annotations

import argparse
import csv
import io
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Flask, g, jsonify, request, session
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

ROOT = Path(__file__).resolve().parent
PRIORITIES = {'low', 'normal', 'high', 'critical'}
STATUSES = {'new', 'assigned', 'in_progress', 'done', 'closed'}
STATUS_NAMES = {'new': 'Новая', 'assigned': 'Назначена', 'in_progress': 'В работе',
                'done': 'Выполнена', 'closed': 'Закрыта'}
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
 id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
 role TEXT NOT NULL CHECK(role IN ('dispatcher','technician')), password_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assets (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, location TEXT NOT NULL,
 inventory_no TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS tickets (
 id INTEGER PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL,
 asset_id INTEGER NOT NULL REFERENCES assets(id),
 priority TEXT NOT NULL CHECK(priority IN ('low','normal','high','critical')),
 status TEXT NOT NULL CHECK(status IN ('new','assigned','in_progress','done','closed')),
 assignee_id INTEGER REFERENCES users(id), created_by INTEGER NOT NULL REFERENCES users(id),
 resolution TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 version INTEGER NOT NULL DEFAULT 1,
 CHECK((status='new' AND assignee_id IS NULL) OR (status!='new' AND assignee_id IS NOT NULL))
);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY, ticket_id INTEGER NOT NULL REFERENCES tickets(id),
 actor_id INTEGER NOT NULL REFERENCES users(id), kind TEXT NOT NULL,
 body TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tickets_assignee ON tickets(assignee_id, status);
CREATE INDEX IF NOT EXISTS events_ticket ON events(ticket_id, id);
"""
TICKET_SELECT = """SELECT t.*, a.name asset_name, a.location, a.inventory_no,
 u.name assignee_name FROM tickets t JOIN assets a ON a.id=t.asset_id
 LEFT JOIN users u ON u.id=t.assignee_id"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class APIError(Exception):
    def __init__(self, message, status=400):
        self.message, self.status = message, status


def connect(path):
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA busy_timeout=10000')
    return db


def initialize(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript(SCHEMA)


def create_app(config=None):
    app = Flask(__name__, static_folder='static')
    data = Path(os.environ.get('KONTUR_DATA_DIR', ROOT / 'data'))
    data.mkdir(parents=True, exist_ok=True)
    # The secret must survive a server restart, but must never go into Git.
    secret_path = data / 'session.key'
    if config and config.get('SECRET_KEY'):
        secret = config['SECRET_KEY']
    else:
        try:
            fd = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            secret = secret_path.read_text().strip()
        else:
            secret = secrets.token_hex(32)
            with os.fdopen(fd, 'w') as f:
                f.write(secret)
    app.config.update(SECRET_KEY=secret, DATABASE=str(data / 'kontur.sqlite'),
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
                      SESSION_COOKIE_SECURE=os.environ.get('KONTUR_HTTPS') == '1',
                      PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
                      MAX_CONTENT_LENGTH=32768)
    if config:
        app.config.update(config)
    initialize(app.config['DATABASE'])
    failures = {}
    failure_lock = threading.Lock()

    def db():
        if 'db' not in g:
            g.db = connect(app.config['DATABASE'])
        return g.db

    @app.teardown_appcontext
    def close_db(_error):
        connection = g.pop('db', None)
        if connection:
            connection.close()

    def csrf():
        if 'csrf' not in session:
            session['csrf'] = secrets.token_urlsafe(32)
        return session['csrf']

    def current_user():
        row = db().execute('SELECT id,name,username,role FROM users WHERE id=?',
                           (session.get('user_id'),)).fetchone()
        return dict(row) if row else None

    @app.before_request
    def protect_mutations():
        if request.path.startswith('/api/') and request.method in {'POST', 'PATCH', 'PUT', 'DELETE'}:
            token = request.headers.get('X-CSRF-Token', '')
            if not token or not secrets.compare_digest(token, session.get('csrf', '')):
                raise APIError('Сессия устарела. Обнови страницу и повтори действие.', 403)
            origin = request.headers.get('Origin')
            if origin and origin.rstrip('/') != request.host_url.rstrip('/'):
                raise APIError('Запрос с другого сайта отклонён.', 403)

    @app.after_request
    def headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = ("default-src 'self'; img-src 'self' data:; "
            "style-src 'self'; script-src 'self'; connect-src 'self'; object-src 'none'; "
            "base-uri 'self'; frame-ancestors 'none'; form-action 'self'")
        if request.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.errorhandler(APIError)
    def api_error(error):
        return jsonify(error=error.message), error.status

    @app.errorhandler(HTTPException)
    def http_error(error):
        messages = {400: 'Некорректный запрос.', 404: 'Не найдено.', 405: 'Метод не поддерживается.',
                    413: 'Запрос слишком большой.', 415: 'Ожидается JSON.'}
        return jsonify(error=messages.get(error.code, 'Ошибка HTTP.')), error.code

    @app.errorhandler(sqlite3.Error)
    def database_error(error):
        app.logger.exception('Database error')
        return jsonify(error='База данных временно недоступна. Повтори запрос.'), 503

    def require_user(role=None):
        def decorator(fn):
            @wraps(fn)
            def wrapped(*args, **kwargs):
                g.user = current_user()
                if not g.user:
                    raise APIError('Войди в систему.', 401)
                if role and g.user['role'] != role:
                    raise APIError('Недостаточно прав для этого действия.', 403)
                return fn(*args, **kwargs)
            return wrapped
        return decorator

    def body(allowed):
        value = request.get_json()
        if not isinstance(value, dict):
            raise APIError('Ожидается JSON-объект.')
        if set(value) - set(allowed):
            raise APIError('Запрос содержит неизвестные поля.')
        return value

    def text_field(value, name, maximum, minimum=1):
        if not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum:
            raise APIError(f'{name}: требуется текст длиной от {minimum} до {maximum} символов.')
        value = value.strip()
        if any(ord(c) < 32 and c not in '\n\r\t' for c in value):
            raise APIError(f'{name}: недопустимые управляющие символы.')
        return value

    def integer(value, name):
        if type(value) is not int or not 1 <= value <= 9223372036854775807:
            raise APIError(f'{name}: требуется положительное целое число.')
        return value

    def assignee(value):
        if value is None:
            return None
        integer(value, 'Исполнитель')
        row = db().execute("SELECT id FROM users WHERE id=? AND role='technician'", (value,)).fetchone()
        if not row:
            raise APIError('Выбери существующего техника.')
        return value

    def get_ticket(ticket_id):
        integer(ticket_id, 'Номер заявки')
        row = db().execute(TICKET_SELECT + ' WHERE t.id=?', (ticket_id,)).fetchone()
        if not row:
            raise APIError('Заявка не найдена.', 404)
        ticket = dict(row)
        if g.user['role'] == 'technician' and ticket['assignee_id'] != g.user['id']:
            raise APIError('Заявка не найдена.', 404)
        return ticket

    def event(ticket_id, kind, content):
        cursor = db().execute('INSERT INTO events(ticket_id,actor_id,kind,body,created_at) VALUES(?,?,?,?,?)',
                             (ticket_id, g.user['id'], kind, content, now()))
        return cursor.lastrowid

    @app.get('/')
    def index():
        return app.send_static_file('index.html')

    @app.get('/api/session')
    def get_session():
        return jsonify(user=current_user(), csrf_token=csrf())

    @app.post('/api/login')
    def login():
        values = body({'username', 'password'})
        username = text_field(values.get('username'), 'Логин', 80)
        password = text_field(values.get('password'), 'Пароль', 200)
        # Bounded in-memory throttling, suitable for a single-process LAN deployment.
        address = request.remote_addr or 'unknown'
        timestamp = time.monotonic()
        with failure_lock:
            for key in list(failures):
                failures[key] = [t for t in failures[key] if timestamp - t < 300]
                if not failures[key]:
                    del failures[key]
            if len(failures.get(address, [])) >= 10:
                raise APIError('Слишком много попыток входа. Подожди 5 минут.', 429)
            if len(failures) >= 1000 and address not in failures:
                raise APIError('Слишком много попыток входа. Попробуй позже.', 429)
            failures.setdefault(address, []).append(timestamp)
        row = db().execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
        if not row or not check_password_hash(row['password_hash'], password):
            raise APIError('Неверный логин или пароль.', 401)
        with failure_lock:
            failures.pop(address, None)
        session.clear()
        session['user_id'] = row['id']
        session.permanent = True
        return jsonify(user=current_user(), csrf_token=csrf())

    @app.post('/api/logout')
    def logout():
        body(set())
        session.clear()
        return jsonify(user=None, csrf_token=csrf())

    @app.get('/api/meta')
    @require_user()
    def meta():
        return jsonify(users=[dict(r) for r in db().execute('SELECT id,name,role FROM users ORDER BY name')],
                       assets=[dict(r) for r in db().execute('SELECT * FROM assets ORDER BY name')])

    @app.get('/api/tickets')
    @require_user()
    def list_tickets():
        query, params = TICKET_SELECT, ()
        if g.user['role'] == 'technician':
            query += ' WHERE t.assignee_id=?'
            params = (g.user['id'],)
        return jsonify(tickets=[dict(r) for r in db().execute(query + ' ORDER BY t.updated_at DESC,t.id DESC', params)])

    @app.get('/api/tickets/<int:ticket_id>')
    @require_user()
    def detail(ticket_id):
        ticket = get_ticket(ticket_id)
        events = db().execute('''SELECT e.id,e.kind,e.body,e.created_at,u.name actor_name
                                 FROM events e JOIN users u ON u.id=e.actor_id
                                 WHERE e.ticket_id=? ORDER BY e.id''', (ticket_id,))
        return jsonify(ticket=ticket, events=[dict(r) for r in events])

    @app.post('/api/tickets')
    @require_user('dispatcher')
    def create_ticket():
        values = body({'title', 'description', 'asset_id', 'priority', 'assignee_id'})
        title = text_field(values.get('title'), 'Название', 160)
        description = text_field(values.get('description'), 'Описание', 4000)
        asset_id = integer(values.get('asset_id'), 'Оборудование')
        if not db().execute('SELECT id FROM assets WHERE id=?', (asset_id,)).fetchone():
            raise APIError('Оборудование не найдено.')
        priority = values.get('priority', 'normal')
        if not isinstance(priority, str) or priority not in PRIORITIES:
            raise APIError('Некорректный приоритет.')
        assigned = assignee(values.get('assignee_id'))
        timestamp = now()
        with db():
            cursor = db().execute('''INSERT INTO tickets(title,description,asset_id,priority,status,
                assignee_id,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)''',
                (title, description, asset_id, priority, 'assigned' if assigned else 'new',
                 assigned, g.user['id'], timestamp, timestamp))
            ticket_id = cursor.lastrowid
            event(ticket_id, 'created', 'Заявка зарегистрирована' + (' и назначена исполнителю.' if assigned else '.'))
        return jsonify(ticket=get_ticket(ticket_id)), 201

    @app.patch('/api/tickets/<int:ticket_id>')
    @require_user()
    def update_ticket(ticket_id):
        values = body({'version', 'assignee_id', 'priority', 'status', 'resolution'})
        version = integer(values.get('version'), 'Версия')
        if len(values) == 1:
            raise APIError('Не указаны изменения.')
        # BEGIN IMMEDIATE keeps permission checks and update in the same transaction.
        db().execute('BEGIN IMMEDIATE')
        try:
            ticket = get_ticket(ticket_id)
            if ticket['version'] != version:
                raise APIError('Заявка уже изменена на другом устройстве. Обнови её и повтори действие.', 409)
            updated = dict(ticket)
            role = g.user['role']
            if role == 'technician' and set(values) - {'version', 'status', 'resolution'}:
                raise APIError('Техник может изменять только статус своей заявки и результат работы.', 403)
            if 'priority' in values:
                if ticket['status'] == 'closed':
                    raise APIError('Сначала верни закрытую заявку в работу.')
                if not isinstance(values['priority'], str) or values['priority'] not in PRIORITIES:
                    raise APIError('Некорректный приоритет.')
                updated['priority'] = values['priority']
            if 'assignee_id' in values:
                if ticket['status'] not in {'new', 'assigned'}:
                    raise APIError('Исполнителя можно менять только до начала работы.')
                updated['assignee_id'] = assignee(values['assignee_id'])
                updated['status'] = 'assigned' if updated['assignee_id'] else 'new'
            if 'status' in values:
                status = values['status']
                if not isinstance(status, str) or status not in STATUSES:
                    raise APIError('Некорректный статус.')
                transitions = ({'assigned': {'in_progress'}, 'in_progress': {'done'}} if role == 'technician'
                               else {'done': {'closed', 'assigned'}, 'closed': {'assigned'}})
                # Assignment is its own action: avoid ambiguous compound transitions.
                if 'assignee_id' in values or status not in transitions.get(ticket['status'], set()):
                    raise APIError('Такой переход статуса недопустим.')
                updated['status'] = status
                if status == 'done':
                    updated['resolution'] = text_field(values.get('resolution'), 'Результат работы', 4000)
                elif 'resolution' in values:
                    raise APIError('Результат указывается только при завершении работы.')
                if status == 'assigned':
                    updated['resolution'] = ''
            elif 'resolution' in values:
                raise APIError('Результат указывается только при завершении работы.')
            changes = [k for k in ('assignee_id', 'priority', 'status', 'resolution') if updated[k] != ticket[k]]
            if not changes:
                raise APIError('Заявка не изменилась.')
            cursor = db().execute('''UPDATE tickets SET assignee_id=?,priority=?,status=?,resolution=?,
                updated_at=?,version=version+1 WHERE id=? AND version=?''',
                (updated['assignee_id'], updated['priority'], updated['status'], updated['resolution'],
                 now(), ticket_id, version))
            if cursor.rowcount != 1:
                raise APIError('Конфликт версий. Обнови заявку.', 409)
            if 'assignee_id' in changes:
                name = db().execute('SELECT name FROM users WHERE id=?', (updated['assignee_id'],)).fetchone()
                event(ticket_id, 'assignment', f"Исполнитель: {name['name'] if name else 'не назначен'}.")
            if 'priority' in changes:
                names = {'low': 'Низкий', 'normal': 'Обычный', 'high': 'Высокий', 'critical': 'Критический'}
                event(ticket_id, 'priority', f"Приоритет: {names[updated['priority']]}.")
            if 'status' in changes:
                event(ticket_id, 'status', f"{STATUS_NAMES[ticket['status']]} → {STATUS_NAMES[updated['status']]}.")
                if updated['status'] == 'done':
                    event(ticket_id, 'resolution', updated['resolution'])
            db().commit()
        except Exception:
            db().rollback()
            raise
        return jsonify(ticket=get_ticket(ticket_id))

    @app.post('/api/tickets/<int:ticket_id>/comments')
    @require_user()
    def add_comment(ticket_id):
        values = body({'body'})
        content = text_field(values.get('body'), 'Комментарий', 2000)
        db().execute('BEGIN IMMEDIATE')
        try:
            get_ticket(ticket_id)
            event_id = event(ticket_id, 'comment', content)
            db().commit()
        except Exception:
            db().rollback()
            raise
        row = db().execute('''SELECT e.id,e.kind,e.body,e.created_at,u.name actor_name FROM events e
                             JOIN users u ON u.id=e.actor_id WHERE e.id=?''', (event_id,)).fetchone()
        return jsonify(event=dict(row)), 201

    @app.get('/api/export.csv')
    @require_user('dispatcher')
    def export_csv():
        output = io.StringIO(newline='')
        writer = csv.writer(output, delimiter=';')
        writer.writerow(['Номер', 'Название', 'Оборудование', 'Место', 'Приоритет', 'Статус',
                         'Исполнитель', 'Создана (UTC)', 'Обновлена (UTC)', 'Результат'])
        def safe(value):
            value = '' if value is None else str(value)
            return "'" + value if value.lstrip().startswith(('=', '+', '-', '@')) else value
        for row in db().execute(TICKET_SELECT + ' ORDER BY t.id'):
            writer.writerow([safe(row[k]) for k in ('id', 'title', 'asset_name', 'location', 'priority',
                             'status', 'assignee_name', 'created_at', 'updated_at', 'resolution')])
        return app.response_class('\ufeff' + output.getvalue(), mimetype='text/csv',
                                  headers={'Content-Disposition': 'attachment; filename=kontur-tickets.csv'})

    return app


def seed_demo(path):
    """Synthetic demonstration only; never overwrite a populated database."""
    initialize(path)
    with connect(path) as db:
        if db.execute('SELECT count(*) FROM users').fetchone()[0]:
            raise ValueError('База уже содержит пользователей. Демо-данные не добавлены.')
        users = [(1, 'dispatcher', 'Анна Смирнова', 'dispatcher', 'demo-dispatch-2026'),
                 (2, 'tech', 'Михаил Волков', 'technician', 'demo-tech-2026'),
                 (3, 'tech2', 'Елена Орлова', 'technician', 'demo-tech2-2026')]
        db.executemany('INSERT INTO users VALUES(?,?,?,?,?)',
                       [(i, login, name, role, generate_password_hash(password)) for i, login, name, role, password in users])
        assets = [(1, 'Проектор Epson EB-X49', 'Корпус А · аудитория 204', 'PR-0204'),
                  (2, 'Рабочая станция № 12', 'Корпус Б · компьютерный класс 108', 'PC-0112'),
                  (3, 'Принтер HP LaserJet', 'Корпус А · преподавательская', 'PT-0031'),
                  (4, 'Точка доступа Wi-Fi', 'Библиотека · читальный зал', 'NW-0008'),
                  (5, 'Учебный стенд автоматики', 'Лаборатория · кабинет 302', 'LB-0017'),
                  (6, 'Мультимедийная панель', 'Корпус В · аудитория 105', 'DS-0004')]
        db.executemany('INSERT INTO assets VALUES(?,?,?,?)', assets)
        examples = [
            ('Нет изображения с проектора', 'При подключении по HDMI проектор не определяет источник. Проверить кабель и выбранный вход.', 1, 'high', 'new', None),
            ('Компьютер не загружает систему', 'После включения появляется сообщение об отсутствии загрузочного устройства.', 2, 'critical', 'assigned', 2),
            ('Полосы при печати', 'На распечатках заметна вертикальная полоса. Требуется осмотр картриджа.', 3, 'normal', 'in_progress', 3),
            ('Пропадает подключение к сети', 'В читальном зале периодически теряется соединение. Проверить настройки точки доступа.', 4, 'high', 'in_progress', 2),
            ('Проверка стенда перед практикумом', 'Провести визуальный осмотр и проверить комплектность учебного стенда.', 5, 'normal', 'assigned', 3),
            ('Калибровка сенсорной панели', 'Касание регистрируется со смещением. Выполнить калибровку.', 6, 'low', 'done', 2),
            ('Профилактическая очистка проектора', 'Плановое обслуживание фильтра. Работы выполнять согласно инструкции производителя.', 1, 'low', 'closed', 3),
            ('Не работает клавиатура', 'Не реагирует цифровой блок клавиатуры. Проверить настройки и подключение.', 2, 'normal', 'new', None),
            ('Настройка двусторонней печати', 'Проверить доступность дуплексного режима в драйвере.', 3, 'low', 'assigned', 2),
            ('Обновить схему подключения стенда', 'На рабочем месте отсутствует актуальная памятка по подключению.', 5, 'normal', 'done', 3),
            ('Нет звука на панели', 'Изображение выводится, звук отсутствует. Проверить выбранное устройство вывода.', 6, 'high', 'new', None),
            ('Проверка покрытия Wi-Fi', 'После перестановки мебели выполнить контроль подключения в дальнем ряду.', 4, 'normal', 'closed', 2),
        ]
        base = datetime.now(timezone.utc).replace(microsecond=0)
        for i, (title, desc, asset, priority, status, assigned) in enumerate(examples, 1):
            created = (base - timedelta(hours=36-i*2)).isoformat()
            resolution = 'Проверка завершена, работоспособность подтверждена. Учебный пример.' if status in {'done','closed'} else ''
            db.execute('''INSERT INTO tickets(id,title,description,asset_id,priority,status,assignee_id,
                created_by,resolution,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                (i,title,desc,asset,priority,status,assigned,1,resolution,created,created))
            db.execute('INSERT INTO events(ticket_id,actor_id,kind,body,created_at) VALUES(?,?,?,?,?)',
                       (i,1,'created','Учебная заявка импортирована в демонстрационном состоянии: ' + STATUS_NAMES[status] + '.',created))
            if resolution:
                db.execute('INSERT INTO events(ticket_id,actor_id,kind,body,created_at) VALUES(?,?,?,?,?)',
                           (i,assigned,'resolution',resolution,created))


def main():
    parser = argparse.ArgumentParser(description='Контур — обслуживание оборудования')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('init-demo', help='Создать синтетические демо-данные в пустой базе')
    serve = sub.add_parser('serve', help='Запустить сервер')
    serve.add_argument('--host', default='127.0.0.1')
    serve.add_argument('--port', type=int, default=8080)
    args = parser.parse_args()
    app = create_app()
    if args.command == 'init-demo':
        try:
            seed_demo(app.config['DATABASE'])
        except ValueError as error:
            parser.exit(1, str(error) + '\n')
        print('Только учебные данные. Не используй эти пароли в публичном сервисе.')
        print('dispatcher / demo-dispatch-2026\ntech / demo-tech-2026\ntech2 / demo-tech2-2026')
    else:
        from waitress import serve as run
        print(f'Контур: http://{args.host}:{args.port} (Ctrl+C — остановить)', flush=True)
        run(app, host=args.host, port=args.port, threads=4)


if __name__ == '__main__':
    main()
