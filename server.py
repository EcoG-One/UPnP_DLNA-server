import hashlib
import os
from io import BytesIO
from mutagen.flac import FLAC
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
import re
import urllib.parse
import html
import asyncio
import sqlite3
import logging
import socket
import struct
import uuid
from pathlib import Path
import xml.etree.ElementTree as ET
from aiohttp import web
from mutagen import File as MutagenFile

# Configuration
MEDIA_DIRS = ["D:\EcoG\Music\Moved to NICO"]
BIND_IP = "192.168.178.143"  # Replace with your actual local IP
PORT = 8080
UUID = str(uuid.uuid5(uuid.NAMESPACE_DNS, "python-audiophile-dlna"))
SERVER_NAME = "Python Audiophile Server"
ART_CACHE_DIR = "art_cache"
os.makedirs(ART_CACHE_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("DLNAServer")

# ==========================================
# 1. Database & Metadata Scanner
# ==========================================
class MediaLibrary:
    def __init__(self, db_path="media.db"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        cursor = self.conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS media (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT UNIQUE,
                title TEXT,
                artist TEXT,
                album TEXT,
                mime_type TEXT,
                size INTEGER,
                duration REAL,
                art_hash TEXT
            )
        """)
        self.conn.commit()

    def scan_directories(self, directories):
        logger.info("Starting media scan...")
        cursor = self.conn.cursor()
        supported_exts = {'.flac', '.wav', '.mp3', '.dsf', '.m4a'}

        for directory in directories:
            path = Path(directory)
            for file_path in path.rglob("*"):
                if file_path.is_file() and file_path.suffix.lower() in supported_exts:
                    self._index_file(file_path, cursor)
        self.conn.commit()
        logger.info("Scan complete.")

    def _index_file(self, file_path, cursor):
        try:
            audio = MutagenFile(file_path, easy=True)
            if audio is None:
                return

            title = audio.get("title", [file_path.stem])[0]
            artist = audio.get("artist", ["Unknown Artist"])[0]
            album = audio.get("album", ["Unknown Album"])[0]
            mime_type = f"audio/{file_path.suffix.lower().strip('.')}"
            size = file_path.stat().st_size
            duration = audio.info.length if hasattr(audio, "info") else 0

            # Extract Art
            art_hash = self._extract_and_cache_art(file_path, album)

            cursor.execute(
                """
                INSERT OR REPLACE INTO media (path, title, artist, album, mime_type, size, duration, art_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    str(file_path),
                    title,
                    artist,
                    album,
                    mime_type,
                    size,
                    duration,
                    art_hash,
                ),
            )
        except Exception as e:
            logger.error(f"Failed to parse {file_path}: {e}")

    def _extract_and_cache_art(self, file_path, album_name):
        """Extracts embedded artwork and saves it using a hash of the album name."""
        if album_name == "Unknown Album":
            return None

        # Create a safe, unique filename for the album art
        album_hash = hashlib.md5(album_name.encode("utf-8")).hexdigest()
        cache_path = Path(ART_CACHE_DIR) / f"{album_hash}.jpg"

        # Skip extraction if we already cached art for this album
        if cache_path.exists():
            return album_hash

        try:
            art_data = None
            extension = file_path.suffix.lower()

            # FLAC
            if extension == ".flac":
                audio = FLAC(file_path)
                if audio.pictures:
                    art_data = audio.pictures[0].data

            # MP3 (ID3)
            elif extension == ".mp3":
                audio = MP3(file_path)
                for tag in audio.tags.values():
                    if tag.FrameID == "APIC":
                        art_data = tag.data
                        break

            # M4A / ALAC / MP4
            elif extension in [".m4a", ".mp4"]:
                audio = MP4(file_path)
                if "covr" in audio.tags and audio.tags["covr"]:
                    art_data = audio.tags["covr"][0]

            # Save to disk if found
            if art_data:
                with open(cache_path, "wb") as f:
                    f.write(art_data)
                return album_hash

        except Exception as e:
            logger.warning(f"Could not extract art from {file_path}: {e}")

        return None


import email.utils
import time


# ==========================================
# 2. SSDP Discovery (Upgraded)
# ==========================================
class SSDPProtocol(asyncio.DatagramProtocol):
    def __init__(self, ip, port, uuid):
        self.ip = ip
        self.port = port
        self.uuid = uuid
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport
        sock = transport.get_extra_info("socket")

        # 1. Allow multiple apps to use port 1900
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        # 2. Bind multicast EXPLICITLY to our specific network interface (BIND_IP)
        # This prevents the OS from sending SSDP packets to Docker/VPN adapters
        mreq = socket.inet_aton("239.255.255.250") + socket.inet_aton(self.ip)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

        # 3. Force outbound multicast traffic to use the correct interface
        sock.setsockopt(
            socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(self.ip)
        )

        # 4. Set Time-To-Live so packets don't drop at the first switch
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)

        logger.info(f"SSDP Bound explicitly to interface {self.ip}")

        # Start passive NOTIFY broadcasts
        asyncio.create_task(self.broadcast_notify())

    def datagram_received(self, data, addr):
        msg = data.decode("utf-8", errors="ignore")
        if msg.startswith("M-SEARCH"):
            # Generate RFC 1123 format date (required by strict DLNA clients)
            date_str = email.utils.formatdate(
                timeval=None, localtime=False, usegmt=True
            )

            response = (
                "HTTP/1.1 200 OK\r\n"
                "CACHE-CONTROL: max-age=1800\r\n"
                f"DATE: {date_str}\r\n"
                "EXT:\r\n"
                f"LOCATION: http://{self.ip}:{self.port}/description.xml\r\n"
                f"SERVER: Linux/5.0 UPnP/1.0 {SERVER_NAME}/1.0\r\n"
                "ST: upnp:rootdevice\r\n"
                f"USN: uuid:{self.uuid}::upnp:rootdevice\r\n"
                "\r\n"
            )
            # Send unicast reply directly back to the device that asked
            self.transport.sendto(response.encode("utf-8"), addr)

    async def broadcast_notify(self):
        """Periodically announce the server's existence to the network."""
        while True:
            if self.transport:
                notify_msg = (
                    "NOTIFY * HTTP/1.1\r\n"
                    "HOST: 239.255.255.250:1900\r\n"
                    "CACHE-CONTROL: max-age=1800\r\n"
                    f"LOCATION: http://{self.ip}:{self.port}/description.xml\r\n"
                    f"SERVER: Linux/5.0 UPnP/1.0 {SERVER_NAME}/1.0\r\n"
                    "NT: upnp:rootdevice\r\n"
                    "NTS: ssdp:alive\r\n"
                    f"USN: uuid:{self.uuid}::upnp:rootdevice\r\n"
                    "\r\n"
                )
                # Multicast the notification
                self.transport.sendto(
                    notify_msg.encode("utf-8"), ("239.255.255.250", 1900)
                )
            await asyncio.sleep(30)  # Broadcast every 30 seconds


# ==========================================
# 3. HTTP Server & UPnP SOAP Endpoints
# ==========================================
class UPnPServer:
    def __init__(self, library, ip, port, uuid):
        self.library = library
        self.ip = ip
        self.port = port
        self.uuid = uuid
        self.app = web.Application(middlewares=[self.dlna_headers_middleware])
        self.setup_routes()

    @web.middleware
    async def dlna_headers_middleware(self, request, handler):
        # Critical for Pioneer N-50AE compatibility
        response = await handler(request)
        response.headers['transferMode.dlna.org'] = 'Streaming'
        response.headers['contentFeatures.dlna.org'] = 'DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000'
        return response

    def setup_routes(self):
        self.app.router.add_get("/description.xml", self.handle_description)
        self.app.router.add_post("/ContentDirectory/control", self.handle_soap)
        self.app.router.add_get("/media/{id}", self.handle_media)
        # Art Route
        self.app.router.add_get("/art/{hash}", self.handle_art)

    async def handle_description(self, request):
        # UPnP Device Architecture XML
        xml = f"""<?xml version="1.0"?>
        <root xmlns="urn:schemas-upnp-org:device-1-0">
            <specVersion><major>1</major><minor>0</minor></specVersion>
            <device>
                <deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>
                <friendlyName>{SERVER_NAME}</friendlyName>
                <manufacturer>Custom Python Server</manufacturer>
                <modelName>Audiophile DLNA</modelName>
                <UDN>uuid:{self.uuid}</UDN>
                <serviceList>
                    <service>
                        <serviceType>urn:schemas-upnp-org:service:ContentDirectory:1</serviceType>
                        <serviceId>urn:upnp-org:serviceId:ContentDirectory</serviceId>
                        <controlURL>/ContentDirectory/control</controlURL>
                        <eventSubURL>/ContentDirectory/event</eventSubURL>
                        <SCPDURL>/ContentDirectory.xml</SCPDURL>
                    </service>
                </serviceList>
            </device>
        </root>"""
        return web.Response(text=xml, content_type='text/xml')

    async def handle_soap(self, request):
        body = await request.text()

        # Safely extract the ObjectID using regex to bypass complex SOAP namespaces
        match = re.search(r'<ObjectID[^>]*>(.*?)</ObjectID>', body)
        object_id = match.group(1) if match else "0"

        cursor = self.library.conn.cursor()
        items_xml = ""
        item_count = 0

        # ==========================================
        # ROOT VIEW: List all Artists
        # ==========================================
        if object_id == "0":
            cursor.execute("SELECT DISTINCT artist FROM media ORDER BY artist")
            artists = cursor.fetchall()

            for (artist,) in artists:
                if not artist: continue
                # Create a URL-safe virtual ID for the artist
                safe_artist = urllib.parse.quote(artist)
                v_id = f"artist_{safe_artist}"

                items_xml += f"""
                <container id="{v_id}" parentID="0" restricted="1" searchable="0">
                    <dc:title>{html.escape(artist)}</dc:title>
                    <upnp:class>object.container.person.musicArtist</upnp:class>
                </container>"""
                item_count += 1

        # ==========================================
        # ARTIST VIEW: List Albums by selected Artist
        # ==========================================
        elif object_id.startswith("artist_"):
            artist = urllib.parse.unquote(object_id.replace("artist_", ""))
            cursor.execute("SELECT DISTINCT album FROM media WHERE artist=? ORDER BY album", (artist,))
            albums = cursor.fetchall()

            for (album,) in albums:
                if not album: continue
                safe_album = urllib.parse.quote(album)
                v_id = f"album_{urllib.parse.quote(artist)}_{safe_album}"

                items_xml += f"""
                <container id="{v_id}" parentID="{object_id}" restricted="1" searchable="0">
                    <dc:title>{html.escape(album)}</dc:title>
                    <upnp:class>object.container.album.musicAlbum</upnp:class>
                </container>"""
                item_count += 1

        # ==========================================
        # ALBUM VIEW: List Tracks in selected Album
        # ==========================================
        elif object_id.startswith("album_"):
            parts = object_id.split("_", 2)
            artist = urllib.parse.unquote(parts[1])
            album = urllib.parse.unquote(parts[2])

            # Select the art_hash as well
            cursor.execute(
                "SELECT id, title, mime_type, size, duration, art_hash FROM media WHERE artist=? AND album=? ORDER BY title",
                (artist, album),
            )
            tracks = cursor.fetchall()

            for track in tracks:
                t_id, title, mime_type, size, duration, art_hash = track
                m, s = divmod(int(duration), 60)
                h, m = divmod(m, 60)
                dur_str = f"{h}:{m:02d}:{s:02d}.000"

                # Build the Art URI if it exists
                art_tag = ""
                if art_hash:
                    art_url = f"http://{self.ip}:{self.port}/art/{art_hash}"
                    # Add standard DLNA profiles for JPEG
                    art_tag = f"""
                    <upnp:albumArtURI dlna:profileID="JPEG_TN">{art_url}</upnp:albumArtURI>
                    <upnp:icon>{art_url}</upnp:icon>
                    """

                items_xml += f"""
                <item id="{t_id}" parentID="{object_id}" restricted="1">
                    <dc:title>{html.escape(title)}</dc:title>
                    <upnp:class>object.item.audioItem.musicTrack</upnp:class>
                    <upnp:artist>{html.escape(artist)}</upnp:artist>
                    <upnp:album>{html.escape(album)}</upnp:album>
                    {art_tag}
                    <res protocolInfo="http-get:*:{mime_type}:DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000" 
                         size="{size}" duration="{dur_str}">http://{self.ip}:{self.port}/media/{t_id}</res>
                </item>"""
                item_count += 1

        # ==========================================
        # WRAP AND RETURN DIDL-LITE XML
        # ==========================================
        didl = f"""<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" 
                              xmlns:dc="http://purl.org/dc/elements/1.1/" 
                              xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">
            {items_xml}
        </DIDL-Lite>"""

        soap_response = f"""<?xml version="1.0" encoding="utf-8"?>
        <s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
            <s:Body>
                <u:BrowseResponse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1">
                    <Result>{html.escape(didl)}</Result>
                    <NumberReturned>{item_count}</NumberReturned>
                    <TotalMatches>{item_count}</TotalMatches>
                    <UpdateID>1</UpdateID>
                </u:BrowseResponse>
            </s:Body>
        </s:Envelope>"""

        return web.Response(text=soap_response, content_type='text/xml')

    async def handle_media(self, request):
        # Aiohttp's FileResponse automatically handles HTTP 206 Range Requests natively!
        # This is vital for the Pioneer N-50AE to seek through tracks.
        media_id = request.match_info['id']
        cursor = self.library.conn.cursor()
        cursor.execute("SELECT path FROM media WHERE id=?", (media_id,))
        row = cursor.fetchone()

        if row and Path(row[0]).exists():
            return web.FileResponse(row[0])
        return web.Response(status=404)

    async def handle_art(self, request):
        art_hash = request.match_info["hash"]
        art_path = Path(ART_CACHE_DIR) / f"{art_hash}.jpg"

        if art_path.exists():
            return web.FileResponse(art_path, headers={"Content-Type": "image/jpeg"})
        return web.Response(status=404)


# ==========================================
# 4. Main Event Loop
# ==========================================
async def main():
    library = MediaLibrary()
    library.scan_directories(MEDIA_DIRS)

    loop = asyncio.get_running_loop()
    
    # Start SSDP
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: SSDPProtocol(BIND_IP, PORT, UUID),
        local_addr=('0.0.0.0', 1900),
        allow_broadcast=True
    )
    
    # Start Web Server
    upnp_server = UPnPServer(library, BIND_IP, PORT, UUID)
    runner = web.AppRunner(upnp_server.app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    
    logger.info(f"Server started on http://{BIND_IP}:{PORT}")
    
    try:
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        transport.close()
        await runner.cleanup()

if __name__ == '__main__':
    asyncio.run(main())
