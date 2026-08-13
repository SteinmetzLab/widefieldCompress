"""Read the TrueNAS Cloud Sync Task configuration. Strictly read-only.

The B2 backup is a Cloud Sync Task, and its settings live in the TrueNAS config database rather
than an rclone.conf. That database is world-readable, so the settings can be inspected without
root - which answers "was --transfers ever changed?" without asking anyone.

Opens the database in SQLite read-only mode and prints nothing that looks like a credential.
Makes no modification of any kind.
"""

# ruff: noqa: UP031
# Percent formatting: this file runs on the appliance under an interpreter this repo never sees.

import json
import sqlite3
import sys

DB = "/data/freenas-v1.db"
SECRETY = ("pass", "secret", "key", "token", "attributes", "encryption")


def redact(name, value):
    low = name.lower()
    if any(s in low for s in SECRETY):
        return "<redacted>"
    return value


def main():
    con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    tables = [r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%cloud%'")]
    print("cloud-related tables: %s\n" % ", ".join(tables))

    for t in tables:
        try:
            rows = list(cur.execute("SELECT * FROM %s" % t))
        except sqlite3.Error as e:
            print("%s: %s" % (t, e))
            continue
        if not rows:
            continue
        print("=" * 70)
        print("%s  (%d row%s)" % (t, len(rows), "" if len(rows) == 1 else "s"))
        for r in rows:
            print("-" * 70)
            for k in r.keys():
                v = redact(k, r[k])
                if isinstance(v, str) and len(v) > 300:
                    v = v[:300] + " ...[truncated]"
                print("  %-28s %s" % (k, v))

    # the flags we actually care about often live in a JSON blob
    print("\n" + "=" * 70)
    print("looking for transfer/bandwidth settings in any JSON column")
    for t in tables:
        try:
            rows = list(cur.execute("SELECT * FROM %s" % t))
        except sqlite3.Error:
            continue
        for r in rows:
            for k in r.keys():
                v = r[k]
                if not isinstance(v, str) or not v.strip().startswith(("{", "[")):
                    continue
                try:
                    blob = json.loads(v)
                except ValueError:
                    continue
                if isinstance(blob, dict):
                    for kk, vv in blob.items():
                        if any(s in kk.lower() for s in
                               ("transfer", "bwlimit", "bandwidth", "chunk", "fast", "checker")):
                            print("  %s.%s -> %s = %r" % (t, k, kk, vv))
    con.close()
    print("\nread-only; nothing was modified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
