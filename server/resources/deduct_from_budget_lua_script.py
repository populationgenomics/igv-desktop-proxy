# adapted from - https://github.com/alisaifee/limits,
# https://redis.io/tutorials/howtos/ratelimiting/#1-fixed-window-counter

# Keys:   KEYS[1] = user_sub       (user hash key)
# Args:   ARGV[1] = request_range  (bytes being requested)
#         ARGV[2] = cap            (total bytes allowed per window)
#         ARGV[3] = window         (window duration in seconds)
#         ARGV[4] = now            (current unix timestamp as float)
#
# Returns:
#   >= 0  → allowed; value is remaining bytes after deduction
#     -1  → denied (cap exceeded, or single request > cap on a fresh window)
DEDUCT_LUA_SCRIPT = """
local key         = KEYS[1]
local amount      = tonumber(ARGV[1])
local cap         = tonumber(ARGV[2])
local window      = tonumber(ARGV[3])
local now         = tonumber(ARGV[4])

local remaining   = tonumber(redis.call('HGET', key, 'remaining_bytes'))
local expire_time = tonumber(redis.call('HGET', key, 'expire_time'))

if remaining ~= nil and expire_time ~= nil and (expire_time - now) > 0 then
    if (remaining - amount) >= 0 then
        local new_remaining = remaining - amount
        redis.call('HSET', key, 'remaining_bytes', new_remaining)
        return new_remaining
    else
        return -1
    end
end

-- No active window (new user or expired). Start a fresh window.
local new_remaining = cap - amount
local new_expire    = now + window
redis.call('HSET', key, 'remaining_bytes', new_remaining, 'expire_time', new_expire)
redis.call('EXPIREAT', key, math.ceil(new_expire))
return new_remaining
"""
