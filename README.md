# Audiophile Python DLNA/UPnP Media Server

A lightweight, highly optimized, and strictly compliant DLNA/UPnP media server written entirely in Python. 

Designed from the ground up for high-end, strict audiophile network players (such as the **Pioneer N-70AE**), this server prioritizes perfect DIDL-Lite XML formatting, native HTTP Range Request support for scrubbing high-res FLAC/DSD files, and instantaneous metadata browsing without database bloat.

---

## 🌟 Key Features

* **Strict UPnP/DLNA Compliance:** Perfectly formats DIDL-Lite SOAP responses, complete with `xmlns:dlna` namespaces, exact pagination, and correct `BrowseMetadata` handling to satisfy the strictest audiophile renderers.
* **Native HTTP 206 Range Requests:** Seamlessly supports seeking, skipping, and scrubbing through massive high-res lossless audio files without buffering issues.
* **Asynchronous Core:** Built on `asyncio` and `aiohttp` for lightning-fast concurrent requests and non-blocking media streaming.
* **Smart Artwork Caching:** Uses `mutagen` to extract embedded album art (FLAC, MP3, ALAC/M4A) precisely once during the initial scan, caching it locally to ensure instant UI loading on the renderer.
* **Virtual Hierarchy:** Organizes media logically by `Artist -> Album -> Track` on the fly using virtual `ObjectID` routing, keeping the SQLite schema flat and incredibly fast.
* **Resilient SSDP Discovery:** explicitly binds multicast UDP sockets and broadcasts periodic `ssdp:alive` notifications to bypass aggressive OS-level network suppression.

---

## 🛠️ Prerequisites

* Python 3.8 or higher
* Your media library mounted locally or via a fast network drive.

### Required Python Libraries
Install the required dependencies via pip:
```bash
pip install aiohttp mutagen
```
---

## 🚀 Installation & Setup

1.  Clone the repository:

```bash
git clone [https://github.com/EcoG-One/audiophile-dlna-server.git](https://github.com/EcoG-One/audiophile-dlna-server.git)
cd audiophile-dlna-server
```
2.  Configure the Server: 

Open server.py in a text editor and update the core configuration variables at the top of the file:

```bash
MEDIA_DIRS = ["C:/Path/To/Your/Music"] # List of directories to scan
BIND_IP = "192.168.1.100"              # Your machine's exact local IPv4 address
PORT = 8080                            # HTTP port for streaming and XML
```                           

3.  Run the Server:

```shell 
python server.py
``` 
The server will recursively scan your directories, build the media.db SQLite database, extract cover art to the art_cachefolder, and announce itself to your local network.

## 🪟 Critical Windows Deployment Notes
If you are running this server on Windows and it does not appear on your UPnP control point (or your audiophile network player), Windows is likely blocking the SSDP multicast traffic. Follow these exact steps to clear the native roadblocks:

1.  Stop the Native Windows SSDP Hijacker
Windows has a native service that intercepts Port 1900 traffic, starving Python of discovery requests.

     - Press ```Win + R```, type ```services.msc```.

     - Locate SSDP Discovery .

     - Right-click -> Stop (and set Startup type to Manual or Disabled).

2.  Punch the Firewall Holes
Open **PowerShell** as **Administrator** and run these commands to allow unsolicited inbound UPnP traffic:
```shell
New-NetFirewallRule -DisplayName "Python DLNA Discovery (UDP 1900)" -Direction Inbound -LocalPort 1900 -Protocol UDP -Action Allow
New-NetFirewallRule -DisplayName "Python DLNA Web Server (TCP 8080)" -Direction Inbound -LocalPort 8080 -Protocol TCP -Action Allow
``` 
3.  Verify Network Profile
Ensure your Windows Network Profile is set to Private . If it is set to "Public," Windows will block all multicast packets at the kernel level regardless of your firewall rules.

## 🏃‍♂️ Running as a Background Service
For a permanent, polished installation on Windows, it is highly recommended to run this script as a hidden background service using NSSM (Non-Sucking Service Manager) .

1  Download NSSM and open a command prompt in the extracted folder.

2  Run ```nssm install PythonDLNA```.

3  In the GUI:

      - Path: ```C:\Path\To\Python\python.exe```

      - Arguments: ```C:\Path\To\audiophile-dlna-server\server.py```

      - Details Tab: Set Display Name to "Audiophile DLNA Server".

4  Click Install Service .

The server will now automatically start silently in the background whenever your PC boots up.

## 🤝 Contributing
Pull requests for optimizing the SQLite queries, expanding the virtual folder hierarchy (eg, adding a "By Genre" or "Folder View" route), or implementing the watchdoglibrary for real-time filesystem monitoring are welcome!

## 📜 License
This project is licensed under the MIT License - see the LICENSE file for details.
