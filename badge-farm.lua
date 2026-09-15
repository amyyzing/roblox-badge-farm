-- Hosted script and universe list; only progress is stored locally.
local BASE_URL = "https://raw.githubusercontent.com/amyyzing/roblox-badge-farm/main/"
local LOADER = 'loadstring(game:HttpGet("' .. BASE_URL .. 'badge-farm.lua"))()'
local Players = game:GetService("Players")
local Teleports = game:GetService("TeleportService")
local Http = game:GetService("HttpService")
local Badges = game:GetService("BadgeService")
local queue = queue_on_teleport or queueonteleport or (syn and syn.queue_on_teleport)
assert(readfile and writefile and isfile and queue, "File access and queue_on_teleport are required.")

local env = getgenv()
if env.BadgeFarmStop then env.BadgeFarmStop() end
local player = Players.LocalPlayer
while not player do task.wait(); player = Players.LocalPlayer end
local stateFile = "badge-farm-" .. player.UserId .. ".json"
local state = { enabled = false, visited = {} }
if isfile(stateFile) then
    state = Http:JSONDecode(readfile(stateFile))
    assert(type(state) == "table" and type(state.visited) == "table", "Invalid badge farm save; rename it to reset.")
end

local ids, seen = {}, {}
for line in game:HttpGet(BASE_URL .. "games.txt"):gmatch("[^\r\n]+") do
    local id = line:match("^%s*(%d+)%s*$")
    assert(id, "games.txt must contain one universe ID per line.")
    id = tostring(tonumber(id))
    if not seen[id] then
        seen[id] = true
        table.insert(ids, id)
    end
end
assert(#ids > 0, "games.txt is empty.")

local running, earned, pending, queued = true, false, nil, false
local deadline = os.clock() + 10
local skipped, connections = {}, {}
local gui = Instance.new("ScreenGui")
gui.Name = "BadgeFarm"
gui.ResetOnSpawn = false
gui.DisplayOrder = 2147483647
gui.Parent = player:WaitForChild("PlayerGui")
local button = Instance.new("TextButton")
button.Size = UDim2.fromOffset(100, 40)
button.Position = UDim2.fromOffset(15, 100)
button.TextSize = 20
button.Parent = gui

local function save()
    writefile(stateFile, Http:JSONEncode(state))
end
local function updateButton()
    button.Text = state.enabled and "ON" or "OFF"
end
local function markCurrent()
    if game.GameId ~= 0 then state.visited[tostring(game.GameId)] = true end
end
local function stopWithError(message)
    state.enabled = false
    updateButton()
    pcall(save)
    warn("Badge farm: " .. tostring(message))
end

env.BadgeFarmStop = function()
    running = false
    for _, connection in ipairs(connections) do connection:Disconnect() end
    gui:Destroy()
end
table.insert(connections, button.Activated:Connect(function()
    state.enabled = not state.enabled
    if state.enabled then
        deadline = os.clock() + 10
        markCurrent()
    end
    updateButton()
    local ok, err = pcall(save)
    if not ok then stopWithError(err) end
end))

-- These internal events require executor support. The timer still works without them.
local function badgeReceived(userId)
    if tonumber(userId) == player.UserId then earned = true end
end
local badgeEventCount = 0
for _, name in ipairs({ "BadgeAwarded", "OnBadgeAwarded" }) do
    local ok = pcall(function()
        local connection = Badges[name]:Connect(function(first, second)
            badgeReceived(name == "BadgeAwarded" and second or first)
        end)
        table.insert(connections, connection)
    end)
    if ok then badgeEventCount = badgeEventCount + 1 end
end
if badgeEventCount == 0 then warn("Badge farm: badge events unavailable; using the 10-second timer.") end

table.insert(connections, Teleports.TeleportInitFailed:Connect(function(who, _, message, placeId)
    if who == player and pending and pending.place == placeId then
        skipped[pending.id] = true
        pending = nil
        warn("Badge farm: teleport failed; skipping for this session. " .. tostring(message))
    end
end))

local function resolve(id)
    local result = Http:JSONDecode(game:HttpGet("https://games.roblox.com/v1/games?universeIds=" .. id))
    for _, info in ipairs(result.data or {}) do
        if tostring(info.id) == id and tonumber(info.rootPlaceId) and info.rootPlaceId > 0 then
            return info.rootPlaceId
        end
    end
    error("No starting place found for " .. id)
end

updateButton()
if state.enabled then
    markCurrent()
    local ok, err = pcall(save)
    if not ok then stopWithError(err) end
end

-- Resolve the next destination during the current game's waiting period.
task.spawn(function()
    while running do
        if not state.enabled or pending then
            task.wait(0.1)
        else
            local nextId
            for _, id in ipairs(ids) do
                if not state.visited[id] and not skipped[id] then nextId = id; break end
            end
            if not nextId then
                stopWithError("Finished available games. Failed games can be retried by rerunning the script.")
            else
                local ok, place = pcall(resolve, nextId)
                if not ok then
                    skipped[nextId] = true
                    warn("Badge farm: " .. tostring(place))
                    task.wait(1)
                else
                    while running and (not state.enabled or (not earned and os.clock() < deadline)) do
                        task.wait(0.05)
                    end
                    if not running then return end
                    local success, err = pcall(function()
                        save()
                        if not queued then
                            queue(LOADER)
                            queued = true
                        end
                    end)
                    if not success then
                        stopWithError(err)
                    else
                        pending = { id = nextId, place = place }
                        local sent, message = pcall(function() Teleports:Teleport(place, player) end)
                        if not sent then
                            skipped[nextId] = true
                            pending = nil
                            warn("Badge farm: " .. tostring(message))
                            task.wait(1)
                        end
                        -- Wait for arrival or TeleportInitFailed; overlapping teleports are unsafe.
                    end
                end
            end
        end
    end
end)
