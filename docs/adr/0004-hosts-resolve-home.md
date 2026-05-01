# Hosts resolve their own home directory

Hosts resolve their own home directory at construction. `SSHHost` makes an SSH call to determine `$HOME` on the remote, `LocalHost` uses `Path.home()`. This replaces the previous pattern of embedding `$HOME`/`~` in path strings and deferring expansion to call sites, which produced bugs when the wrong context's home was substituted.
