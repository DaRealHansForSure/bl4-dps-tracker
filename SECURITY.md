# Security

## What the mod exposes

BL4 Damage Tracker runs inside the game and serves a web dashboard while the game runs. The
dashboard has no login. By default it listens on every network interface (`web_host` is
`0.0.0.0`, port `8765`), so anyone who can reach your machine on that port can read:

- the damage you dealt, per hit, per fight and per weapon
- your character names and classes
- the names, rarities and pictures of your items
- the names of your session log files

It cannot change anything: the HTTP API and the WebSocket are read-only, and nothing a client
sends reaches the game, the records or the files.

## Recommendations for players

- On a network you do not trust (a LAN party, a dorm, a public network), set `web_host` to
  `127.0.0.1` in `sdk_mods\settings\dmgtracker.json`, or switch "Web dashboard" off in the mod
  menu.
- Never forward the dashboard's port on your router. It is not built for the internet.
- When Windows asks whether the game may accept network connections, allow it only for private
  networks, or not at all if you only use the dashboard on the same machine.

## What the mod does to protect you

- A request whose browser `Origin` header names another site is refused, so a web page you visit
  cannot read the dashboard through your browser.
- The item name reader reads the game's own memory only, through `ReadProcessMemory` on the
  current process, which fails cleanly on unmapped pages. It never writes memory.
- Every game structure is checked against its expected content before it is used, so a game patch
  makes the mod stop with one log line rather than act on wrong data.
- The mod writes only to `%LOCALAPPDATA%\BL4DamageTracker\` and to its own settings file.

## Reporting a problem

Found a security problem? Please report it privately to the maintainer through the repository's
private vulnerability reporting on GitHub (the Security tab), rather than in a public issue.
Include the mod version (`dmgstatus` prints it), what you did and what happened. You will get an
answer, and a fix will be released as a patch version and noted in the [changelog](CHANGELOG.md).
