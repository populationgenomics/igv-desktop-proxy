-- Release the lock safely
-- Refer - https://redis.io/docs/latest/commands/set/ for this lock pattern
-- Keys:   KEYS[1] = hashed token (user token hash)
-- Args:   ARGV[1] = uuid         (request uuid)


local key         = KEYS[1]
local uuid        = ARGV[1]

if redis.call('GET',key) == uuid
then
    return redis.call('DEL',key)
else
    return 0
end
