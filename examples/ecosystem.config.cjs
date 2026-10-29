// PM2 must launch the FreeBSD executable directly, not interpret it as JavaScript.
const path = require("node:path");

module.exports = {
  apps: [{
    name: "memos",
    script: path.join(__dirname, "memos"),
    cwd: __dirname,
    interpreter: "none",
    exec_mode: "fork",
    instances: 1,
    args: ["--addr=127.0.0.1", "--port=5230", `--data=${path.join(__dirname, "data")}`],
    autorestart: true,
    min_uptime: "10s",
    max_restarts: 10,
    restart_delay: 3000,
  }],
};
