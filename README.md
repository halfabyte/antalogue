# antalogue

Decentralised anonymous text chat. Anyone can run a server. Clients connect to multiple servers simultaneously and get matched with the first available stranger.

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Edit config

Open `config.yaml` and change:

- `ip_salt` — set to a random secret string
- `admin.password` — change the default admin password
- `server.port` — change if needed
- `server.name` — your server's display name

### 3. Run the server

```bash
python server.py
```

The server starts on `http://0.0.0.0:8765` by default.

### 4. Use it

| URL | What |
|---|---|
| `http://yourhost:8765/` | Client chat page |
| `http://yourhost:8765/admin` | Admin / moderator panel |
| `ws://yourhost:8765/ws` | WebSocket endpoint (what clients connect to) |

## How It Works

### Client

The client is a single static HTML page. Users add server domains (like `antalogue.example.com`), then click **Find Someone**. The client constructs `wss://domain/ws` automatically, connects to all added servers, and waits for a match.

**Multi-server handshake:** When a server finds a pair, it sends `match_request` to both clients. Each client responds with `accept` or `reject`. If a client was already matched on another server, it rejects — the server seamlessly requeues the partner with no visible interruption. Only when both clients accept does the server send `matched` and the chat begins. This eliminates the "phantom disconnect" problem where a client matched on two servers simultaneously.

**Don't-rematch mechanism:** When two users are paired, the server generates a unique chat ID sent to both clients. When the conversation ends, both clients store this single ID. On the next search, each client sends its stored ID to all servers — if two potential matches share the same ID, the server won't pair them. The ID is replaced when a new match is found, so you only ever avoid your most recent partner.

Chat history is stored locally in the browser for easy review in the right sidebar.

### Server

Each server is independent. It handles:

- **WebSocket matchmaking** with two-phase handshake and exclude-ID support
- **Message relay and storage** (SQLite, 1-week retention by default)
- **IP tracking** — IPs are salted + SHA-256 hashed for matching and bans; raw IPs are also stored and visible to admins only
- **Rate limiting** — per-connection, configurable messages-per-minute (shared IPs get separate limits)
- **Auto-cleanup** — old messages purged on a schedule

### Admin Panel

Accessible at `/admin`. Accounts stored in the SQLite database.

**Admins** can:
- View server stats (connected users, active chats, pending reports)
- Review and resolve/dismiss reports
- View full message logs for any chat session
- See raw (unhashed) IPs for all sessions and bans
- Ban/unban IPs (with optional expiry)
- Create and manage moderator/admin accounts

**Moderators** can do everything above except see raw IPs and manage accounts.

## Config Reference

```yaml
server:
  host: "0.0.0.0"        # bind address
  port: 8765              # listen port
  name: "My Server"       # shown to clients
  motd: "Be nice."        # message of the day

database: "antalogue.db"  # SQLite file path
ip_salt: "random-secret"  # CHANGE THIS — used for IP hashing (raw IPs are also stored, visible to admins only)

cleanup:
  retention_days: 7       # delete messages older than this
  interval_hours: 6       # how often to run cleanup

admin:
  username: "admin"       # default admin (created on first run)
  password: "changeme"    # CHANGE THIS

max_message_length: 2000  # character limit per message

rate_limit:
  messages_per_minute: 30
```

## WebSocket Protocol

Messages are JSON objects with a `type` field.

### Client → Server

| type | fields | description |
|---|---|---|
| `search` | `exclude_chat_id: string` | Enter the matching queue |
| `cancel` | | Leave the queue |
| `accept` | `chat_id` | Accept a match (handshake) |
| `reject` | `chat_id` | Reject a match — partner is requeued seamlessly |
| `message` | `content: string` | Send a chat message |
| `typing` | | Notify partner is typing |
| `disconnect` | | End the current chat |
| `report` | `chat_id, reason` | Report a chat |

### Server → Client

| type | fields | description |
|---|---|---|
| `server_info` | `name, motd, online` | Sent on connect |
| `online_count` | `count` | Live user count update |
| `searching` | | Search acknowledged |
| `match_request` | `chat_id` | Potential match found — respond with accept/reject |
| `matched` | `chat_id` | Both clients accepted — chat begins |
| `requeued` | | Partner rejected — back in the queue seamlessly |
| `message` | `content, chat_id, sender` | Incoming message |
| `typing` | | Partner is typing |
| `partner_disconnected` | `chat_id` | Partner left |
| `report_ack` | | Report received |
| `error` | `message` | Error (ban, rate limit, etc.) |
| `cancelled` | | Search cancelled |

## Deployment Tips

- Use a reverse proxy (nginx/caddy) with TLS so clients can connect via `wss://`
- Set `X-Forwarded-For` header in your proxy so IP tracking works correctly
- Run with a process manager like systemd or supervisor
- Back up `antalogue.db` periodically
- Raw IPs are stored in the database — keep it secure and consider your data retention obligations

## License

Do whatever you want with it.
