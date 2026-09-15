local time, jobs, teleports, queued = 0, {}, {}, 0
local function signal()
 local s = { callbacks = {} }
 function s:Connect(f) table.insert(self.callbacks,f); return {Disconnect=function() for i, callback in ipairs(self.callbacks) do if callback == f then table.remove(self.callbacks,i); break end end end} end
 function s:Fire(...) for _, f in ipairs(self.callbacks) do f(...) end end
 return s
end
local button
Instance = {new=function(kind)
 local o = {Destroy=function() end, Event=signal()}
 if kind == 'TextButton' then o.Activated=signal(); button=o end
 return o
end}
UDim2 = {fromOffset=function() return {} end}
local player = {UserId=7,WaitForChild=function() return {GetChildren=function() return {} end} end}
local badge = {BadgeAwarded=signal(),OnBadgeAwarded=signal()}
local tp = {TeleportInitFailed=signal(),Teleport=function(_,id) table.insert(teleports,id) end}
local saved = {enabled=true,visited={},resumeTarget="1"}
local services = {Players={LocalPlayer=player},TeleportService=tp,BadgeService=badge,
 HttpService={JSONDecode=function(_,s) if s=='state' then return saved end return {data={{id=tonumber(s),rootPlaceId=tonumber(s)*10}}} end,JSONEncode=function() return 'state' end}}
game={GameId=1,GetService=function(_,s) return services[s] end,HttpGet=function(_,url) if url:match('games.txt$') then return '1\n2\n2\n3\n' end return url:match('=(%d+)$') end}
local env={}
getgenv=function() return env end
isfile=function() return true end
readfile=function(p) if p=='games.txt' then return '1\n2\n2\n3\n' end return 'state' end
writefile=function() end
queue_on_teleport=function() queued=queued+1 end
warn=function() end
os.clock=function() return time end
task={wait=function(n) return coroutine.yield(n or 0.01) end,spawn=function(f) table.insert(jobs,{co=coroutine.create(f),at=time}) end}
local function advance(target)
 while true do
  local job
  for _,j in ipairs(jobs) do if coroutine.status(j.co)~='dead' and j.at<=target and (not job or j.at<job.at) then job=j end end
  if not job then break end
  time=job.at
  local ok,delay=coroutine.resume(job.co); assert(ok,delay)
  job.at=time+(delay or 0)
 end
 time=target
end
env.BadgeFarmResume=true; assert(loadfile('badge-farm.lua'))()
advance(9.9); assert(#teleports==0,'teleported early')
advance(10.1); assert(teleports[1]==20,'timer failed'); assert(saved.visited['1'] and not saved.visited['2'],'visited before arrival')
tp.TeleportInitFailed:Fire(player,nil,'failed',20)
advance(13); assert(#teleports==1,'cooldown ignored'); advance(13.3); assert(teleports[2]==30,'failed destination not skipped'); assert(queued==1,'queue duplicated')
print('PASS: timer, deduplication, arrival tracking, failed teleport, single queue')
-- Fresh runtime with early award and pause/resume.
env.BadgeFarmStop(); jobs={}; teleports={}; time=0; saved={enabled=true,visited={},resumeTarget="1"}
env.BadgeFarmResume=true; assert(loadfile('badge-farm.lua'))()
advance(1); badge.OnBadgeAwarded:Fire(99,0,5); advance(2); assert(#teleports==0,'other player badge counted')
button.Activated:Fire(); badge.OnBadgeAwarded:Fire(7,0,5); advance(3); assert(#teleports==0,'OFF teleported')
button.Activated:Fire(); advance(3.2); assert(teleports[1]==20,'local badge did not advance')
print('PASS: local badge filtering and OFF/ON')


-- Source-wide restrictions pause without burning through destinations.
env.BadgeFarmStop(); jobs={}; teleports={}; time=0; saved={enabled=true,visited={},resumeTarget="1"}
env.BadgeFarmResume=true; assert(loadfile('badge-farm.lua'))()
advance(10.2)
tp.TeleportInitFailed:Fire(player,nil,'Cannot teleport from this universe to a universe owned by a different creator',20)
advance(60); assert(#teleports==1 and not saved.enabled,'restriction did not pause')
assert(not saved.visited['2'],'rejected destination marked visited')
print('PASS: creator restriction pauses')
-- Temporary malformed responses retry the same ID with bounded backoff.
env.BadgeFarmStop(); jobs={}; teleports={}; time=0; saved={enabled=true,visited={},resumeTarget="1"}
local originalHttp = game.HttpGet
local lookups = {}
game.HttpGet=function(self,url)
 if url:match('universeIds=') then table.insert(lookups,{time=time,id=url:match('=(%d+)$')}); error("Can't parse JSON") end
 return originalHttp(self,url)
end
env.BadgeFarmResume=true; assert(loadfile('badge-farm.lua'))()
advance(60)
assert(#lookups==3 and not saved.enabled,'lookup retries not bounded')
assert(lookups[2].time>=15 and lookups[3].time>=45,'lookup cooldown too short')
for _,lookup in ipairs(lookups) do assert(lookup.id=='2','temporary failure skipped ID') end
print('PASS: lookup backoff, same-ID retry, final pause')


env.BadgeFarmStop(); jobs={}; teleports={}; time=0; saved={enabled=true,visited={['2']=true}}
game.HttpGet=originalHttp
assert(loadfile('badge-farm.lua'))()
assert(button.Text=='OFF','manual execution resumed old session')
button.Activated:Fire(); advance(10.2)
assert(teleports[1]==20,'manual execution kept visited IDs')
print('PASS: manual execution resets progress and starts OFF')
