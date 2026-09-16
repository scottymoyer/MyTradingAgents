#!/usr/bin/env python3
"""S3 helpers for the containerized (Fargate) batch run.

Two jobs, both boto3-only (the image has no AWS CLI):

  * ``pull-watchlist`` — download ``s3://$BUCKET/config/watchlist.yaml`` over the
    baked-in default before the run. This is how the Fargate task gets the live
    (trimmed) watchlist WITHOUT baking it into the image or committing it to git:
    edit the list by re-uploading to S3, no rebuild. Missing/empty remote object
    is not an error — the run falls back to the image's baked watchlist.

  * ``push-reports`` — upload everything under ``~/.tradingagents/logs/reports`` to
    ``s3://$BUCKET/reports/`` (the boto3 equivalent of the ``aws s3 sync`` step in
    ``scheduled_run.sh``, preserving the ``reports/<TICKER>_<stamp>/…`` key layout
    that the ledger's ``report_path`` mirrors).

Bucket: ``$TRADINGAGENTS_S3_BUCKET`` (default the provisioned results bucket).
Region: ``$AWS_REGION`` / ``$AWS_DEFAULT_REGION`` (default us-east-2).

Neither subcommand ever raises: S3 problems must not fail a paid analysis run, so
they are logged and swallowed (exit 0). Import errors (no boto3) are logged too.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_DEFAULT_BUCKET = "tradingagents-963910217112-results"
_APP_DIR = Path(__file__).resolve().parent
_REPORTS_DIR = Path.home() / ".tradingagents" / "logs" / "reports"
_WATCHLIST_KEY = "config/watchlist.yaml"
_REPORTS_PREFIX = "reports/"


def _bucket() -> str:
    return os.environ.get("TRADINGAGENTS_S3_BUCKET", _DEFAULT_BUCKET)


def _region() -> str:
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-2"


def _client():
    import boto3  # imported lazily so this module loads even without boto3

    return boto3.client("s3", region_name=_region())


def pull_watchlist() -> int:
    """Overlay the baked watchlist.yaml with s3://<bucket>/config/watchlist.yaml.
    Returns 0 always (missing remote object is a normal fallback, not an error)."""
    dest = _APP_DIR / "watchlist.yaml"
    try:
        from botocore.exceptions import ClientError

        s3 = _client()
        try:
            obj = s3.get_object(Bucket=_bucket(), Key=_WATCHLIST_KEY)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "404", "NoSuchBucket"):
                print(f"s3_sync: no s3://{_bucket()}/{_WATCHLIST_KEY}; using baked watchlist")
                return 0
            raise
        body = obj["Body"].read()
        if not body.strip():
            print("s3_sync: remote watchlist is empty; using baked watchlist")
            return 0
        dest.write_bytes(body)
        print(f"s3_sync: pulled watchlist ({len(body)} bytes) -> {dest}")
    except Exception as e:  # never fail the run over config fetch
        print(f"s3_sync: pull-watchlist failed ({e!r}); using baked watchlist")
    return 0


def push_reports() -> int:
    """Upload every file under ~/.tradingagents/logs/reports to s3://<bucket>/reports/.
    Returns 0 always (a sync failure must not fail the run)."""
    try:
        if not _REPORTS_DIR.is_dir():
            print(f"s3_sync: no reports dir at {_REPORTS_DIR}; nothing to upload")
            return 0
        s3 = _client()
        bucket = _bucket()
        n = 0
        for path in sorted(_REPORTS_DIR.rglob("*")):
            if not path.is_file():
                continue
            key = _REPORTS_PREFIX + str(path.relative_to(_REPORTS_DIR)).replace(os.sep, "/")
            s3.upload_file(str(path), bucket, key)
            n += 1
        print(f"s3_sync: uploaded {n} report file(s) -> s3://{bucket}/{_REPORTS_PREFIX}")
    except Exception as e:  # never fail the run over the upload
        print(f"s3_sync: push-reports failed ({e!r})")
    return 0


def _main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "pull-watchlist":
        return pull_watchlist()
    if cmd == "push-reports":
        return push_reports()
    print("usage: s3_sync.py {pull-watchlist|push-reports}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
