-- Refunds bytes to download budget after a failed GCS request,
-- if the time window is still active.
-- Keys:   KEYS[1] = user_sub (user hash key)
-- Args:   ARGV[1] = amount   (bytes to refund)
--         ARGV[2] = now      (current unix timestamp)


local key         = KEYS[1]
local amount      = tonumber(ARGV[1])
local now         = tonumber(ARGV[2])

local expire_time = tonumber(redis.call('HGET', key, 'expire_time'))

if expire_time ~= nil and (expire_time - now) > 0 then
    redis.call('HINCRBY', key, 'remaining_bytes', amount)
end
return 1
