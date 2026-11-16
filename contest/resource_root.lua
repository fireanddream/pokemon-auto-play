-- Shared Lua lives beside contest/ in the worktree and inside it in the release.
local directory = assert(debug.getinfo(1, "S").source:sub(2):gsub("\\", "/"):match("^(.*)/"))
return loadfile(directory .. "/lua/bridge_io.lua") and directory or directory .. "/.."
