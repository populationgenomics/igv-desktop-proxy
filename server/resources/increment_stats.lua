-- Keys:   KEYS[1] = stats_key      (the key for the download stats)
-- Args:   ARGV[1] = bytes_count    (the number of bytes to increment by)
--         ARGV[2] = ttl_seconds    (the TTL to set if it doesn't exist)
--
-- Returns:
--   1 if the expire time was set, 0 otherwise.

redis.call('INCRBY', KEYS[1], ARGV[1])
return redis.call('EXPIRE', KEYS[1], ARGV[2], 'NX')

