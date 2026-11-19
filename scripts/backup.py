"""Create a consistent SQLite backup without replacing existing files."""
from contextlib import closing
import argparse
import os
from pathlib import Path
import sqlite3

ROOT = Path(__file__).resolve().parents[1]


def backup(source, output):
    source, output = Path(source).resolve(), Path(output).absolute()
    if not source.is_file():
        raise FileNotFoundError(f'База не найдена: {source}')
    # Exclusive creation also refuses a destination symlink or the source itself.
    fd = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as src:
            with closing(sqlite3.connect(output)) as dst:
                src.backup(dst)
                if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise sqlite3.DatabaseError('Проверка целостности копии не пройдена')
    except Exception:
        output.unlink()  # Only the new file created by this call.
        raise
    return output


def main():
    parser = argparse.ArgumentParser(description='Резервная копия базы Контур')
    parser.add_argument('--source', type=Path, default=Path(os.environ.get('KONTUR_DATA_DIR', ROOT / 'data')) / 'kontur.sqlite')
    parser.add_argument('--output', type=Path, required=True, help='Новый файл; существующий не перезаписывается')
    args = parser.parse_args()
    try:
        result = backup(args.source, args.output)
    except (OSError, sqlite3.Error) as error:
        parser.exit(1, f'Копия не создана: {error}\n')
    print(f'Копия создана: {result}')


if __name__ == '__main__':
    main()
