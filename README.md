# Badge farm

Execute this in your executor:

```lua
loadstring(game:HttpGet("https://raw.githubusercontent.com/amyyzing/roblox-badge-farm/main/badge-farm.lua"))()
```

Click **OFF** to turn it **ON**. Click **ON** to pause. The script and game list load from GitHub automatically, including after teleports. No manual file copying is needed.

The list uses one **universe ID** per line. Update `games.txt` in this repository whenever you want to use a new list.

The script resolves each universe's starting place, skips visited universes, and requests the next teleport after 10 seconds or a detected badge award. The timer begins when the script resumes, or when you turn it on. Network lookups and Roblox loading can add time; the script cannot force Roblox to complete a teleport immediately. A teleport already requested cannot be canceled with OFF.

Progress and ON/OFF state persist in `badge-farm-<user ID>.json`. Only games actually reached while enabled are marked visited. Delete that file while the script is stopped to reset progress. Failed lookups and rejected teleports are skipped for the current script session and remain eligible on a later run.

Requires executor support for file access, `getgenv`, `game:HttpGet`, `loadstring`, and `queue_on_teleport` (or its supported aliases). Automatic continuation depends on the executor running queued scripts after teleport. This is not a normal Studio LocalScript.

Early departure uses Roblox's restricted badge events. If the executor cannot connect to them, the script warns and uses the timer. Events awarded before the script starts listening may be missed. Live badge delivery and cross-game continuation still need testing in your executor.
