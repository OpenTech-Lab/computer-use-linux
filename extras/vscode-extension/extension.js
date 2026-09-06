const net = require('net');
const fs = require('fs');
const path = require('path');
const os = require('os');
const crypto = require('crypto');
const vscode = require('vscode');

let server;
let filename;
let stateFilename;
let token;

function ownerUid() {
  return typeof process.getuid === 'function' ? process.getuid() : null;
}

function ensurePrivateDirectory(dir) {
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  const metadata = fs.statSync(dir);
  const uid = ownerUid();
  if ((uid !== null && metadata.uid !== uid) || (metadata.mode & 0o777) !== 0o700) {
    throw new Error('CUL VSCode bridge directory is not owner-only');
  }
}

function socketPath() {
  if (process.env.CUL_VSCODE_SOCKET) return process.env.CUL_VSCODE_SOCKET;
  const runtime = process.env.XDG_RUNTIME_DIR;
  return runtime ? path.join(runtime, 'cul-vscode.sock') : path.join(os.homedir(), '.local', 'state', 'computer-use-linux', 'vscode.sock');
}

function statePath() {
  if (process.env.CUL_VSCODE_STATE_FILE) return process.env.CUL_VSCODE_STATE_FILE;
  return path.join(path.dirname(socketPath()), 'vscode.json');
}

function readState(filenameToRead) {
  try {
    const value = JSON.parse(fs.readFileSync(filenameToRead, 'utf8'));
    return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  } catch (_) {
    return {};
  }
}

function writePrivateState(filenameToWrite, value) {
  const dir = path.dirname(filenameToWrite);
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  const descriptor = fs.openSync(filenameToWrite, 'w', 0o600);
  try {
    fs.fchmodSync(descriptor, 0o600);
    fs.writeFileSync(descriptor, JSON.stringify(value));
  } finally {
    fs.closeSync(descriptor);
  }
  const metadata = fs.statSync(filenameToWrite);
  const uid = ownerUid();
  if ((uid !== null && metadata.uid !== uid) || (metadata.mode & 0o777) !== 0o600) {
    throw new Error('CUL VSCode bridge state file is not owner-only');
  }
}

function removeStaleSocket(filenameToRemove) {
  try {
    const metadata = fs.lstatSync(filenameToRemove);
    if (!metadata.isSocket()) throw new Error('CUL VSCode bridge path is not a socket');
    fs.unlinkSync(filenameToRemove);
  } catch (error) {
    if (error && error.code !== 'ENOENT') throw error;
  }
}

function constantTimeTokenMatches(requestToken) {
  const expected = Buffer.from(token || '', 'utf8');
  const received = Buffer.from(typeof requestToken === 'string' ? requestToken : '', 'utf8');
  const comparable = Buffer.alloc(expected.length);
  received.copy(comparable, 0, 0, Math.min(received.length, comparable.length));
  const equal = crypto.timingSafeEqual(expected, comparable);
  return equal && received.length === expected.length;
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

function closeUnauthorized(connection) {
  connection.destroy();
}

function activate(context) {
  filename = socketPath();
  stateFilename = statePath();
  const socketDirectory = path.dirname(filename);
  ensurePrivateDirectory(socketDirectory);
  removeStaleSocket(filename);
  token = crypto.randomBytes(32).toString('base64url');
  const state = { ...readState(stateFilename), pid: process.pid, socket: filename, token };

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
        try {
          request = JSON.parse(line);
        } catch (_) {
          connection.end();
          return;
        }
        if (!request || typeof request !== 'object' || Array.isArray(request) || !constantTimeTokenMatches(request.token)) {
          closeUnauthorized(connection);
          return;
        }
        const { token: _requestToken, ...authenticatedRequest } = request;
        handle(authenticatedRequest).then((response) => {
          connection.write(JSON.stringify(response) + '\n');
          connection.end();
        }).catch(() => {
          connection.write(JSON.stringify({ ok: false, error: 'bridge action failed' }) + '\n');
          connection.end();
        });
        return;
      }
    });
  });
  // The callback runs after the Unix socket has been created, so chmod happens before any
  // request can be served and before the private token state is published.
  server.listen(filename, () => {
    try {
      fs.chmodSync(filename, 0o600);
      const metadata = fs.statSync(filename);
      if ((metadata.mode & 0o777) !== 0o600) throw new Error('CUL VSCode bridge socket is not owner-only');
      writePrivateState(stateFilename, state);
    } catch (error) {
      server.close();
      try { fs.unlinkSync(filename); } catch (_) {}
      throw error;
    }
  });
  context.subscriptions.push({ dispose: deactivate });
}

function deactivate() {
  if (server) server.close();
  if (filename) {
    try { fs.unlinkSync(filename); } catch (_) {}
  }
  if (stateFilename && readState(stateFilename).pid === process.pid) {
    try { fs.unlinkSync(stateFilename); } catch (_) {}
  }
  server = undefined;
  filename = undefined;
  stateFilename = undefined;
  token = undefined;
}

module.exports = {
  activate,
  deactivate,
  _test: { constantTimeTokenMatches, ensurePrivateDirectory, writePrivateState },
};
