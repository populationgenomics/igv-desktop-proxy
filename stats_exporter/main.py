import csv
import logging
import os
from datetime import UTC, datetime, timedelta

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
EXPIRE_AFTER_EXPORT_SECS = 300  # 5 minutes
DOWNLOAD_STATS_PREFIX = 'download-stats'
REDIS_DEFAULT_CONFIGS = {
    'host': 'localhost',
    'port': '6379',
}


def _get_redis_client() -> redis.Redis:
    redis_host = os.environ.get('REDIS_HOST', REDIS_DEFAULT_CONFIGS.get('host'))
    redis_port = int(os.environ.get('REDIS_PORT', REDIS_DEFAULT_CONFIGS.get('port')))
    password = os.environ.get('REDIS_PASSWORD')

    url = f'redis://:{password}@{redis_host}:{redis_port}/0' if password else f'redis://{redis_host}:{redis_port}/0'
    return redis.Redis.from_pool(redis.ConnectionPool.from_url(url=url, max_connections=5, decode_responses=True))


def _parse_stats_key(key: str) -> tuple[str, str, str]:
    """Parse dl_stats:{user_id}:{bucket}:{date} into (user_id, bucket, date)."""
    parts = key.split(':')
    date = parts[-1]
    bucket = parts[-2]
    user_id = ':'.join(parts[1:-2])
    return user_id, bucket, date


def iter_stats_in_batches(redis_client: redis.Redis, date_str: str, batch_size: int = 500):
    """Yield batches of (keys, values)."""
    pattern = f'{STATS_KEY_PREFIX}:*:{date_str}'
    batch_keys = []

    for key in redis_client.scan_iter(match=pattern, count=batch_size):
        batch_keys.append(key)
        if len(batch_keys) >= batch_size:
            values = redis_client.mget(batch_keys)
            yield batch_keys, values
            batch_keys = []

    if batch_keys:
        values = redis_client.mget(batch_keys)
        yield batch_keys, values


@functions_framework.http
def export_download_stats(_request: flask.Request) -> tuple[str, int]:
    """Export yesterday's download stats from Redis to GCS as a CSV."""
    yesterday = (datetime.now(UTC) - timedelta(days=1)).strftime('%Y-%m-%d')
    redis_client = _get_redis_client()

    year, month, day = yesterday.split('-')
    gcs_bucket_name = os.environ.get('GCS_STATS_BUCKET')
    gcs_object_name = f'{DOWNLOAD_STATS_PREFIX}/year={year}/month={month}/day={day}/summary.csv'

    gcs_client = storage.Client()
    blob = gcs_client.bucket(gcs_bucket_name).blob(gcs_object_name)

    total_records = 0
    with blob.open('wt', content_type='text/csv') as gcs_file:
        writer = csv.writer(gcs_file)
        writer.writerow(['user_id', 'bucket', 'bytes'])
        # avoids reading all keys and all values to memory at once
        for batch_keys, batch_values in iter_stats_in_batches(redis_client, yesterday):
            pipeline = redis_client.pipeline(transaction=False)
            for key, value in zip(batch_keys, batch_values, strict=True):
                if value is None:
                    continue

                user_id, bucket, _date = _parse_stats_key(key)
                writer.writerow([user_id, bucket, int(value)])
                pipeline.expire(key, EXPIRE_AFTER_EXPORT_SECS)
                total_records += 1

            pipeline.execute()

    if total_records == 0:
        logging.info(f'No download stats found for {yesterday}')
        blob.delete()
        return 'No data to export.', 200

    logging.info(f'Exported download stat records. Count: {total_records}')
    return f'Exported {total_records} records.', 200
