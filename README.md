# Badge farm

Execute this in your executor:

```lua
loadstring(game:HttpGet("https://raw.githubusercontent.com/amyyzing/roblox-badge-farm/refs/heads/main/badge-farm.lua"))()
```

Click **OFF** to turn it **ON**. Click **ON** to pause. The script and game list load from GitHub automatically, including after teleports. No manual file copying is needed.

The list uses one **universe ID** per line. Update `games.txt` in this repository whenever you want to use a new list.

The script resolves each universe's starting place, skips visited universes, and requests the next teleport after 10 seconds or a detected badge award. The timer begins when the script resumes, or when you turn it on. Network lookups and Roblox loading can add time; the script cannot force Roblox to complete a teleport immediately. A teleport already requested cannot be canceled with OFF.

Automatic farm teleports carry progress through `badge-farm-<user ID>.json`. Manual re-execution starts a fresh session with OFF selected, and replaces old buttons and session progress. Joining a different game manually does not resume the old session: execute the loader there to start fresh. Only a queued continuation arriving in its expected destination resumes. Missing destinations and rejected destination teleports are skipped for the current session.

If the current game blocks teleports to other creators, the farm pauses. Manually joining another game only helps if that game permits third-party teleports. Restricted destinations are skipped with a 3-second cooldown. Temporary lookup or JSON errors retry the same destination after 15 and 30 seconds, then pause after the third failure. Roblox's own error dialog can still appear when it rejects a teleport.

The cross-creator restriction is enforced by Roblox. `TeleportService` cannot override it from an executor. Each hop originates in the game you are currently playing, so an arbitrary list of unrelated experiences cannot be chained unless each current experience permits third-party teleports. An owned hub can allow the first hop, but it cannot change the setting on the destination games. The script now reports the blocked hop and stops instead of pretending it is an age restriction.

Innovation Labs (`7065948`) is currently kept at the end of the hosted route because its outbound restriction was observed live. It can still be visited for its badge, but the route ends there. Other restricted experiences may need the same treatment if Roblox rejects their outbound hop.

Requires executor support for file access, `getgenv`, `game:HttpGet`, `loadstring`, and `queue_on_teleport` (or its supported aliases). Automatic continuation depends on the executor running queued scripts after teleport. This is not a normal Studio LocalScript.

Early departure uses Roblox's restricted badge events. If the executor cannot connect to them, the script warns and uses the timer. Events awarded before the script starts listening may be missed. Live badge delivery and cross-game continuation still need testing in your executor.
