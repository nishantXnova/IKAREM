# ikarem-backup — SQLite backups that prove they restore

Every company needs backups; the core has no I/O opinions, so they live
here. Online dumps of live SQLite files (`sqlite3` backup API, not file
copy), restore drills on every run (`integrity_check` + table list),
retention pruning, and a pluggable uploader (S3/GCS stay zero-dep).
Stdlib only.

```bash
pip install ./extensions/ikarem-backup
```

```python
from ikarem_backup import BackupManager

manifest = BackupManager("sqlite:///app.db", "backups/", keep=7).run()
# {"backup": "backups/app-20261009-120000.db.gz", "tables": [...], "pruned": [...], "uploaded": None}
```

Automation (nightly, after your queue drains):

```python
from ikarem_backup import BackupPlugin

app.register(BackupPlugin(dest_dir="backups", keep=7))  # POST /admin/backup runs the cycle
# guard it like any mutating route, then:
# @app.every(86400)
# async def nightly(): BackupManager("sqlite:///app.db", "backups/").run(uploader=push_to_s3)
```

S3 upload is a callable (boto3 stays your dependency, not ours):

```python
import boto3

s3 = boto3.client("s3")
manager.run(uploader=lambda p: s3.upload_file(str(p), "my-bucket", p.name))
```

Non-SQLite engines are refused with the native-dump fix (`pg_dump`,
`mysqldump`) instead of a traceback. `:memory:` databases are refused —
point `DatabasePlugin` at a file first.

## Removal path

Uninstall and you keep your database exactly as it was — this package
only reads it (online backup API, never locks writers out) plus writes
to its own directory. Replace with a cron `sqlite3 app.db .backup` if
you ever want fewer moving parts.
