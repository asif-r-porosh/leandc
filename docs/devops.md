# Running leandc as a dedicated user

leandc runs commands and edits files as the user that starts the server. A dedicated user limits what a leaked URL or a bad command can reach. See [SECURITY.md](../SECURITY.md) for the reasoning.

## Linux

1. Create a user with no admin rights. The command below adds no extra groups:

   ```
   sudo useradd --create-home --shell /bin/bash leandcrun
   ```

2. Install leandc for all users and start the service as that user, as in the README section "As a service on Linux (systemd)": `sudo leandc setup`, then `sudo leandc start leandcrun`. Pick a user name other than `leandc`, because the service commands create a group of that name.
3. Give the user no private SSH keys, no agent and no `sudo`.

## macOS

A launchd LaunchAgent starts the server when the owning user logs in and restarts the server if the process dies. This setup has been run with a normal logged-in user.

1. Create the plist `~/Library/LaunchAgents/net.leandc.http.plist` for that user. launchd does not expand `~`, so every path is absolute. Replace `USER` throughout:

   ```
   <?xml version="1.0" encoding="UTF-8"?>
   <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
   <plist version="1.0">
   <dict>
     <key>Label</key><string>net.leandc.http</string>
     <key>ProgramArguments</key>
     <array>
       <string>/Users/USER/.local/share/leandc/venv/bin/leandc</string>
       <string>serve</string>
       <string>--config</string>
       <string>/Users/USER/.config/leandc/config.toml</string>
       <string>--transport</string>
       <string>http</string>
     </array>
     <key>RunAtLoad</key><true/>
     <key>KeepAlive</key><true/>
     <key>EnvironmentVariables</key>
     <dict>
       <key>PATH</key><string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
     </dict>
     <key>StandardOutPath</key><string>/Users/USER/Library/Logs/leandc.log</string>
     <key>StandardErrorPath</key><string>/Users/USER/Library/Logs/leandc.log</string>
   </dict>
   </plist>
   ```

2. Check the syntax and load the job:

   ```
   plutil -lint ~/Library/LaunchAgents/net.leandc.http.plist
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/net.leandc.http.plist
   ```

3. Look at the state, and at the log when something is wrong:

   ```
   launchctl print gui/$(id -u)/net.leandc.http | grep -E "state|pid"
   tail -20 ~/Library/Logs/leandc.log
   ```

Notes:

- `launchctl bootstrap` fails with `Input/output error` when the job is already loaded. Use `launchctl bootout gui/$(id -u)/net.leandc.http` first, or `launchctl kickstart -k gui/$(id -u)/net.leandc.http` to restart.
- A launchd job starts with a minimal `PATH`. Add the folders of the tools that `run` needs to the `PATH` entry.
- A LaunchAgent runs only while the owning user is logged in. Starting at boot without a login needs a LaunchDaemon, which is not covered here.
