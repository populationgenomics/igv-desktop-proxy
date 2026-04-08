import csv
import io
import logging
import os
from datetime import datetime, timedelta, timezone

import flask
import functions_framework
import redis
from google.cloud import storage

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s [%(filename)s:%(lineno)d] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)

STATS_KEY_PREFIX = 'dl_stats'
EXPIRE_AFTER_EXPORT_SECS = 300  # 5 minutes — allows any in-flight increments to finish


def _get_redis_client() -> redis.Redis:
    host = os.environ['REDIS_HOST']
    port = int(os.environ.get('REDIS_PORT', 6379))
    password = os.environ.get('REDIS_PASSWORD')
    url = f'redis://:{password}@{host}:{port}/0' if password else f'redis://{host}:{port}/0'
    return redis.Redis.from_pool(redis.ConnectionPool.from_url(url=url, max_connections=5, decode_responses=True))


def _scan_stats_keys(r: redis.Redis, date_str: str) -> list[str]:
    """Cursor-based scan for all dl_stats keys matching the given date."""
    pattern = f'{STATS_KEY_PREFIX}:*:{date_str}'
    return list(r.scan_iter(match=pattern, count=100))


def _parse_stats_key(key: str) -> tuple[str, str, str]:
    """Parse dl_stats:{user_id}:{bucket}:{date} into (user_id, bucket, date).
    """
    parts = key.split(':')
    date = parts[-1]
    bucket = parts[-2]
    user_id = ':'.join(parts[1:-2])
    return user_id, bucket, date

def batch_get_values(r: redis.Redis, keys: list[str], chunk_size: int = 500) -> list:
    all_values = []
    
    # Process the keys array in chunks of 500
    for i in range(0, len(keys), chunk_size):
        chunk = keys[i:i + chunk_size]
        all_values.extend(r.mget(chunk))
        
    return all_values


@functions_framework.http
def export_download_stats(request: flask.Request) -> tuple[str, int]:
    """Export yesterday's download stats from Redis to GCS as a CSV."""
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime('%Y-%m-%d')

    redis_client = _get_redis_client()

    # Phase A: load all matching keys into memory
    keys = _scan_stats_keys(redis_client, yesterday)
    if not keys:
        logging.info('No download stats found for %s', yesterday)
        return 'No data to export.', 200

    
    values = batch_get_values(redis_client, keys)
    records = []
    for key, value in zip(keys, values):
        if value is None:
            continue
        user_id, bucket, date = _parse_stats_key(key)
        records.append({'user_id': user_id, 'bucket': bucket, 'bytes': int(value)})

    logging.info('Loaded %d records for %s', len(records), yesterday)

    # write to GCS as CSV
    year, month, day = yesterday.split('-')
    gcs_prefix = os.environ.get('GCS_STATS_PREFIX', 'download-stats')
    gcs_bucket_name = os.environ['GCS_STATS_BUCKET']
    gcs_object_name = f'{gcs_prefix}/year={year}/month={month}/day={day}/summary.csv'

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=['user_id', 'bucket',  'bytes'])
    writer.writeheader()
    writer.writerows(records)

    gcs_client = storage.Client()
    blob = gcs_client.bucket(gcs_bucket_name).blob(gcs_object_name)
    blob.upload_from_string(buffer.getvalue(), content_type='text/csv')

    logging.info(f'Exported download stat records. Count: {len(records)}')
    
    pipeline = redis_client.pipeline(transaction=False)
    for key in keys:
        pipeline.expire(key, EXPIRE_AFTER_EXPORT_SECS)
    pipeline.execute()

    return f'Exported {len(records)} records to {gcs_object_name}.', 200
