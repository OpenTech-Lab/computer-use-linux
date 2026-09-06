const net = require('net');
const fs = require('fs');
const path = require('path');
const vscode = require('vscode');

let server;

function socketPath() {
  if (process.env.CUL_VSCODE_SOCKET) return process.env.CUL_VSCODE_SOCKET;
  const runtime = process.env.XDG_RUNTIME_DIR;
  return runtime ? path.join(runtime, 'cul-vscode.sock') : path.join(require('os').homedir(), '.local', 'state', 'computer-use-linux', 'vscode.sock');
}

async function handle(request) {
  if (request.action === 'status') {
    return { ok: true, version: vscode.version, appName: vscode.env.appName };
  }
  if (request.action === 'command') {
    const id = String(request.id || '');
    const args = Array.isArray(request.args) ? request.args : [];
    const result = await vscode.commands.executeCommand(id, ...args);
    return { ok: true, command: id, result: result === undefined ? null : result };
  }
  return { ok: false, error: `unknown bridge action: ${request.action}` };
}

function activate(context) {
  const filename = socketPath();
  fs.mkdirSync(path.dirname(filename), { recursive: true });
  try { fs.unlinkSync(filename); } catch (_) {}
  server = net.createServer((connection) => {
    let buffer = '';
    connection.setEncoding('utf8');
    connection.on('data', (chunk) => {
      buffer += chunk;
      let newline;
      while ((newline = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, newline);
        buffer = buffer.slice(newline + 1);
        let request;
        try { request = JSON.parse(line); } catch (error) {
          connection.write(JSON.stringify({ ok: false, error: String(error) }) + '\n');
          continue;
        }
        handle(request).then((response) => {
          connection.write(JSON.stringify(response) + '\n');
          connection.end();
        }).catch((error) => {
          connection.write(JSON.stringify({ ok: false, error: String(error) }) + '\n');
          connection.end();
        });
      }
    });
  });
  // A Unix-domain socket is local by construction; unlike a TCP listener it has no address
  // argument and cannot be reached from another host.
  server.listen(filename);
  context.subscriptions.push({ dispose: () => {
    if (server) server.close();
    try { fs.unlinkSync(filename); } catch (_) {}
  }});
}

function deactivate() {
  if (server) server.close();
}

module.exports = { activate, deactivate };
