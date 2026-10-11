# WhatsApp MCP Server

This is a Model Context Protocol (MCP) server for WhatsApp.

With this you can search and read your personal Whatsapp messages (including images, videos, documents, and audio messages), search your contacts and send messages to either individuals or groups. You can also send media files including images, videos, documents, and audio messages.

It connects to your **personal WhatsApp account** directly via the Whatsapp web multidevice API (using the [whatsmeow](https://github.com/tulir/whatsmeow) library). All your messages are stored locally in a SQLite database and only sent to an LLM (such as Claude) when the agent accesses them through tools (which you control).

Here's an example of what you can do when it's connected to Claude.

![WhatsApp MCP](./example-use.png)

> To get updates on this and other projects I work on [enter your email here](https://docs.google.com/forms/d/1rTF9wMBTN0vPfzWuQa2BjfGKdKIpTbyeKxhPMcEzgyI/preview)

> *Caution:* as with many MCP servers, the WhatsApp MCP is subject to [the lethal trifecta](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/). This means that project injection could lead to private data exfiltration.

## Installation

### Prerequisites

- Go
- Python 3.6+
- Anthropic Claude Desktop app (or Cursor)
- UV (Python package manager), install with `curl -LsSf https://astral.sh/uv/install.sh | sh`
- FFmpeg (_optional_) - Only needed for audio messages. If you want to send audio files as playable WhatsApp voice messages, they must be in `.ogg` Opus format. With FFmpeg installed, the MCP server will automatically convert non-Opus audio files. Without FFmpeg, you can still send raw audio files using the `send_file` tool.

### Steps

1. **Clone this repository**

   ```bash
   git clone https://github.com/lharries/whatsapp-mcp.git
   cd whatsapp-mcp
   ```

2. **Run the WhatsApp bridge**

   Navigate to the whatsapp-bridge directory and run the Go application:

   ```bash
   cd whatsapp-bridge
   go run .
   ```

   Use `go run .` (or `go build .` and run the binary), not `go run main.go`: the bridge is split across several files, including `blocklist.go`, and `go run main.go` compiles only one of them.

   The first time you run it, you will be prompted to scan a QR code. Scan the QR code with your WhatsApp mobile app to authenticate.

   After approximately 20 days, you will might need to re-authenticate.

3. **Connect to the MCP server**

   Copy the below json with the appropriate {{PATH}} values:

   ```json
   {
     "mcpServers": {
       "whatsapp": {
         "command": "{{PATH_TO_UV}}", // Run `which uv` and place the output here
         "args": [
           "--directory",
           "{{PATH_TO_SRC}}/whatsapp-mcp/whatsapp-mcp-server", // cd into the repo, run `pwd` and enter the output here + "/whatsapp-mcp-server"
           "run",
           "main.py"
         ]
       }
     }
   }
   ```

   For **Claude**, save this as `claude_desktop_config.json` in your Claude Desktop configuration directory at:

   ```
   ~/Library/Application Support/Claude/claude_desktop_config.json
   ```

   For **Cursor**, save this as `mcp.json` in your Cursor configuration directory at:

   ```
   ~/.cursor/mcp.json
   ```

4. **Restart Claude Desktop / Cursor**

   Open Claude Desktop and you should now see WhatsApp as an available integration.

   Or restart Cursor.

### Windows Compatibility

If you're running this project on Windows, be aware that `go-sqlite3` requires **CGO to be enabled** in order to compile and work properly. By default, **CGO is disabled on Windows**, so you need to explicitly enable it and have a C compiler installed.

#### Steps to get it working:

1. **Install a C compiler**  
   We recommend using [MSYS2](https://www.msys2.org/) to install a C compiler for Windows. After installing MSYS2, make sure to add the `ucrt64\bin` folder to your `PATH`.  
   → A step-by-step guide is available [here](https://code.visualstudio.com/docs/cpp/config-mingw).

2. **Enable CGO and run the app**

   ```bash
   cd whatsapp-bridge
   go env -w CGO_ENABLED=1
   go run .
   ```

Without this setup, you'll likely run into errors like:

> `Binary was compiled with 'CGO_ENABLED=0', go-sqlite3 requires cgo to work.`

## Architecture Overview

This application consists of two main components:

1. **Go WhatsApp Bridge** (`whatsapp-bridge/`): A Go application that connects to WhatsApp's web API, handles authentication via QR code, and stores message history in SQLite. It serves as the bridge between WhatsApp and the MCP server.

2. **Python MCP Server** (`whatsapp-mcp-server/`): A Python server implementing the Model Context Protocol (MCP), which provides standardized tools for Claude to interact with WhatsApp data and send/receive messages.

### Data Storage

- All message history is stored in a SQLite database within the `whatsapp-bridge/store/` directory
- The database maintains tables for chats and messages
- Messages are indexed for efficient searching and retrieval

## Usage

Once connected, you can interact with your WhatsApp contacts through Claude, leveraging Claude's AI capabilities in your WhatsApp conversations.

### MCP Tools

Claude can access the following tools to interact with WhatsApp:

- **search_contacts**: Search for contacts by name or phone number
- **list_messages**: Retrieve messages with optional filters and context
- **list_chats**: List available chats with metadata
- **get_chat**: Get information about a specific chat
- **get_direct_chat_by_contact**: Find a direct chat with a specific contact
- **get_contact_chats**: List all chats involving a specific contact
- **get_last_interaction**: Get the most recent message with a contact
- **get_message_context**: Retrieve context around a specific message
- **send_message**: Send a WhatsApp message to a specified phone number or group JID
- **send_file**: Send a file (image, video, raw audio, document) to a specified recipient
- **send_audio_message**: Send an audio file as a WhatsApp voice message (requires the file to be an .ogg opus file or ffmpeg must be installed)
- **download_media**: Download media from a WhatsApp message and get the local file path

### Media Handling Features

The MCP server supports both sending and receiving various media types:

#### Media Sending

You can send various media types to your WhatsApp contacts:

- **Images, Videos, Documents**: Use the `send_file` tool to share any supported media type.
- **Voice Messages**: Use the `send_audio_message` tool to send audio files as playable WhatsApp voice messages.
  - For optimal compatibility, audio files should be in `.ogg` Opus format.
  - With FFmpeg installed, the system will automatically convert other audio formats (MP3, WAV, etc.) to the required format.
  - Without FFmpeg, you can still send raw audio files using the `send_file` tool, but they won't appear as playable voice messages.

#### Media Downloading

By default, just the metadata of the media is stored in the local database. The message will indicate that media was sent. To access this media you need to use the download_media tool which takes the `message_id` and `chat_jid` (which are shown when printing messages containing the meda), this downloads the media and then returns the file path which can be then opened or passed to another tool.

## Blocking chats

You can keep personal chats out of reach of the MCP server with a blocklist. Copy `blocklist.example.txt` to `blocklist.txt` in the repo root and list the chats to hide. `blocklist.txt` is git-ignored so your entries are never committed. To keep the file somewhere else, set `WHATSAPP_MCP_BLOCKLIST` to its path for both the bridge and the MCP server.

Blocking is per chat and by ID only:

```
# One entry per line; '#' starts a comment
+44 7700 900000                  # phone number: blocks your 1:1 chat with that person
105506358227070@lid              # LID: also blocks that person's 1:1 chat
447700900000@s.whatsapp.net      # phone JID: the same
120363000000000000@g.us          # group JID: blocks that group
```

- A person's phone number and their linked identity (LID) are the same chat. WhatsApp keys some 1:1 chats by LID, so an entry in either form blocks the chat however it is stored (the bridge's `whatsmeow_lid_map` links the two). Listing both is safest.
- Blocking a person does not hide their messages in group chats you haven't blocked. There they stay visible, with their name.
- Display names are not matched: names change and aren't unique. Any line that isn't a number or a JID (a name, or a mistyped number) makes the whole file invalid, and then everything is blocked until it is fixed, so a bad line can never quietly unblock a chat.

What blocking does:

- **Go bridge**: messages and chat metadata from blocked chats are dropped before they are stored in `messages.db` or printed to the console, and media downloads from them are refused.
- **MCP server**: every read tool (`list_messages`, `list_chats`, `search_contacts`, `get_chat`, `get_contact_chats`, `get_last_interaction`, `get_direct_chat_by_contact`, `get_message_context`, `download_media`) excludes blocked chats. A blocked message looks the same as a missing one. Send tools are unchanged.

The file is re-read when it changes, so edits apply without a restart. If the file is missing, nothing is blocked. If it exists but can't be read or parsed, the read tools return an error instead of unfiltered data, and the bridge stores nothing until it is fixed.

Messages stored before a chat was blocked stay in `whatsapp-bridge/store/messages.db`. The MCP server still hides them, but delete them from the database if you don't want them on disk.

## Sender names

WhatsApp now identifies group participants by LID (for example `105506358227070@lid`) rather than phone number. The MCP server turns these into names using the bridge's whatsmeow store, `whatsapp-bridge/store/whatsapp.db`, which it opens read-only:

1. A LID is mapped to the person's phone number (`whatsmeow_lid_map`).
2. The name is the first one found, for either the LID or the phone number, in this order: your address-book name, first name, the name they set themselves (push name), then business name (`whatsmeow_contacts`).
3. If none is found, the name of a 1:1 chat with that person is used, and failing that the raw ID.

Your own messages still show as "Me". Messages returned by `get_message_context` and the chat tools include a `sender_name` or `last_sender_name` field, and 1:1 chats that were only named by number show the contact's name. `search_contacts` also finds people you only share groups with, and returns both their phone JID and LID. The `sender_phone_number` filter in `list_messages` accepts a phone number, phone JID or LID.

Restart the MCP server (for Claude Desktop, quit and reopen it) to pick up these changes. Restart the bridge too: it then gives LID-keyed 1:1 chats real names when it stores them, and applies the LID/phone-aware blocking above before storing anything. Restarting the bridge does not require pairing again.

## Technical Details

1. Claude sends requests to the Python MCP server
2. The MCP server queries the Go bridge for WhatsApp data or directly to the SQLite database
3. The Go accesses the WhatsApp API and keeps the SQLite database up to date
4. Data flows back through the chain to Claude
5. When sending messages, the request flows from Claude through the MCP server to the Go bridge and to WhatsApp

## Troubleshooting

- If you encounter permission issues when running uv, you may need to add it to your PATH or use the full path to the executable.
- Make sure both the Go application and the Python server are running for the integration to work properly.

### Authentication Issues

- **QR Code Not Displaying**: If the QR code doesn't appear, try restarting the authentication script. If issues persist, check if your terminal supports displaying QR codes.
- **WhatsApp Already Logged In**: If your session is already active, the Go bridge will automatically reconnect without showing a QR code.
- **Device Limit Reached**: WhatsApp limits the number of linked devices. If you reach this limit, you'll need to remove an existing device from WhatsApp on your phone (Settings > Linked Devices).
- **No Messages Loading**: After initial authentication, it can take several minutes for your message history to load, especially if you have many chats.
- **WhatsApp Out of Sync**: If your WhatsApp messages get out of sync with the bridge, delete both database files (`whatsapp-bridge/store/messages.db` and `whatsapp-bridge/store/whatsapp.db`) and restart the bridge to re-authenticate.

For additional Claude Desktop integration troubleshooting, see the [MCP documentation](https://modelcontextprotocol.io/quickstart/server#claude-for-desktop-integration-issues). The documentation includes helpful tips for checking logs and resolving common issues.
