from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3
import time


@dataclass(frozen=True)
class InventoryRecord:
    rule: str
    date: str
    relative_path: str
    local_path: str
    drive_path: str
    file_name: str
    status: str
    size: int | None = None
    mtime_ns: int | None = None
    drive_file_id: str | None = None
    drive_modified_time: str | None = None


class SyncInventory:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        self._init_schema()
        self.assert_writable()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SyncInventory":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def get(self, rule: str, date: str, relative_path: str) -> InventoryRecord | None:
        row = self._conn.execute(
            """
            select * from files
            where rule = ? and date = ? and relative_path = ?
            """,
            (rule, date, relative_path),
        ).fetchone()
        return _row_to_record(row) if row else None

    def upsert(self, record: InventoryRecord) -> None:
        now = int(time.time())
        self._conn.execute(
            """
            insert into files (
                rule, date, relative_path, local_path, drive_path, file_name, status,
                size, mtime_ns, drive_file_id, drive_modified_time, first_seen, last_seen
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(rule, date, relative_path) do update set
                local_path = excluded.local_path,
                drive_path = excluded.drive_path,
                file_name = excluded.file_name,
                status = excluded.status,
                size = excluded.size,
                mtime_ns = excluded.mtime_ns,
                drive_file_id = excluded.drive_file_id,
                drive_modified_time = excluded.drive_modified_time,
                last_seen = excluded.last_seen
            """,
            (
                record.rule,
                record.date,
                record.relative_path,
                record.local_path,
                record.drive_path,
                record.file_name,
                record.status,
                record.size,
                record.mtime_ns,
                record.drive_file_id,
                record.drive_modified_time,
                now,
                now,
            ),
        )
        self._conn.commit()

    def mark_remote_only(
        self,
        *,
        rule: str,
        date: str,
        relative_path: str,
        drive_path: str,
        file_name: str,
        drive_file_id: str | None,
        drive_modified_time: str | None,
    ) -> None:
        self.upsert(
            InventoryRecord(
                rule=rule,
                date=date,
                relative_path=relative_path,
                local_path="",
                drive_path=drive_path,
                file_name=file_name,
                status="remote_only",
                drive_file_id=drive_file_id,
                drive_modified_time=drive_modified_time,
            )
        )

    def records(self) -> list[InventoryRecord]:
        rows = self._conn.execute(
            "select * from files order by date, rule, relative_path"
        ).fetchall()
        return [_row_to_record(row) for row in rows]

    def assert_writable(self) -> None:
        try:
            self._conn.execute(
                """
                create table if not exists inventory_metadata (
                    key text primary key,
                    value text not null
                )
                """
            )
            self._conn.execute(
                """
                insert into inventory_metadata(key, value)
                values ('last_write_check', ?)
                on conflict(key) do update set value = excluded.value
                """,
                (str(int(time.time())),),
            )
            self._conn.commit()
        except sqlite3.OperationalError as exc:
            raise PermissionError(
                f"Sync inventory database is not writable: {self.path}. "
                "Use --inventory or MONSOON_SYNC_DB to select a writable SQLite file."
            ) from exc

    def _init_schema(self) -> None:
        self._conn.execute(
            """
            create table if not exists files (
                rule text not null,
                date text not null,
                relative_path text not null,
                local_path text not null,
                drive_path text not null,
                file_name text not null,
                status text not null,
                size integer,
                mtime_ns integer,
                drive_file_id text,
                drive_modified_time text,
                first_seen integer not null,
                last_seen integer not null,
                primary key (rule, date, relative_path)
            )
            """
        )
        self._conn.execute(
            "create index if not exists idx_files_status on files(status)"
        )
        self._conn.commit()


def _row_to_record(row: sqlite3.Row) -> InventoryRecord:
    return InventoryRecord(
        rule=row["rule"],
        date=row["date"],
        relative_path=row["relative_path"],
        local_path=row["local_path"],
        drive_path=row["drive_path"],
        file_name=row["file_name"],
        status=row["status"],
        size=row["size"],
        mtime_ns=row["mtime_ns"],
        drive_file_id=row["drive_file_id"],
        drive_modified_time=row["drive_modified_time"],
    )
